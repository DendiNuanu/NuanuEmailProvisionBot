FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
COPY secrets/ /secrets/

CMD ["python", "bot.py"]
