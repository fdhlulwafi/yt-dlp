FROM python:3.11-alpine AS builder

# Install build dependencies and ffmpeg in one layer
RUN apk add --no-cache \
    ffmpeg \
    gcc \
    musl-dev \
    python3-dev \
    libffi-dev

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# Final stage
FROM python:3.11-alpine

# Install only runtime dependencies
RUN apk add --no-cache \
    ffmpeg \
    curl \
    ca-certificates

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Create app directory and downloads folder
WORKDIR /app
RUN mkdir -p /app/downloads && \
    chmod 755 /app/downloads

# Copy application code
COPY yt_api.py /app/yt_api.py

# Expose port
EXPOSE 8000

# Health check for container orchestration
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run with optimized workers for production
CMD ["uvicorn", "yt_api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "75"]
