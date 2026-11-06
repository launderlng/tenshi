# Railway uses this Dockerfile (it takes priority over Nixpacks), so the
# Tesseract OCR engine the web stash scanner needs is always installed.
FROM python:3.12-slim

# Tesseract OCR for the stash screenshot scanner
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Same start command as before
CMD ["python", "gang_bot.py"]
