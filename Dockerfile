FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user; it owns only data/, the one folder the API
# writes to (the harness persona library). docker-compose.yml persists it.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown app:app /app/data

COPY src ./src
COPY knowledge ./knowledge
COPY scripts ./scripts

USER app

EXPOSE 8000

# 0.0.0.0 is inside the container only; docker-compose.yml publishes the port
# on the host's 127.0.0.1, so nothing outside the server can reach it.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
