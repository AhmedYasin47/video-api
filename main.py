from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse
import yt_dlp
import os
import uuid

app = FastAPI()

# 1. İndirilenlerin duracağı klasörü ayarla
DOWNLOAD_DIR = "indirilenler"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# 2. İndirme bitince dosyayı temizleyen fonksiyon
def dosyayi_sil(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
            print(f"-> Temizlik yapıldı: {path} silindi.")
    except Exception as e:
        print(f"Silme hatası: {e}")

@app.get("/")
def ana_sayfa():
    return {"durum": "API Yayında", "mesaj": "Render üzerinden çalışıyor. /indir?url=... kullanın"}

@app.get("/indir")
async def video_indir(url: str, background_tasks: BackgroundTasks):
    try:
        # Rastgele dosya ismi oluştur
        dosya_adi = str(uuid.uuid4())
        dosya_yolu = os.path.join(DOWNLOAD_DIR, dosya_adi)
        
        print(f"İndirme İsteği: {url}")

        # yt-dlp Ayarları (Android Dostu Sürüm)
        ydl_opts = {
            # 1. En iyi kaliteyi seç ama MP4 öncelikli olsun
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            
            # 2. Çıktı şablonu
            'outtmpl': f'{dosya_yolu}.%(ext)s',
            
            # 3. İndirme bittikten sonra videoyu MUTLAKA mp4 yap (Post-processor)
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            
            # 4. Terminal kirliliği olmasın
            'quiet': True,
            'no_warnings': True,
            
            # 5. Tarayıcı gibi davran (Bot korumasını geçmek için)
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
            }
        }

        # İndirmeyi Başlat
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # Dosya uzantısını kontrol et (mp4 olarak birleştirdiğimiz için mp4 olacaktır)
            # Ancak garanti olsun diye info'dan veya varsayılan olarak mp4 alıyoruz
            tam_dosya_yolu = f"{dosya_yolu}.mp4"

        # Dosya gerçekten oluştu mu kontrol et
        if not os.path.exists(tam_dosya_yolu):
            return {"hata": True, "mesaj": "Dosya indirilemedi veya bulunamadı."}

        print(f"✅ Başarılı: {tam_dosya_yolu}")

        # Dosyayı kullanıcıya gönder ve gönderim bitince silmesi için göreve ekle
        background_tasks.add_task(dosyayi_sil, tam_dosya_yolu)
        
        return FileResponse(
            path=tam_dosya_yolu, 
            filename=f"video_{dosya_adi}.mp4", 
            media_type='video/mp4'
        )

    except Exception as e:
        print(f"❌ HATA: {str(e)}")
        return {"hata": True, "mesaj": str(e)}