FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LISTEN_HOST=0.0.0.0 PORT=3000 DATA_DIR=/app/data \
    PUBLIC_WORKER_URL=https://rakcrmphelper.bothost.tech
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY helper ./helper
COPY runtime ./runtime
COPY deploy ./deploy
EXPOSE 3000
CMD ["python", "main.py"]
