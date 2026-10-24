FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/hotel.db \
    PORT=8000

WORKDIR /app

RUN useradd --system --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app src/ ./src/

USER app

VOLUME ["/data"]
EXPOSE 8000

CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0"]