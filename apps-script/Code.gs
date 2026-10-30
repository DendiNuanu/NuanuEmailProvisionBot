/**
 * NUANU - Google Workspace Email Provisioning
 *
 * Flow: Telegram -> Google Sheets -> HR APPROVED -> Google Apps Script.
 * Sheet column names (case-sensitive): request_id, first_name, last_name,
 * primary_email, secondary_email, status, error, department.
 *
 * SAFETY:
 * - Off until ENABLE_PROVISIONING is changed to true.
 * - Only APPROVED rows are processed.
 * - Existing Google users are NEVER modified.
 * - Passwords are NEVER written to Sheets; one HTML email with credentials goes to configured recipients.
 * - A mail problem cannot change a successfully created user to FAILED.
 */
const CONFIG = {
  DOMAIN: 'nuanu.com',
  SHEET_ID: '1J0U-GHry4oASFMXcKSMcCykhpBYst5URxnYjr4-UHSw',
  SHEET_NAME: 'requests',
  TRIGGER_STATUS: 'APPROVED',
  DEFAULT_OU: '/',
  ORGANIZATION_NAME: 'Nuanu',

  // Production enabled. Only status APPROVED is processed.
  ENABLE_PROVISIONING: true,
  MAX_REQUESTS_PER_RUN: 10,

  // ONE email containing temporary credentials goes to these recipients.
  // Restrict access to this distribution list.
  NOTIFY_EMAILS: [
    'dendi@nuanu.com',
    'ayu@nuanu.com',
    'hr.admin@nuanu.com',
    'it.nuanu@nuanu.com',
    'yogadarma@nuanu.com',
    'it.admin@nuanu.com',
    'meyta@nuanu.com',
    'recruitment@nuanu.com'
  ]
};

/** Main function: keep the name because the existing time trigger uses it. */
function processRequests() {
  if (!CONFIG.ENABLE_PROVISIONING) {
    console.log('Provisioning is DISABLED. No account will be created.');
    return;
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    console.log('Another provisioning run is still active.');
    return;
  }

  try {
    const { sheet, data, col } = loadSheet_();
    let handled = 0;

    for (let r = 1; r < data.length; r++) {
      const row = data[r];
      const status = text_(row[col.status]).toUpperCase();
      if (status !== CONFIG.TRIGGER_STATUS) continue;
      if (handled >= CONFIG.MAX_REQUESTS_PER_RUN) break;
      handled++;

      const rowNumber = r + 1;
      const requestId = text_(row[col.request_id]);
      const firstName = text_(row[col.first_name]);
      const lastName = text_(row[col.last_name]);
      const rawEmail = text_(row[col.primary_email]).toLowerCase();
      const email = rawEmail.includes('@')
        ? rawEmail
        : rawEmail + '@' + CONFIG.DOMAIN;
      const department = text_(row[col.department]);

      // Fail closed: never create accounts with missing / malformed data.
      const validEmail = /^[a-z0-9][a-z0-9._-]*@[a-z0-9.-]+$/.test(email) &&
        email.endsWith('@' + CONFIG.DOMAIN);
      if (!requestId || !validEmail || !firstName || !lastName ||
          !department || !CONFIG.NOTIFY_EMAILS.length) {
        setResult_(sheet, rowNumber, col, 'NEEDS_REVIEW',
          'Missing/invalid request ID, email, name, department, or recipients');
        continue;
      }

      // A read or permission failure must NOT be interpreted as "user missing".
      let existing;
      try {
        existing = findExistingUser_(email);
      } catch (err) {
        setResult_(sheet, rowNumber, col, 'NEEDS_REVIEW',
          'Directory lookup failed: ' + safeError_(err));
        continue;
      }
      if (existing) {
        setResult_(sheet, rowNumber, col, 'SKIPPED',
          'Account already exists. No existing user was modified.');
        continue;
      }

      // Check the mail service BEFORE generating an account whose temporary
      // password might otherwise be impossible to deliver.
      try {
        const requiredRecipients = CONFIG.NOTIFY_EMAILS.length;
        const remaining = MailApp.getRemainingDailyQuota();
        if (remaining < requiredRecipients) {
          setResult_(sheet, rowNumber, col, 'MAIL_BLOCKED',
            'Not enough MailApp recipient quota. No account created.');
          continue;
        }
      } catch (err) {
        setResult_(sheet, rowNumber, col, 'MAIL_BLOCKED',
          'MailApp unavailable/not authorized. No account created: ' + safeError_(err));
        continue;
      }

      const tempPassword = generateTempPassword_();
      let createdUser;
      try {
        createdUser = AdminDirectory.Users.insert({
          primaryEmail: email,
          name: { givenName: firstName, familyName: lastName },
          password: tempPassword,
          changePasswordAtNextLogin: true,
          orgUnitPath: CONFIG.DEFAULT_OU,
          organizations: [{
            name: CONFIG.ORGANIZATION_NAME,
            type: 'work',
            primary: true,
            department: department
          }]
        });
      } catch (err) {
        // The API result can be ambiguous after transient errors. No retries
        // are attempted automatically; an admin should inspect the user first.
        setResult_(sheet, rowNumber, col, 'NEEDS_REVIEW',
          'User insert failed or outcome unknown; inspect Directory: ' + safeError_(err));
        continue;
      }

      // Account creation is successful independently of email delivery.
      // If this sheet write fails, the existing-user check on the next run
      // avoids creating the same account a second time.
      setResult_(sheet, rowNumber, col, 'DONE',
        'Account created. Preparing notifications.');

      // ONE email for the eight admins, including the temporary password.
      // No separate welcome email to secondary or fallback addresses.
      let emailResult = 'Account created; one credentials email sent to ' +
        CONFIG.NOTIFY_EMAILS.length + ' recipients';
      try {
        MailApp.sendEmail({
          to: CONFIG.NOTIFY_EMAILS.join(','),
          subject: '[NuanuBot] [Provisioned] ' + email,
          name: 'Nuanu IT Support',
          body: getEmailText(firstName, email, tempPassword),
          htmlBody: getEmailHtml(firstName, email, tempPassword)
        });
      } catch (err) {
        emailResult = 'Account created; credentials email FAILED. ' +
          'Manual password reset/delivery required: ' + safeError_(err);
      }

      // Do not write the password to Sheets or logs, even on failure.
      setResult_(sheet, rowNumber, col, 'DONE', emailResult);
      console.log('Provisioned: ' + (createdUser.primaryEmail || email));
    }
    console.log('Requests handled this run: ' + handled);
  } finally {
    lock.releaseLock();
  }
}

/** Read-only preview: safe to run before enabling provisioning. */
function previewApprovedRequests() {
  const { data, col } = loadSheet_();
  const waiting = [];
  for (let r = 1; r < data.length; r++) {
    if (text_(data[r][col.status]).toUpperCase() === CONFIG.TRIGGER_STATUS) {
      waiting.push({
        row: r + 1,
        email: text_(data[r][col.primary_email]),
        department: text_(data[r][col.department]) || '(MISSING)'
      });
    }
  }
  console.log('APPROVED requests awaiting processing: ' + waiting.length);
  waiting.slice(0, 30).forEach(x => console.log(JSON.stringify(x)));
  if (waiting.length > 30) console.log('Showing only first 30 requests.');
}

/** Read-only inspection of the already-approved October 30 test account. */
function inspectTestAccount() {
  const email = 'test123@nuanu.com';
  const user = findExistingUser_(email);
  if (!user) {
    console.log(email + ': NOT FOUND');
    return;
  }
  console.log(JSON.stringify({
    email: user.primaryEmail,
    name: user.name,
    organizations: user.organizations || []
  }, null, 2));
}

/** Read-only diagnostic: checks quota/permission, sends no email. */
function checkMailPermission() {
  console.log('Remaining recipients today: ' + MailApp.getRemainingDailyQuota());
}

/* ============================ HELPERS ============================ */
function loadSheet_() {
  const sheet = SpreadsheetApp.openById(CONFIG.SHEET_ID)
    .getSheetByName(CONFIG.SHEET_NAME);
  if (!sheet) throw new Error('Sheet not found: ' + CONFIG.SHEET_NAME);
  const data = sheet.getDataRange().getValues();
  if (data.length < 1) throw new Error('Missing header row');
  const headers = data[0].map(text_);
  const required = [
    'request_id', 'status', 'primary_email', 'secondary_email',
    'first_name', 'last_name', 'error', 'department', 'activation_code'
  ];
  const col = {};
  required.forEach(name => {
    const i = headers.indexOf(name);
    if (i < 0) throw new Error('Missing column: ' + name);
    col[name] = i;
  });
  // Do not shift or overwrite the columns used by the Telegram bot.
  if (col.status !== 8 || col.error !== 10 ||
      col.activation_code !== 11 || col.department !== 12) {
    throw new Error('Unexpected columns: expected I=status, K=error, L=activation_code, M=department');
  }
  return { sheet, data, col };
}

function text_(value) {
  return String(value == null ? '' : value).trim();
}

function setResult_(sheet, rowNumber, col, status, note) {
  sheet.getRange(rowNumber, col.status + 1).setValue(status);
  // Avoid spreadsheet-formula interpretation and never store credentials.
  sheet.getRange(rowNumber, col.error + 1)
    .setValue(text_(note).slice(0, 450));
}

function findExistingUser_(email) {
  try {
    return AdminDirectory.Users.get(email);
  } catch (err) {
    const message = String(err && err.message ? err.message : err);
    // A 403, 429, 500, etc. is NOT equivalent to a missing user.
    if (/\b404\b|resource not found|user not found/i.test(message)) {
      return null;
    }
    throw err;
  }
}

function generateTempPassword_() {
  return Utilities.getUuid().replace(/-/g, '').slice(0, 20) + 'aA9!';
}

function safeError_(err) {
  return String(err && err.message ? err.message : err)
    .replace(/[\r\n]+/g, ' ').slice(0, 230);
}

function escapeHtml_(value) {
  return String(value).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function getEmailHtml(name, email, password) {
  const n = escapeHtml_(name);
  const e = escapeHtml_(email);
  const p = escapeHtml_(password);
  return `
    <div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#333">
      <p>Hi ${n},</p>
      <p>Welcome to Nuanu! 🎉<br>
      Your account has been successfully created using this email address.</p>
      <p>You can now sign in and start using Nuanu right away.</p>
      <p style="background-color:#f9f9f9;padding:15px;border-radius:5px">
        <strong>Email:</strong> ${e}<br>
        <strong>Temp Password:</strong> ${p}
      </p>
      <p>Login: <a href="https://accounts.google.com">https://accounts.google.com</a></p>
      <p>If you did not create this account, please contact us immediately at
        <a href="mailto:admin@nuanu.com">admin@nuanu.com</a> so we can secure your information.</p>
      <p>If you have any questions or need help getting started, our team is always happy to assist.</p>
      <p>Best regards,<br><strong>Nuanu IT Support</strong><br>
      Nuanu<br><a href="https://nuanu.com">https://nuanu.com</a></p>
    </div>`;
}

function getEmailText(name, email, password) {
  return [
    'Hi ' + name + ',',
    '',
    'Welcome to Nuanu! 🎉',
    'Your account has been successfully created using this email address.',
    '',
    'You can now sign in and start using Nuanu right away.',
    '',
    'Email: ' + email,
    'Temp Password: ' + password,
    '',
    'Login: https://accounts.google.com',
    '',
    'If you did not create this account, please contact us immediately at admin@nuanu.com so we can secure your information.',
    '',
    'If you have any questions or need help getting started, our team is always happy to assist.',
    '',
    'Best regards,',
    'Nuanu IT Support',
    'Nuanu',
    'https://nuanu.com'
  ].join('\n');
}

function testMailDelivery() {
  MailApp.sendEmail({
    to: 'admin@nuanu.com',
    subject: '[Nuanu Provisioning] Delivery Test',
    body: 'Test email from Nuanu Apps Script. No credentials included.'
  });
  console.log('Mail request accepted. Check inbox: admin@nuanu.com');
}

function checkProvisioningTrigger() {
  const triggers = ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'processRequests');
  console.log('processRequests triggers: ' + triggers.length);
  triggers.forEach(t => console.log('Event type: ' + t.getEventType()));
}
