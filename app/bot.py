import os
import re
import json
import time
import secrets
import logging
import html
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest


# =========================
# Load ENV
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HR_CHAT_ID_RAW = os.getenv("HR_CHAT_ID", "").strip()
DOMAIN = os.getenv("DOMAIN", "nuanu.com").strip()
GSHEET_ID = os.getenv("GSHEET_ID", "").strip()
GSHEET_TAB = os.getenv("GSHEET_TAB", "requests").strip()
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_CREDS", "/secrets/service_account.json").strip()
TZ_NAME = os.getenv("TZ", "Asia/Makassar").strip()


def must_env(name: str, value: str):
    if not value:
        raise SystemExit(f"ENV {name} kosong. Set dulu di .env")


must_env("BOT_TOKEN", BOT_TOKEN)
must_env("HR_CHAT_ID", HR_CHAT_ID_RAW)
must_env("GSHEET_ID", GSHEET_ID)
must_env("GOOGLE_CREDS", GOOGLE_CREDS_PATH)


# HR_CHAT_ID bisa numeric atau @username
def parse_chat_id(raw: str):
    raw = raw.strip()
    if raw.startswith("@"):
        return raw
    # buang spasi / newline
    raw = raw.replace(" ", "")
    return int(raw)


HR_CHAT_ID = parse_chat_id(HR_CHAT_ID_RAW)

# =========================
# Logging (matikan httpx biar token gak ke-log)
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("nuanu-email-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # IMPORTANT: hide request logs


# =========================
# Google Sheet Setup
# =========================
HEADERS = [
    "request_id",
    "created_at",
    "requester_tg",
    "first_name",
    "last_name",
    "primary_email",
    "secondary_email",
    "phone_number",
    "status",
    "submitted_to_hr_at",
    "error",
]

HEADERS.extend(["activation_code", "department"])

_gs_client = None
_gs_ws = None


def now_str():
    tz = ZoneInfo(TZ_NAME)
    return datetime.now(tz).isoformat(timespec="seconds")


def init_gsheet():
    global _gs_client, _gs_ws

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=scopes)
    _gs_client = gspread.authorize(creds)

    sh = _gs_client.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet(GSHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=GSHEET_TAB, rows=1000, cols=len(HEADERS))

    # pastikan header ada
    first_row = ws.get("A1:Z1")
    if not first_row or not first_row[0]:
        ws.update(range_name="A1", values=[HEADERS])
    else:
        # kalau header beda, biarkan (atau bisa overwrite)
        pass

    current_header = ws.row_values(1)
    if current_header[:11] != HEADERS[:11]:
        raise RuntimeError("Google Sheet header mismatch")
    if len(current_header) < 12 or current_header[11] != "activation_code":
        raise RuntimeError("Activation code header mismatch")

    if ws.col_count < len(HEADERS):
        ws.add_cols(len(HEADERS) - ws.col_count)

    existing_m = ws.col_values(13)

    if existing_m and existing_m[0] not in ("", "department"):
        raise RuntimeError("Column M is already in use")

    if not existing_m or not existing_m[0]:
        if any(v.strip() for v in existing_m[1:]):
            raise RuntimeError("Column M contains existing data")
        ws.update_acell("M1", "department")

    _gs_ws = ws
    log.info("Google Sheet ready: tab=%s", GSHEET_TAB)


def append_request_row(row: list):
    # row harus sesuai HEADERS
    _gs_ws.append_row(row, value_input_option="USER_ENTERED")


# =========================
# Conversation States
# =========================
FIRST_NAME, LAST_NAME, PRIMARY_LOCAL, SECONDARY_EMAIL, PHONE_NUMBER, DEPARTMENT, CONFIRM = range(7)


# =========================
# Validators
# =========================
EMAIL_LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$")  # 3..64 chars
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def build_summary(data: dict) -> str:
    # hasil yang tampil ke user (simple)
    full_primary = f"{data['primary_local']}@{DOMAIN}"
    secondary = data.get("secondary_email") or "-"
    phone = data.get("phone_number") or "-"
    first_name = data.get("first_name") or "-"
    last_name = data.get("last_name") or "-"

    # Escape for HTML
    safe_first = html.escape(first_name)
    safe_last = html.escape(last_name)
    safe_primary = html.escape(full_primary)
    safe_secondary = html.escape(secondary)
    safe_phone = html.escape(phone)
    safe_department = html.escape(data.get("department") or "-")

    lines = [
        "✅ <b>User Information</b>",
        f"First Name : {safe_first}",
        f"Last Name  : {safe_last}",
        f"Primary    : {safe_primary}",
        f"Secondary  : {safe_secondary}",
        f"Phone      : {safe_phone}",
        f"Department : {safe_department}",
    ]
    return "\n".join(lines)


def build_hr_text(request_id: str, data: dict) -> str:
    # KIRIM KE HR: PLAIN TEXT (NO MARKDOWN) biar gak error entity
    full_primary = f"{data['primary_local']}@{DOMAIN}"
    secondary = data.get("secondary_email") or "-"
    phone = data.get("phone_number") or "-"
    first_name = data.get("first_name") or "-"
    last_name = data.get("last_name") or "-"

    text = (
        "📩 New Company Email Request\n"
        f"Request ID   : {request_id}\n"
        f"Name         : {first_name} {last_name}\n"
        f"Primary Email: {full_primary}\n"
        f"Secondary    : {secondary}\n"
        f"Phone        : {phone}\n"
        f"Department   : {data.get('department') or '-'}\n\n"
        "Status: PENDING_HR"
    )
    return text


# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    text = (
        "Halo! 👋\n"
        "Aku bantu bikin <b>Email Provisioning Request</b> super simpel.\n\n"
        "Isi data berikut ya.\n"
        "Ketik /cancel untuk batal kapan saja.\n\n"
        "1) First Name?"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    return FIRST_NAME


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Dibatalkan. Ketik /start untuk mulai lagi.")
    return ConversationHandler.END


async def first_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()
    if len(val) < 1:
        await update.message.reply_text("First Name tidak boleh kosong. Coba lagi:")
        return FIRST_NAME

    context.user_data["first_name"] = val
    await update.message.reply_text("2) Last Name?")
    return LAST_NAME


async def last_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()
    if len(val) < 1:
        await update.message.reply_text("Last Name tidak boleh kosong. Coba lagi:")
        return LAST_NAME

    context.user_data["last_name"] = val
    await update.message.reply_text(
        f"3) Primary email (cukup bagian depan saja).\n"
        f"Contoh: <code>david</code> (nanti jadi david@{DOMAIN})",
        parse_mode="HTML",
    )
    return PRIMARY_LOCAL


async def primary_local(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip().lower()

    if not EMAIL_LOCAL_RE.match(val):
        await update.message.reply_text(
            "Format primary email tidak valid.\n"
            "Rules: huruf/angka + boleh . _ - (3-64 char), tidak boleh mulai/akhir pakai simbol.\n"
            "Coba lagi:"
        )
        return PRIMARY_LOCAL

    context.user_data["primary_local"] = val
    await update.message.reply_text(
        "4) Secondary email? (opsional)\n"
        "Ketik `-` kalau tidak ada."
    )
    return SECONDARY_EMAIL


async def secondary_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()
    if val == "-" or val == "":
        context.user_data["secondary_email"] = ""
    else:
        if not EMAIL_RE.match(val):
            await update.message.reply_text("Secondary email tidak valid. Coba lagi atau ketik <code>-</code>:", parse_mode="HTML")
            return SECONDARY_EMAIL
        context.user_data["secondary_email"] = val

    await update.message.reply_text(
        "5) Phone number? (opsional)\n"
        "Ketik `-` kalau tidak ada."
    )
    return PHONE_NUMBER


async def phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()
    if val == "-" or val == "":
        context.user_data["phone_number"] = ""
    else:
        # very loose validation
        cleaned = re.sub(r"[^\d+]", "", val)
        if len(cleaned) < 6:
            await update.message.reply_text("Phone number terlalu pendek. Coba lagi atau ketik `-`:")
            return PHONE_NUMBER
        context.user_data["phone_number"] = val

    await update.message.reply_text(
        "6) Department? (wajib)\n"
        "Contoh: IT Department / HR Department / Finance"
    )
    return DEPARTMENT


async def department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()

    if (not val or len(val) > 80
            or val[0] in "=+-@'"
            or "\n" in val or "\r" in val):
        await update.message.reply_text(
            "Department wajib diisi (maksimal 80 karakter). Coba lagi:"
        )
        return DEPARTMENT

    context.user_data["department"] = val

    summary = build_summary(context.user_data)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Kirim ke HR", callback_data="confirm_send")],
        [InlineKeyboardButton("🔁 Ulang dari awal", callback_data="confirm_restart")],
        [InlineKeyboardButton("❌ Batal", callback_data="confirm_cancel")],
    ])

    await update.message.reply_text(
        summary + "\n\nKirim ke HR sekarang?",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    return CONFIRM


async def safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as e:
        # ini sering muncul: "Message is not modified"
        if "Message is not modified" in str(e):
            return
        raise


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_cancel":
        context.user_data.clear()
        await safe_edit(query, "❌ Dibatalkan. Ketik /start untuk mulai lagi.")
        return ConversationHandler.END

    if query.data == "confirm_restart":
        context.user_data.clear()
        await safe_edit(query, "Oke, ulang dari awal.\n\n1) First Name?")
        return FIRST_NAME

    if query.data != "confirm_send":
        await safe_edit(query, "Aksi tidak dikenali. Ketik /start untuk mulai lagi.")
        return ConversationHandler.END

    # ============ SUBMIT ============
    data = dict(context.user_data)
    requester = update.effective_user
    requester_tag = f"{requester.full_name} (@{requester.username})" if requester.username else requester.full_name

    request_id = f"REQ-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{requester.id}-{int(time.time())%10000}"
    created_at = now_str()

    full_primary = f"{data['primary_local']}@{DOMAIN}"

    # Cek koneksi sheet dulu
    if _gs_ws is None:
        # Coba reconnect 1x
        try:
            init_gsheet()
        except Exception:
            pass

    if _gs_ws is None:
        await safe_edit(query, "⚠️ Gagal koneksi ke Google Sheet. Cek log server/credentials.")
        return ConversationHandler.END

    # simpan ke sheet (status awal)
    row = [
        request_id,
        created_at,
        requester_tag,
        data.get("first_name", ""),
        data.get("last_name", ""),
        full_primary,
        data.get("secondary_email", ""),
        data.get("phone_number", ""),
        "PENDING_HR",
        "",
        "",
        "",  # activation_code in column L
        data.get("department", ""),
    ]

    sheet_ok = True
    sheet_err = ""
    try:
        append_request_row(row)
    except Exception as e:
        sheet_ok = False
        sheet_err = str(e)
        log.exception("Append to sheet failed")

    # kirim ke HR
    hr_ok = True
    hr_err = ""
    submitted_at = now_str()
    hr_text = build_hr_text(request_id, data)

    try:
        # Create buttons
        hr_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"HR_APPROVE|{request_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"HR_REJECT|{request_id}"),
            ]
        ])

        await context.bot.send_message(
            chat_id=HR_CHAT_ID,
            text=hr_text,
            disable_web_page_preview=True,
            reply_markup=hr_keyboard,
        )
    except Exception as e:
        hr_ok = False
        hr_err = str(e)
        log.exception("send to HR failed")

    # kasih feedback ke user
    if sheet_ok and hr_ok:
        result_text = (
            "✅ Request terkirim ke HR!\n\n"
            f"Request ID: {request_id}\n"
            f"Name: {data.get('first_name')} {data.get('last_name')}\n"
            f"Email: {full_primary}\n\n"
            "Status: PENDING_HR"
        )
        await safe_edit(query, result_text)
    else:
        fail_lines = ["⚠️ Ada masalah saat submit:"]
        if not sheet_ok:
            fail_lines.append(f"- Sheet error: {sheet_err}")
        if not hr_ok:
            fail_lines.append(f"- HR send error: {hr_err}")

        fail_lines.append("\nKetik /start untuk coba lagi.")
        await safe_edit(query, "\n".join(fail_lines))

    context.user_data.clear()
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Terjadi error internal. Coba /start lagi ya.")
    except Exception:
        pass


async def hr_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split("|", 1)
    if len(parts) != 2:
        return

    action, request_id = parts

    user_name = update.effective_user.full_name
    new_status = ""
    result_text = ""

    if action == "HR_APPROVE":
        new_status = "APPROVED"
        result_text = f"✅ Approved by {user_name}"
    elif action == "HR_REJECT":
        new_status = "REJECTED"
        result_text = f"❌ Rejected by {user_name}"
    else:
        return

    # Update Sheet
    sheet_msg = ""
    try:
        # Re-init if needed
        if _gs_ws is None:
            try:
                init_gsheet()
            except Exception:
                pass

        if _gs_ws:
            # Find the row by Request ID (Column 1)
            cell = _gs_ws.find(request_id)
            if cell:
                # Status is column 9 (1-based index for 'status' in HEADERS list)
                _gs_ws.update_cell(cell.row, 9, new_status)
                sheet_msg = f" (Sheet updated)"
            else:
                sheet_msg = " (⚠️ Row not found)"
        else:
             sheet_msg = " (⚠️ Sheet not connected)"

    except Exception as e:
        log.exception("Failed to update sheet for %s", request_id)
        sheet_msg = f" (⚠️ Error: {str(e)})"

    # Update Telegram Message (remove buttons)
    # Update Telegram Message (remove buttons)
    current_text = query.message.text_markdown if query.message.text_markdown else query.message.text

    # Replace the status line
    if "Status: PENDING_HR" in current_text:
        current_text = current_text.replace("Status: PENDING_HR", f"Status: {new_status}")

    # Append approval info
    final_text = f"{current_text}\n\n{result_text}{sheet_msg}"

    try:
        await query.edit_message_text(text=final_text, reply_markup=None)
    except BadRequest:
        pass


def main():
    try:
        init_gsheet()
    except Exception:
        log.exception("Failed to initialize Google Sheet on startup")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, first_name)],
            LAST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, last_name)],
            PRIMARY_LOCAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, primary_local)],
            SECONDARY_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, secondary_email)],
            PHONE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_number)],
            DEPARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, department)],
            CONFIRM: [CallbackQueryHandler(confirm_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        allow_reentry=True,
    )

    app.add_handler(CallbackQueryHandler(hr_action_callback, pattern=r"^HR_"))
    app.add_handler(conv)
    app.add_error_handler(error_handler)

    log.info("Bot started. HR_CHAT_ID=%s", HR_CHAT_ID_RAW)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
