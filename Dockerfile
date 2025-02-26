FROM python:3.9-slim

# Install Chrome and its dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gnupg2 \
    apt-transport-https \
    ca-certificates \
    curl \
    unzip \
    xvfb \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | tee /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install matching Chrome for Testing and ChromeDriver (version 133)
RUN wget -q "https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/133.0.6943.141/linux64/chrome-linux64.zip" \
    && wget -q "https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/133.0.6943.141/linux64/chromedriver-linux64.zip" \
    && unzip chrome-linux64.zip \
    && unzip chromedriver-linux64.zip \
    && mv chrome-linux64/chrome /usr/local/bin/ \
    && mv chromedriver-linux64/chromedriver /usr/local/bin/ \
    && chmod +x /usr/local/bin/chrome \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf chrome-linux64.zip chrome-linux64 chromedriver-linux64.zip chromedriver-linux64

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application file
COPY ["linkchecker PUBLIC.py", "./linkchecker PUBLIC.py"]

# Set Chrome binary path and display
ENV CHROME_BIN=/usr/local/bin/chrome
ENV DISPLAY=:99

# Start the worker
CMD ["python", "linkchecker PUBLIC.py"]