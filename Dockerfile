FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright system dependencies for Chromium + curl for healthcheck +
# xvfb (virtual display so the real Chrome can run HEADED — required to pass
# Cloudflare on Dhiraagu; headless Chrome gets challenged) + x11vnc/novnc/websockify
# so an operator can watch and DRIVE that headed Chrome from a browser to solve a
# Cloudflare challenge by hand when the automatic bypass can't (Turnstile/CAPTCHA).
# Uses install-deps (apt packages) separately from browser download for reliability
RUN playwright install-deps chromium && \
    apt-get install -y --no-install-recommends \
        curl xvfb xauth tzdata x11vnc novnc websockify fluxbox socat && \
    rm -rf /var/lib/apt/lists/*

# Default container timezone to Maldives (UTC+5). Overridable via the TZ env var.
ENV TZ=Indian/Maldives
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Download Playwright Chromium (used by Ooredoo/ROL/Medianet) AND the real Google
# Chrome channel (Dhiraagu uses real Chrome headed to bypass Cloudflare).
RUN playwright install chromium && \
    playwright install --with-deps chrome

# Copy application code
COPY . .

# Create data directories for persistent volumes
RUN mkdir -p /app/data /app/data/browser_sessions

# Normalize line endings (strip CR) then make entrypoints executable. Defensive:
# if the build context arrived with CRLF (e.g. a Windows checkout or a Portainer
# stack built from a Windows-edited repo), a bare CRLF entrypoint fails at startup
# with "bad interpreter: /bin/bash^M". This guarantees LF inside the image.
RUN sed -i 's/\r$//' /app/entrypoint.sh /app/browser-entrypoint.sh && \
    chmod +x /app/entrypoint.sh /app/browser-entrypoint.sh

# Expose port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run entrypoint (DB check + app start)
CMD ["/app/entrypoint.sh"]
