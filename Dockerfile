# Use Microsoft's official image with Python and Chromium system dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

WORKDIR /app

# Install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose port 10000 (Render's default web service port)
EXPOSE 10000

# Fire up uvicorn server — bind to Render's injected $PORT, falling back to 10000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
