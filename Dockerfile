# Python 3.11 Slim Image (हल्की और तेज़)
FROM python:3.11-slim

# Work Directory
WORKDIR /app

# Environment Variables (Render पर Set कर देंगे, यहाँ डिफॉल्ट)
ENV PYTHONUNBUFFERED=1

# अपनी requirements.txt कॉपी करें
COPY requirements.txt .

# Python पैकेजेज़ इंस्टॉल करें
RUN pip install --no-cache-dir -r requirements.txt

# पूरा Bot Code कॉपी करें (सिर्फ bot.py ही काफी है, लेकिन सारी .py फाइलें)
COPY bot.py .

# Health check (Render के लिए Optional, लेकिन अच्छा है)
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8000/healthcheck')" || exit 1

# Port 8000 खोलें (Render Web Service के लिए)
EXPOSE 8000

# Bot चलाएं
CMD ["python", "bot.py"]
