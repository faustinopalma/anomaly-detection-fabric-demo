FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY src/requirements.txt .
RUN pip install -r requirements.txt

COPY src/ .

# Control panel served same-origin by the control API (no Static Web App).
# The webapp/ dir is staged into the build context by deploy.ps1.
COPY webapp/ /app/webapp/

# Defaults — override per-deployment via Container Apps env vars
ENV SIM_MACHINES=3 \
    SIM_RATE=1 \
    SIM_ANOMALY_PROB=0.0005 \
    SIM_BATCH_SIZE=200 \
    SIM_CNC_PROFILE=/app/cnc_profile_M-003.json \
    SIM_WEB_DIR=/app/webapp

CMD ["python", "-u", "cloud_runner.py"]
