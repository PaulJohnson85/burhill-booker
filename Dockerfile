FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser
RUN playwright install chromium

# Copy application code
COPY . .

# Data directory for SQLite and open play JSONs
RUN mkdir -p open_play_data

ENV HEADLESS=1
ENV TZ=Europe/London

EXPOSE $PORT

CMD ["python3", "app.py"]
