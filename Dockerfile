# Pinned to the Debian release explicitly (not the floating `python:3.11-slim`
# tag) -- that tag silently re-resolves to whatever Debian codename is current
# whenever the image is rebuilt from scratch (no cached layers), and Playwright
# 1.58.0's `install-deps` has a hardcoded list of OS releases it knows how to
# provision. A base-image codename drift out from under an unchanged Dockerfile
# command is the most plausible explanation for `playwright install-deps
# chromium` suddenly failing with no other change in this file. Bookworm is
# Debian 12, well within 1.58.0's supported range.
FROM python:3.11-slim-bookworm

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright system dependencies for Chromium, then Firefox (Dhiraagu runs
# Firefox headed under Xvfb to bypass Cloudflare — Chrome's renderer crashes in the
# container). Split into separate RUN layers (rather than one chained `&&` command)
# so a build failure's log points at exactly which step broke, instead of hiding it
# inside one opaque combined command.
RUN playwright install-deps chromium
RUN playwright install-deps firefox

# curl for healthcheck + xvfb (virtual display so the real browser can run HEADED)
# + x11vnc/novnc/websockify so an operator can watch and DRIVE that headed browser
# to solve a Cloudflare challenge by hand when the automatic bypass can't (Turnstile/
# CAPTCHA). Must run before `rm -rf /var/lib/apt/lists/*` -- that cleanup wipes the
# package index, so anything needing apt-get after this layer would fail with
# "Unable to locate package". Browser *binaries* are downloaded separately further
# down, without --with-deps, since the deps are already installed above.
RUN apt-get install -y --no-install-recommends \
        curl xvfb xauth tzdata x11vnc novnc websockify fluxbox socat \
        libgl1-mesa-dri libglx-mesa0 libegl1 libgles2 mesa-vulkan-drivers && \
    rm -rf /var/lib/apt/lists/*

# Default container timezone to Maldives (UTC+5). Overridable via the TZ env var.
ENV TZ=Indian/Maldives
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Download the Playwright browser binaries (used by Ooredoo/ROL/Medianet: Chromium;
# Dhiraagu: Firefox). No --with-deps here -- both browsers' system deps were already
# installed above, while the apt package index still existed.
RUN playwright install chromium && \
    playwright install firefox

# Copy application code
COPY . .

# Create data directories for persistent volumes
RUN mkdir -p /app/data /app/data/browser_sessions

# Normalize line endings (strip CR) then make entrypoints executable. Defensive:
# if the build context arrived with CRLF (e.g. a Windows checkout or a Portainer
# stack built from a Windows-edited repo), a bare CRLF entrypoint fails at startup
# with "bad interpreter: /bin/bash^M". This guarantees LF inside the image.
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Expose port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run entrypoint (DB check + app start)
CMD ["/app/entrypoint.sh"]
