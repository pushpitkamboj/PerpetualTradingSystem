FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY app/services/order_book_service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/packages ./packages
COPY app/services/order_book_service .

CMD ["faststream", "run", "app:app"]
