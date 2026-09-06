# Social Saver API

Flutter uygulamanin arka ucu. Link geliyor -> `yt-dlp` ile gercek video adresi
cozuluyor -> baytlar telefona aktariliyor. Sunucuda dosya birikmiyor.

## Nasil calisiyor

```
Telefon --GET /indir?url=...--> API --yt-dlp--> Instagram/TikTok/X
                                  <--mp4 link--
        <====== mp4 stream ======= API <===== CDN
```

Iki yol var:

1. **Proxy stream (normal yol):** yt-dlp'den tek parcali mp4 linki cikarsa,
   sunucu o linki acip baytlari dogrudan telefona aktarir. Diske hic yazmaz,
   RAM kullanmaz, hizlidir. TikTok ve Instagram'da neredeyse hep bu calisir.
2. **Diske indirme (yedek yol):** Video sadece HLS (`.m3u8`) olarak geliyorsa
   yt-dlp gecici klasore indirir, ffmpeg birlestirir, dosya gonderildikten
   sonra klasor silinir. X/Twitter'da bazen bu yola dusuyor. **ffmpeg gerekli**,
   o yuzden Dockerfile ile deploy et.

## Endpointler

| Endpoint | Ne yapar |
|---|---|
| `GET /saglik` | Health check. Render buraya bakiyor. |
| `GET /bilgi?url=...` | Baslik, sure, thumbnail, boyut. Indirmeden onizleme icin. |
| `GET /indir?url=...` | mp4 stream eder. |

Hata durumunda JSON doner:

```json
{ "basarili": false, "detail": "Icerik ozel, silinmis veya bolgende engelli." }
```

Kodlar: `400` bozuk URL, `403` desteklenmeyen site, `413` cok buyuk,
`422` cozumlenemedi, `429` rate limit, `502` CDN erisilemedi.

## Render'a deploy

1. Bu klasoru bir GitHub reposuna at.
2. Render -> New -> Web Service -> repoyu sec.
3. **Runtime: Docker** sec (Python secme, ffmpeg gelmez).
4. Plan: Free. Health check path: `/saglik`.
5. Deploy.

`render.yaml` zaten hazir, "Blueprint" olarak da baglayabilirsin.

### Ortam degiskenleri

| Degisken | Varsayilan | Aciklama |
|---|---|---|
| `MAKS_BOYUT_MB` | 300 | Bundan buyuk videoyu reddeder |
| `DAKIKA_LIMITI` | 12 | IP basina dakikadaki istek |
| `ES_ZAMANLI` | 3 | Ayni anda kac cozumleme |
| `COOKIE_DOSYASI` | (bos) | Netscape formatinda `cookies.txt` yolu |

## Onemli: Instagram sorunu

Instagram veri merkezi IP'lerini agresif engelliyor, Render de veri merkezi.
Ayrica cogu gonderi icin artik giris zorunlu. Sonuc: **Instagram linklerinin
bir kismi cookie olmadan hic calismaz**, `422 giris gerekiyor` donersin.

Cozum: tarayicidan (yedek bir hesapla) Netscape formatinda `cookies.txt`
export edip repoya koy ve `COOKIE_DOSYASI=/app/cookies.txt` ver. Cookie'yi
repoya koymak yerine Render'in Secret Files ozelligini kullan — ust ust
kullanilirsa o hesap askiya alinabilir, riski bilerek yap.

TikTok ve X cookie'siz genelde sorunsuz calisiyor.

## Bakim

`yt-dlp` requirements'ta **pinlenmedi**, bilerek. Platformlar sik sik yapi
degistiriyor ve extractor'lar bozuluyor; ayda bir "Clear build cache & deploy"
yaparsan en guncel surumu ceker. Bir platform aniden calismayi birakirsa ilk
yapacagin sey bu.

## Yerelde calistirma

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
curl "http://localhost:8000/bilgi?url=https://www.tiktok.com/@x/video/123"
```

## Yasal

Indirilen icerigin telifi ve platform kullanim sartlari kullanicinin
sorumlulugunda — uygulamadaki uyari metnini kaldirma. Instagram/TikTok/X
kullanim sartlari otomatik indirmeye izin vermiyor; store'a cikaracaksan
ret yiyebilecegini hesaba kat.
