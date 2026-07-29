# Python 3.11 Slim Image (Light and Fast)
FROM python:3.11-slim

# Work Directory
WORKDIR /app

# Environment Variables
ENV PYTHONUNBUFFERED=1

# Copy requirements.txt
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# 👇 YAHAN CHANGE KIYA HAI: Sabhi .py files aur welcome.png copy karne ke liye
COPY . .

# Port 8000 expose karo (Render Web Service ke liye)
EXPOSE 8000

# Bot Run karo
CMD ["python", "bot.py"]
