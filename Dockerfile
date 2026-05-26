FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py apiv2.py config.py mexico_full_input.csv start.py ./
COPY policies/ ./policies/

CMD ["python", "start.py"]
