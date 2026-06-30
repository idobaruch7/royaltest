FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ROYALTEST_HOST=0.0.0.0 \
    ROYALTEST_PORT=5000 \
    ROYALTEST_DEBUG=0 \
    ROYALTEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/royaltest

WORKDIR /app

RUN groupadd --system royaltest && \
    useradd --system --gid royaltest --create-home --home-dir /home/royaltest royaltest && \
    mkdir -p /data && \
    chown royaltest:royaltest /data

COPY requirements.txt ./

RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

COPY public ./public
COPY server ./server

USER royaltest

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"ROYALTEST_PORT\", \"5000\")}/', timeout=3)"

CMD ["gunicorn", "--no-control-socket", "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "--workers", "1", "--worker-tmp-dir", "/dev/shm", "--bind", "0.0.0.0:5000", "--chdir", "/app/server", "app:app"]
