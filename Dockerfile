FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py apiv2.py config.py mexico_full_input.csv ./
COPY policies/ ./policies/

EXPOSE $PORT

CMD uvicorn api:app --host 0.0.0.0 --port $PORT
