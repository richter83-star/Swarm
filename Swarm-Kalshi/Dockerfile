FROM python:3.11-slim

LABEL maintainer="kalshi-swarm"
LABEL description="Kalshi Bot Swarm - Autonomous Trading System"

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Create runtime directories
RUN mkdir -p data logs keys

# Expose dashboard port
EXPOSE 8080

# Default: run the full swarm
CMD ["python", "run_swarm.py"]
