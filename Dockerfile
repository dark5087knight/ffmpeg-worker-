# Use a lightweight official Python base image
FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr and generating .pyc bytecode
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKER_TEMP_DIR=/tmp/video-jobs

# 1. Install FFmpeg, curl, and tini (init system for PID 1 signal forwarding and zombie reaping)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        tini && \
    rm -rf /var/lib/apt/lists/*

# 2. Create non-root system user and group for security
RUN groupadd -g 10001 worker && \
    useradd -u 10001 -g worker -m -s /bin/bash worker

# Set working directory inside the container
WORKDIR /app

# Step 1: Copy requirements and install dependencies (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 2: Copy the rest of the worker files
COPY . .

# Step 3: Create temp directories and grant permissions to non-root user
RUN mkdir -p /tmp/video-jobs /app/output && \
    chown -R worker:worker /app /tmp/video-jobs

# Step 4: Run as non-root user
USER worker

# Step 5: Use tini as PID 1 entrypoint for graceful shutdown and process reaping
ENTRYPOINT ["/usr/bin/tini", "--", "python", "worker.py"]

# Default execution starts the SQS consumer loop
CMD []
