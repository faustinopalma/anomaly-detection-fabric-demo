FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY src/requirements.txt .
RUN pip install -r requirements.txt

COPY src/ .

# Defaults — override per-deployment via Container Apps env vars
ENV SIM_MACHINES=5 \
    SIM_RATE=1 \
    SIM_ANOMALY_PROB=0.0005 \
    SIM_BATCH_SIZE=200

CMD ["python", "-u", "cloud_runner.py"]
