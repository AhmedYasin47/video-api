from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
import yt_dlp
import os
import uuid

app = FastAPI()

DOWNLOAD_DIR = "indirilenler"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

def dosyayi_sil(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except:
        pass

@app.get("/indir")
async def video_indir(url: str, background_tasks: BackgroundTasks):
    dosya_adi = str(uuid.uuid4())
    dosya_yolu = os.path.join(DOWNLOAD_DIR, dosya_adi)
    
    # 1. En Basit Ayarlar (Dönüştürme YOK, Sadece İndir)
    ydl_opts = {
        'format': 'best[ext=mp4]/best', # MP4 varsa al, yoksa en iyisini al
        'outtmpl': f'{dosya_yolu}.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36',
        }
    }

    try:
        # İndirmeyi Dene
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # 2. Hangi uzantıyla indiğini bul (mp4 mü, webm mi, mkv mi?)
        indirilen_dosya = None
        for dosya in os.listdir(DOWNLOAD_DIR):
            if dosya.startswith(dosya_adi):
                indirilen_dosya = os.path.join(DOWNLOAD_DIR, dosya)
                break
        
        # 3. Dosya Kontrolü (SİGORTA)
        if not indirilen_dosya or not os.path.exists(indirilen_dosya):
            return {"hata": "Dosya oluşturulamadı."}
        
        dosya_boyutu = os.path.getsize(indirilen_dosya)
        # Eğer dosya 100KB'dan küçükse, bu video değil hata mesajıdır!
        if dosya_boyutu < 100 * 1024: 
            os.remove(indirilen_dosya)
            return {"hata": "YouTube erişimi engelledi (Boş dosya)."}

        # 4. Dosyayı Gönder
        background_tasks.add_task(dosyayi_sil, indirilen_dosya)
        return FileResponse(indirilen_dosya, media_type='video/mp4', filename=f"video_{dosya_adi}.mp4")

    except Exception as e:
        return {"hata": f"Sunucu Hatası: {str(e)}"}