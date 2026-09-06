"""
Social Saver API
----------------
Flutter uygulamasinin kullandigi backend.

Endpointler:
  GET /saglik          -> health check (Render icin)
  GET /bilgi?url=...   -> video metadata (baslik, sure, thumbnail, boyut)
  GET /indir?url=...   -> videoyu mp4 olarak stream eder
"""

import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

# --------------------------------------------------------------------------
# AYARLAR
# --------------------------------------------------------------------------

SURUM = "2.0.0"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)

# Ses + goruntu tek dosyada (progressive) olani tercih et. Boylece ffmpeg
# birlestirmesi gerekmez ve dogrudan proxy-stream yapabiliriz.
FORMAT_SECIMI = (
    "best[ext=mp4][vcodec!=none][acodec!=none]/"
    "best[vcodec!=none][acodec!=none]/"
    "bv*+ba/best"
)

IZINLI_HOSTLAR = {
    "instagram.com", "instagr.am", "ddinstagram.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com",
    "twitter.com", "x.com", "mobile.twitter.com", "fxtwitter.com",
    "facebook.com", "fb.watch",
    "reddit.com", "redd.it", "v.redd.it",
    "pinterest.com", "pin.it",
}

YASAKLI_HOSTLAR = {"youtube.com", "youtu.be", "ytimg.com", "googlevideo.com"}

MAKS_BOYUT = int(os.getenv("MAKS_BOYUT_MB", "300")) * 1024 * 1024
DAKIKA_LIMITI = int(os.getenv("DAKIKA_LIMITI", "12"))
ES_ZAMANLI_LIMIT = int(os.getenv("ES_ZAMANLI", "3"))

# Instagram / X icin opsiyonel Netscape formatinda cookies.txt yolu
COOKIE_DOSYASI = os.getenv("COOKIE_DOSYASI", "").strip()

app = FastAPI(title="Social Saver API", version=SURUM)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_cozumleme_kilidi = asyncio.Semaphore(ES_ZAMANLI_LIMIT)
_istek_gecmisi: dict[str, list[float]] = {}


# --------------------------------------------------------------------------
# YARDIMCILAR
# --------------------------------------------------------------------------

def _kok_host(host: str) -> str:
    """www.instagram.com -> instagram.com"""
    host = host.split(":")[0].lower().strip(".")
    parcalar = host.split(".")
    if len(parcalar) > 2:
        # v.redd.it, vm.tiktok.com gibi anlamli subdomainleri de birakiyoruz
        return host if host in IZINLI_HOSTLAR else ".".join(parcalar[-2:])
    return host


def _url_dogrula(url: str) -> None:
    try:
        parcali = urlparse(url)
    except Exception:
        raise HTTPException(400, "Gecersiz URL")

    if parcali.scheme not in ("http", "https") or not parcali.netloc:
        raise HTTPException(400, "URL http:// veya https:// ile baslamali")

    host = _kok_host(parcali.netloc)

    if host in YASAKLI_HOSTLAR:
        raise HTTPException(403, "YouTube desteklenmiyor.")

    if host not in IZINLI_HOSTLAR:
        raise HTTPException(403, f"Bu site desteklenmiyor: {host}")


def _limit_kontrol(request: Request) -> None:
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else "bilinmiyor"

    simdi = time.time()
    kayitlar = [t for t in _istek_gecmisi.get(ip, []) if simdi - t < 60]
    if len(kayitlar) >= DAKIKA_LIMITI:
        raise HTTPException(429, "Cok fazla istek gonderdin, biraz bekle.")
    kayitlar.append(simdi)
    _istek_gecmisi[ip] = kayitlar

    if len(_istek_gecmisi) > 5000:  # bellek sismesin
        _istek_gecmisi.clear()


def _ydl_ayarlari(ekstra: dict | None = None) -> dict:
    ayarlar = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "ignoreconfig": True,
        "socket_timeout": 20,
        "retries": 2,
        "format": FORMAT_SECIMI,
        "http_headers": {"User-Agent": USER_AGENT},
    }
    if COOKIE_DOSYASI and os.path.exists(COOKIE_DOSYASI):
        ayarlar["cookiefile"] = COOKIE_DOSYASI
    if ekstra:
        ayarlar.update(ekstra)
    return ayarlar


def _bilgi_cek(url: str) -> dict:
    """Bloklayan yt-dlp cagrisi. Her zaman thread icinde calistirilir."""
    with YoutubeDL(_ydl_ayarlari({"skip_download": True})) as ydl:
        bilgi = ydl.extract_info(url, download=False)

    if bilgi is None:
        raise ExtractorError("Bos yanit")

    # Carousel / thread gibi durumlarda ilk videoyu al
    if bilgi.get("_type") == "playlist":
        girisler = [g for g in (bilgi.get("entries") or []) if g]
        if not girisler:
            raise ExtractorError("Bu linkte indirilebilir video yok")
        bilgi = girisler[0]

    return bilgi


def _dogrudan_link(bilgi: dict) -> tuple[str | None, dict]:
    """
    Tek parcali (progressive) http mp4 linki bulmaya calisir.
    Bulursa (link, gerekli_headerlar) doner, bulamazsa (None, {}).
    """
    def uygun(protokol: str | None) -> bool:
        p = (protokol or "").lower()
        return p.startswith("http") and "m3u8" not in p and "dash" not in p

    if bilgi.get("url") and uygun(bilgi.get("protocol")):
        return bilgi["url"], dict(bilgi.get("http_headers") or {})

    # yt-dlp formatlari kotuden iyiye siralar -> tersten gez
    for fmt in reversed(bilgi.get("formats") or []):
        if (
            fmt.get("url")
            and fmt.get("vcodec") != "none"
            and fmt.get("acodec") != "none"
            and uygun(fmt.get("protocol"))
        ):
            return fmt["url"], dict(fmt.get("http_headers") or {})

    return None, {}


def _dosya_adi(bilgi: dict) -> str:
    ham = (bilgi.get("title") or "video").strip()
    # Content-Disposition ASCII disi karakterlerde patlayabiliyor
    temiz = re.sub(r"[^A-Za-z0-9._ -]", "", ham).strip()
    temiz = re.sub(r"\s+", "_", temiz)[:60]
    return f"{temiz or 'video'}.mp4"


def _tahmini_boyut(bilgi: dict) -> int | None:
    for anahtar in ("filesize", "filesize_approx"):
        if isinstance(bilgi.get(anahtar), int):
            return bilgi[anahtar]
    return None


def _hata_cevir(mesaj: str) -> str:
    m = mesaj.lower()
    if "login" in m or "cookie" in m or "authentication" in m:
        return "Bu icerik icin giris gerekiyor. (Sunucuda cookie tanimlanmali)"
    if "rate" in m and "limit" in m:
        return "Platform gecici olarak istekleri engelledi, birazdan tekrar dene."
    if "private" in m or "unavailable" in m or "not available" in m:
        return "Icerik ozel, silinmis veya bolgende engelli."
    if "404" in m or "not found" in m:
        return "Link bulunamadi."
    if "unsupported url" in m:
        return "Bu link tipi desteklenmiyor."
    if "no video" in m:
        return "Bu gonderide video yok."
    return "Video cozumlenemedi, linki kontrol et."


def _diske_indir(url: str, klasor: str) -> Path:
    """HLS/DASH gibi stream-edilemeyen durumlar icin yedek yol (ffmpeg gerekir)."""
    sablon = str(Path(klasor) / "video.%(ext)s")
    ayarlar = _ydl_ayarlari({
        "outtmpl": sablon,
        "merge_output_format": "mp4",
        "max_filesize": MAKS_BOYUT,
    })
    with YoutubeDL(ayarlar) as ydl:
        ydl.download([url])

    for yol in sorted(Path(klasor).iterdir()):
        if yol.is_file() and yol.stat().st_size > 0:
            return yol
    raise ExtractorError("Dosya olusturulamadi")


# --------------------------------------------------------------------------
# ENDPOINTLER
# --------------------------------------------------------------------------

@app.get("/")
@app.get("/saglik")
async def saglik():
    return {"durum": "ayakta", "surum": SURUM, "cookie": bool(COOKIE_DOSYASI)}


@app.get("/bilgi")
async def bilgi(request: Request, url: str = Query(..., min_length=8)):
    """Indirmeden once onizleme icin. Uygulamada thumbnail/baslik gostermek icin kullan."""
    _url_dogrula(url)
    _limit_kontrol(request)

    async with _cozumleme_kilidi:
        try:
            veri = await asyncio.to_thread(_bilgi_cek, url)
        except (DownloadError, ExtractorError) as hata:
            raise HTTPException(422, _hata_cevir(str(hata)))
        except Exception:
            raise HTTPException(500, "Sunucu hatasi")

    return {
        "baslik": veri.get("title"),
        "yukleyen": veri.get("uploader") or veri.get("channel"),
        "sure": veri.get("duration"),
        "thumbnail": veri.get("thumbnail"),
        "boyut": _tahmini_boyut(veri),
        "kaynak": veri.get("extractor_key"),
    }


@app.get("/indir")
async def indir(request: Request, url: str = Query(..., min_length=8)):
    _url_dogrula(url)
    _limit_kontrol(request)

    async with _cozumleme_kilidi:
        try:
            veri = await asyncio.to_thread(_bilgi_cek, url)
        except (DownloadError, ExtractorError) as hata:
            raise HTTPException(422, _hata_cevir(str(hata)))
        except Exception:
            raise HTTPException(500, "Sunucu hatasi")

    boyut = _tahmini_boyut(veri)
    if boyut and boyut > MAKS_BOYUT:
        raise HTTPException(413, f"Video cok buyuk ({boyut // 1048576} MB)")

    ad = _dosya_adi(veri)
    link, ust_headerlar = _dogrudan_link(veri)

    if link:
        return await _proxy_stream(link, ust_headerlar, ad)

    # Yedek yol: HLS vb. -> once diske indir, sonra gonder
    klasor = tempfile.mkdtemp(prefix="ss_")
    try:
        async with _cozumleme_kilidi:
            yol = await asyncio.to_thread(_diske_indir, url, klasor)
    except Exception as hata:
        shutil.rmtree(klasor, ignore_errors=True)
        raise HTTPException(422, _hata_cevir(str(hata)))

    return FileResponse(
        path=str(yol),
        media_type="video/mp4",
        filename=ad,
        background=BackgroundTask(shutil.rmtree, klasor, ignore_errors=True),
    )


async def _proxy_stream(link: str, ust_headerlar: dict, ad: str) -> StreamingResponse:
    """CDN'den gelen baytlari dogrudan telefona aktarir. Diske hic yazmaz."""
    ust_headerlar.setdefault("User-Agent", USER_AGENT)
    # httpx varsayilan olarak gzip ister ve icerigi acar; bu durumda ust
    # sunucunun Content-Length'i gercek boyutu vermez. Sikistirmayi kapatiyoruz.
    ust_headerlar["Accept-Encoding"] = "identity"

    istemci = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0),
    )

    try:
        istek = istemci.build_request("GET", link, headers=ust_headerlar)
        yanit = await istemci.send(istek, stream=True)
    except Exception:
        await istemci.aclose()
        raise HTTPException(502, "Video sunucusuna ulasilamadi")

    if yanit.status_code >= 400:
        kod = yanit.status_code
        await yanit.aclose()
        await istemci.aclose()
        raise HTTPException(502, f"Video sunucusu {kod} dondu, link eskimis olabilir")

    async def govde():
        try:
            async for parca in yanit.aiter_bytes(64 * 1024):
                yield parca
        finally:
            await yanit.aclose()
            await istemci.aclose()

    basliklar = {
        "Content-Disposition": f'attachment; filename="{ad}"',
        "Cache-Control": "no-store",
    }
    kodlama = (yanit.headers.get("content-encoding") or "identity").lower()
    uzunluk = yanit.headers.get("content-length")
    if uzunluk and kodlama == "identity":
        # Flutter tarafinda onReceiveProgress'in calismasi bu satira bagli
        basliklar["Content-Length"] = uzunluk

    return StreamingResponse(govde(), media_type="video/mp4", headers=basliklar)


@app.exception_handler(HTTPException)
async def hata_yakala(request: Request, hata: HTTPException):
    return JSONResponse(
        status_code=hata.status_code,
        content={"basarili": False, "detail": hata.detail},
    )
