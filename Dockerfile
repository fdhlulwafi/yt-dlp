FROM python:3.11-slim

# Install system dependencies required for yt-dlp + ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Upgrade pip and install yt-dlp (ALWAYS latest)
RUN pip install --no-cache-dir --upgrade pip yt-dlp fastapi uvicorn

# Create app directory
WORKDIR /app

# Create downloads folder with correct permissions
RUN mkdir -p /app/downloads

# Copy your app code
COPY yt_api.py /app/yt_api.py

# Expose the port (Dokploy ignores but good practice)
EXPOSE 8000

# Run FastAPI using Uvicorn
CMD ["uvicorn", "yt_api:app", "--host", "0.0.0.0", "--port", "8000"]
