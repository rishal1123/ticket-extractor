# ISP Ticket Formatter — web app image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (only what the web app needs at runtime).
COPY webapp.py .
COPY model/ ./model/
COPY services/ ./services/
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 8000

# 2 workers is plenty for this lightweight, CPU-light formatter.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "2", "--timeout", "60", "webapp:app"]