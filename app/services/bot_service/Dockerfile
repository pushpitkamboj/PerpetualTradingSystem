FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY app/services/bot_service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/packages ./packages

CMD ["python", "-m", "packages.liquidity_bot"]
