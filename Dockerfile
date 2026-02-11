FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright system dependencies for Chromium + curl for healthcheck
# Uses install-deps (apt packages) separately from browser download for reliability
RUN playwright install-deps chromium && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Download Playwright Chromium browser binary (no apt needed)
RUN playwright install chromium

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
