FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium with system dependencies + curl for healthcheck
# Combined into single RUN to share apt cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

# Copy application code
COPY . .

# Create data directories for persistent volumes
RUN mkdir -p /app/data /app/data/browser_sessions

# Expose port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["python", "app.py"]
