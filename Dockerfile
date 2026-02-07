# 1. Python'un hafif bir sürümünü kullan
FROM python:3.9-slim

# 2. Çalışma klasörünü ayarla
WORKDIR /app

# 3. Sistem güncellemelerini yap ve FFmpeg'i kur (Linux versiyonu)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# 4. Gerekli Python kütüphanelerini yükle
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Bütün proje dosyalarını kopyala
COPY . .

# 6. İndirilenler klasörünü oluştur (Garanti olsun)
RUN mkdir -p indirilenler

# 7. Uygulamayı başlat (Render'ın verdiği portu dinle)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]