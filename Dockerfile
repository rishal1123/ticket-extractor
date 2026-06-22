FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright system dependencies for Chromium + curl for healthcheck +
# xvfb (virtual display so the real Chrome can run HEADED — required to pass
# Cloudflare on Dhiraagu; headless Chrome gets challenged).
# Uses install-deps (apt packages) separately from browser download for reliability
RUN playwright install-deps chromium && \
    apt-get install -y --no-install-recommends curl xvfb && \
    rm -rf /var/lib/apt/lists/*

# Download Playwright Chromium (used by Ooredoo/ROL/Medianet) AND the real Google
# Chrome channel (Dhiraagu uses real Chrome headed to bypass Cloudflare).
RUN playwright install chromium && \
    playwright install --with-deps chrome

# Copy application code
COPY . .

# Create data directories for persistent volumes
RUN mkdir -p /app/data /app/data/browser_sessions

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Expose port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run entrypoint (DB check + app start)
CMD ["/app/entrypoint.sh"]
