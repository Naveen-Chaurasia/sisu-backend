FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py apiv2.py api_emission_iq.py config.py mexico_full_input.csv uganda_full_input.csv start.py ./
COPY policies/ ./policies/
COPY mines4/ ./mines4/

CMD ["python", "start.py"]
