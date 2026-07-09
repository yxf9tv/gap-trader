FROM python:3.11-slim

WORKDIR /app

# PMXT self-custody needs Node.js for its local server
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pmxt-core

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
