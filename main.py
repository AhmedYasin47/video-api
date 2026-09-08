"""
Social Saver API v3
-------------------
Yenilikler:
  * ?debug=1  -> ham yt-dlp hatasini gosterir (teshis icin)
  * Tum hatalar Render loglarina yaziliyor
  * PROXY destegi (veri merkezi IP engellerini asmak icin)
  * COOKIE_DOSYASI destegi (Instagram / X icin)
  * YOUTUBE_ACIK=1 ile YouTube acilabiliyor (varsayilan kapali)

Endpointler:
  GET /saglik          -> health check + aktif ayarlar
  GET /bilgi?url=...   -> metadata
  GET /indir?url=...   -> mp4 stream
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
import re
import secrets
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

# --------------------------------------------------------------------------
# AYARLAR
# --------------------------------------------------------------------------

SURUM = "3.0.0"
log = logging.getLogger("uvicorn.error")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)

# h264 (avc) her cihazda oynar. h265/bytevc1 bazi Android galerilerinde
# acilmiyor -> kullanici videoyu indirir ama siyah ekran gorur.
# TikTok'ta 1080p sadece h265 olarak veriliyor, o yuzden varsayilan uyumluluk.
# KALITE_ONCELIK=1 dersen codec'e bakmadan en yuksek cozunurlugu alir.
FORMAT_UYUMLU = (
    "best[ext=mp4][vcodec^=h264][acodec!=none][format_id!=download]/"
    "best[ext=mp4][vcodec^=avc][acodec!=none][format_id!=download]/"
    "best[ext=mp4][vcodec!=none][acodec!=none][format_id!=download]/"
    "best[vcodec!=none][acodec!=none]/"
    "bv*+ba/best"
)

FORMAT_KALITELI = (
    "best[ext=mp4][vcodec!=none][acodec!=none][format_id!=download]/"
    "best[vcodec!=none][acodec!=none]/"
    "bv*+ba/best"
)

TEMEL_HOSTLAR = {
    "instagram.com", "instagr.am", "ddinstagram.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com",
    "twitter.com", "x.com", "mobile.twitter.com", "fxtwitter.com",
    "facebook.com", "fb.watch",
    "reddit.com", "redd.it", "v.redd.it",
    "pinterest.com", "pin.it",
}

YOUTUBE_HOSTLARI = {"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}

# --- Ortam degiskenleri ---
YOUTUBE_ACIK = os.getenv("YOUTUBE_ACIK", "0").strip() == "1"
PROXY = os.getenv("PROXY", "").strip()
COOKIE_DOSYASI = os.getenv("COOKIE_DOSYASI", "").strip()
DEBUG_ACIK = os.getenv("DEBUG_ACIK", "1").strip() == "1"
# Bos birakilirsa anahtar kontrolu yapilmaz. Dolduruldugunda istekler
# X-API-Key basligi tasimak zorunda. Mutlak koruma degil, esik yukseltir.
API_ANAHTARI = os.getenv("API_ANAHTARI", "").strip()

# --- KENDINI UYANIK TUTMA ---
# Render ucretsiz plan 15 dk sessizlikten sonra servisi uyutuyor ve
# uyanmasi ~50 sn suruyor. Asagidaki gorev servisin kendi public
# adresine duzenli istek atarak uykuyu engeller.
# DIKKAT: uyanik gecen her saat 750 saatlik aylik kotadan duser.
# 7/24 acik = ~744 saat (kota 750). O yuzden varsayilan olarak sadece
# aktif saatlerde calisir.
KENDINI_UYANIK_TUT = os.getenv("KENDINI_UYANIK_TUT", "0").strip() == "1"
PING_ARALIGI = int(os.getenv("PING_ARALIGI_SN", "600"))  # 10 dakika
UYANIK_BASLANGIC = int(os.getenv("UYANIK_BASLANGIC", "7"))   # yerel saat
UYANIK_BITIS = int(os.getenv("UYANIK_BITIS", "1"))           # ertesi gun 01:00
SAAT_FARKI = int(os.getenv("SAAT_FARKI", "3"))               # UTC+3 (Turkiye)
# Render bu degiskeni otomatik saglar: https://<servis>.onrender.com
KENDI_ADRESIM = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
YT_PLAYER_CLIENT = os.getenv("YT_PLAYER_CLIENT", "").strip()
IMPERSONATE = os.getenv("IMPERSONATE", "").strip()  # ornek: chrome / safari
# Varsayilan: en yuksek cozunurluk. Videolar telefonda acilmazsa
# SADECE_H264=1 yapip uyumlu moda dus.
SADECE_H264 = os.getenv("SADECE_H264", "0").strip() == "1"

MAKS_BOYUT = int(os.getenv("MAKS_BOYUT_MB", "300")) * 1024 * 1024
DAKIKA_LIMITI = int(os.getenv("DAKIKA_LIMITI", "12"))
ES_ZAMANLI_LIMIT = int(os.getenv("ES_ZAMANLI", "3"))

FORMAT_SECIMI = FORMAT_UYUMLU if SADECE_H264 else FORMAT_KALITELI

IZINLI_HOSTLAR = set(TEMEL_HOSTLAR)
if YOUTUBE_ACIK:
    IZINLI_HOSTLAR |= YOUTUBE_HOSTLARI

def _aktif_saatte_mi() -> bool:
    """Yerel saate gore uyanik penceresinde miyiz?"""
    saat = (datetime.now(timezone.utc).hour + SAAT_FARKI) % 24
    if UYANIK_BASLANGIC == UYANIK_BITIS:
        return True  # 7/24
    if UYANIK_BASLANGIC < UYANIK_BITIS:
        return UYANIK_BASLANGIC <= saat < UYANIK_BITIS
    # Gece yarisini asan pencere (ornek: 07 -> 01)
    return saat >= UYANIK_BASLANGIC or saat < UYANIK_BITIS


async def _kendini_uyandir_dongusu():
    """Kendi public adresine duzenli istek atar.

    Istek Render'in yuk dengeleyicisi uzerinden geri geldigi icin
    'gelen trafik' sayilir ve spin-down sayaci sifirlanir.
    """
    if not KENDI_ADRESIM:
        log.warning("[PING] RENDER_EXTERNAL_URL bos, kendini uyandirma kapali")
        return

    hedef = f"{KENDI_ADRESIM}/saglik"
    log.info("[PING] Kendini uyanik tutma aktif: %s (her %s sn)", hedef, PING_ARALIGI)

    async with httpx.AsyncClient(timeout=30.0) as istemci:
        while True:
            await asyncio.sleep(PING_ARALIGI)
            if not _aktif_saatte_mi():
                continue
            try:
                await istemci.get(hedef)
            except Exception as hata:
                log.warning("[PING] basarisiz: %s", hata)


@asynccontextmanager
async def yasam_dongusu(app: FastAPI):
    gorev = None
    if KENDINI_UYANIK_TUT:
        gorev = asyncio.create_task(_kendini_uyandir_dongusu())
    yield
    if gorev:
        gorev.cancel()


app = FastAPI(title="Social Saver API", version=SURUM, lifespan=yasam_dongusu)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
)

_cozumleme_kilidi = asyncio.Semaphore(ES_ZAMANLI_LIMIT)
_istek_gecmisi: dict[str, list[float]] = {}


# --------------------------------------------------------------------------
# YARDIMCILAR
# --------------------------------------------------------------------------

def _kok_host(host: str) -> str:
    host = host.split(":")[0].lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parcalar = host.split(".")
    if len(parcalar) > 2 and host not in IZINLI_HOSTLAR and host not in YOUTUBE_HOSTLARI:
        return ".".join(parcalar[-2:])
    return host


def _url_dogrula(url: str) -> None:
    try:
        parcali = urlparse(url)
    except Exception:
        raise HTTPException(400, "Gecersiz URL")

    if parcali.scheme not in ("http", "https") or not parcali.netloc:
        raise HTTPException(400, "URL http:// veya https:// ile baslamali")

    host = _kok_host(parcali.netloc)

    if host in YOUTUBE_HOSTLARI and not YOUTUBE_ACIK:
        raise HTTPException(403, "YouTube desteklenmiyor.")

    if host not in IZINLI_HOSTLAR:
        raise HTTPException(403, f"Bu site desteklenmiyor: {host}")


def _anahtar_kontrol(request: Request) -> None:
    if not API_ANAHTARI:
        return
    gelen = request.headers.get("x-api-key", "")
    if not secrets.compare_digest(gelen, API_ANAHTARI):
        raise HTTPException(401, "Gecersiz anahtar")


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

    if len(_istek_gecmisi) > 5000:
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
        # IPv6 araliklari daha sik engelleniyor, IPv4'e zorla
        "source_address": "0.0.0.0",
    }
    # NOT: Burada User-Agent override ETMIYORUZ. TikTok gibi extractor'lar
    # curl_cffi ile TLS parmak izi taklidi yapiyor; elle UA basmak parmak izi
    # ile header'i celiskiye dusurup engellenmeyi kolaylastiriyor.

    if IMPERSONATE:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        ayarlar["impersonate"] = ImpersonateTarget.from_str(IMPERSONATE)

    if PROXY:
        ayarlar["proxy"] = PROXY

    if COOKIE_DOSYASI and os.path.exists(COOKIE_DOSYASI):
        ayarlar["cookiefile"] = COOKIE_DOSYASI

    if YT_PLAYER_CLIENT:
        # Ornek: YT_PLAYER_CLIENT=android_vr,web_safari
        ayarlar["extractor_args"] = {
            "youtube": {
                "player_client": [c.strip() for c in YT_PLAYER_CLIENT.split(",") if c.strip()]
            }
        }

    if ekstra:
        ayarlar.update(ekstra)
    return ayarlar


def _bilgi_cek(url: str) -> tuple[dict, object]:
    """Metadata + cookiejar doner.

    TikTok gibi siteler JS challenge cozup cookie aliyor; CDN o cookie olmadan
    403 doner. O yuzden jar'i disari tasiyip stream isteginde kullaniyoruz.
    """
    with YoutubeDL(_ydl_ayarlari({"skip_download": True})) as ydl:
        bilgi = ydl.extract_info(url, download=False)
        jar = ydl.cookiejar

    if bilgi is None:
        raise ExtractorError("Bos yanit")

    if bilgi.get("_type") == "playlist":
        girisler = [g for g in (bilgi.get("entries") or []) if g]
        if not girisler:
            raise ExtractorError("Bu linkte indirilebilir video yok")
        bilgi = girisler[0]

    return bilgi, jar


def _cookie_basligi(jar, hedef_url: str) -> str:
    """Cookiejar'dan hedef adrese uyan cookie'leri Cookie: basligina cevirir."""
    try:
        istek = urllib.request.Request(hedef_url)
        jar.add_cookie_header(istek)
        return istek.get_header("Cookie", "") or ""
    except Exception:
        return ""


def _dogrudan_link(bilgi: dict) -> tuple[str | None, dict]:
    def uygun(protokol: str | None) -> bool:
        p = (protokol or "").lower()
        return p.startswith("http") and "m3u8" not in p and "dash" not in p

    if bilgi.get("url") and uygun(bilgi.get("protocol")):
        return bilgi["url"], dict(bilgi.get("http_headers") or {})

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
    if "not a bot" in m or "po token" in m or "proof of origin" in m:
        return "Platform sunucuyu bot sandi. Cookie veya proxy gerekiyor."
    if "403" in m or "forbidden" in m:
        return "Platform sunucunun IP adresini engelledi. Proxy gerekiyor."
    if "login" in m or "cookie" in m or "authentication" in m or "log in" in m:
        return "Bu icerik icin giris gerekiyor. (Sunucuda cookie tanimlanmali)"
    if "rate" in m and "limit" in m:
        return "Platform gecici olarak istekleri engelledi, birazdan tekrar dene."
    if "geo" in m and ("block" in m or "restrict" in m):
        return "Bu icerik sunucunun bulundugu bolgede engelli."
    if "private" in m:
        return "Icerik gizli hesapta."
    if "removed" in m or "deleted" in m:
        return "Icerik silinmis."
    if "unavailable" in m or "not available" in m:
        return "Icerik acilamadi (silinmis, gizli veya platform sunucuyu engelledi)."
    if "404" in m or "not found" in m:
        return "Link bulunamadi."
    if "unsupported url" in m:
        return "Bu link tipi desteklenmiyor."
    if "no video" in m:
        return "Bu gonderide video yok."
    return "Video cozumlenemedi, linki kontrol et."


def _hata_firlat(url: str, hata: Exception, debug: bool):
    """Ham hatayi loglar; debug=1 ise kullaniciya da gosterir."""
    ham = str(hata)
    log.warning("[COZUMLEME HATASI] url=%s | %s", url, ham.replace("\n", " ")[:800])

    if debug and DEBUG_ACIK:
        raise HTTPException(422, {
            "mesaj": _hata_cevir(ham),
            "ham_hata": ham[:1500],
            "proxy": bool(PROXY),
            "cookie": bool(COOKIE_DOSYASI and os.path.exists(COOKIE_DOSYASI)),
        })
    raise HTTPException(422, _hata_cevir(ham))


def _diske_indir(url: str, klasor: str) -> Path:
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

def _impersonate_sayisi() -> int:
    """curl_cffi kurulu mu, kac taklit hedefi var? 0 ise TikTok patlar."""
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return len(ydl._get_available_impersonate_targets())
    except Exception:
        return 0


@app.get("/")
@app.get("/saglik")
async def saglik():
    from yt_dlp.version import __version__ as ytdlp_surum
    return {
        "durum": "ayakta",
        "surum": SURUM,
        "yt_dlp": ytdlp_surum,
        "impersonate_hedefleri": _impersonate_sayisi(),
        "sadece_h264": SADECE_H264,
        "youtube_acik": YOUTUBE_ACIK,
        "proxy_var": bool(PROXY),
        "cookie_var": bool(COOKIE_DOSYASI and os.path.exists(COOKIE_DOSYASI)),
        "cookie_yolu": COOKIE_DOSYASI or None,
        "anahtar_zorunlu": bool(API_ANAHTARI),
        "kendini_uyanik_tutma": KENDINI_UYANIK_TUT,
        "su_an_aktif_saatte": _aktif_saatte_mi(),
    }


@app.get("/bilgi")
async def bilgi(
    request: Request,
    url: str = Query(..., min_length=8),
    debug: int = Query(0),
):
    _anahtar_kontrol(request)
    _url_dogrula(url)
    _limit_kontrol(request)

    async with _cozumleme_kilidi:
        try:
            veri, jar = await asyncio.to_thread(_bilgi_cek, url)
        except (DownloadError, ExtractorError) as hata:
            _hata_firlat(url, hata, bool(debug))
        except Exception as hata:
            log.exception("[SUNUCU HATASI] url=%s", url)
            raise HTTPException(500, f"Sunucu hatasi: {type(hata).__name__}")

    link, _ = _dogrudan_link(veri)
    return {
        "baslik": veri.get("title"),
        "yukleyen": veri.get("uploader") or veri.get("channel"),
        "sure": veri.get("duration"),
        "thumbnail": veri.get("thumbnail"),
        "boyut": _tahmini_boyut(veri),
        "kaynak": veri.get("extractor_key"),
        "stream_edilebilir": bool(link),
    }


@app.get("/indir")
async def indir(
    request: Request,
    url: str = Query(..., min_length=8),
    debug: int = Query(0),
):
    _anahtar_kontrol(request)
    _url_dogrula(url)
    _limit_kontrol(request)

    async with _cozumleme_kilidi:
        try:
            veri, jar = await asyncio.to_thread(_bilgi_cek, url)
        except (DownloadError, ExtractorError) as hata:
            _hata_firlat(url, hata, bool(debug))
        except Exception as hata:
            log.exception("[SUNUCU HATASI] url=%s", url)
            raise HTTPException(500, f"Sunucu hatasi: {type(hata).__name__}")

    boyut = _tahmini_boyut(veri)
    if boyut and boyut > MAKS_BOYUT:
        raise HTTPException(413, f"Video cok buyuk ({boyut // 1048576} MB)")

    ad = _dosya_adi(veri)
    link, ust_headerlar = _dogrudan_link(veri)

    if link:
        cerez = _cookie_basligi(jar, link)
        if cerez:
            ust_headerlar.setdefault("Cookie", cerez)
        ust_headerlar.setdefault("Referer", veri.get("webpage_url") or url)
        cevap = await _proxy_stream(link, ust_headerlar, ad)
        if cevap is not None:
            return cevap
        # CDN dogrudan istegi reddetti -> yt-dlp kendi indirsin.
        # Yavas ama yt-dlp curl_cffi ile TLS taklidi yapabildigi icin gecer.
        log.info("[YEDEK YOL] yt-dlp ile indiriliyor: %s", url)

    klasor = tempfile.mkdtemp(prefix="ss_")
    try:
        async with _cozumleme_kilidi:
            yol = await asyncio.to_thread(_diske_indir, url, klasor)
    except Exception as hata:
        shutil.rmtree(klasor, ignore_errors=True)
        _hata_firlat(url, hata, bool(debug))

    return FileResponse(
        path=str(yol),
        media_type="video/mp4",
        filename=ad,
        background=BackgroundTask(shutil.rmtree, klasor, ignore_errors=True),
    )


async def _proxy_stream(link: str, ust_headerlar: dict, ad: str) -> StreamingResponse | None:
    ust_headerlar.setdefault("User-Agent", USER_AGENT)
    # httpx varsayilan gzip ister ve acar; o zaman Content-Length yanlis olur
    ust_headerlar["Accept-Encoding"] = "identity"

    istemci_ayarlari = {
        "follow_redirects": True,
        "timeout": httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0),
    }
    if PROXY:
        istemci_ayarlari["proxy"] = PROXY

    istemci = httpx.AsyncClient(**istemci_ayarlari)

    try:
        istek = istemci.build_request("GET", link, headers=ust_headerlar)
        yanit = await istemci.send(istek, stream=True)
    except Exception as hata:
        await istemci.aclose()
        log.warning("[CDN ULASILAMADI] %s", hata)
        return None  # cagiran taraf yedek yola dusecek

    if yanit.status_code >= 400:
        kod = yanit.status_code
        await yanit.aclose()
        await istemci.aclose()
        log.warning("[CDN %s] stream reddedildi, yt-dlp yoluna dusuluyor", kod)
        return None

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
        # Flutter'daki onReceiveProgress bu satira bagli
        basliklar["Content-Length"] = uzunluk

    return StreamingResponse(govde(), media_type="video/mp4", headers=basliklar)


@app.exception_handler(HTTPException)
async def hata_yakala(request: Request, hata: HTTPException):
    detay = hata.detail
    if isinstance(detay, dict):
        return JSONResponse(status_code=hata.status_code, content={"basarili": False, **detay})
    return JSONResponse(
        status_code=hata.status_code,
        content={"basarili": False, "detail": detay},
    )
