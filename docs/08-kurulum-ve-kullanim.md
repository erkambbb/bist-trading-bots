# 08 · Kurulum ve kullanım

## 1. Kurulum

```bash
git clone https://github.com/erkambbb/bist-trading-bots && cd bist-trading-bots
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[data,live,dev]"
pytest -q                      # 14 test, ~30 sn
```

Sırlar `.env` dosyasında tutulur (repoya girmez):

```bash
TELEGRAM_BOT_TOKEN=...         # emir fişleri ve bildirimler
TELEGRAM_CHAT_ID=...
ANTHROPIC_API_KEY=...          # orta vade: KAP/haber LLM skoru (opsiyonel)
TV_USERNAME=... TV_PASSWORD=...  # günlük bot: TradingView gerçek zamanlı BIST verisi
```

Kişisel ayarlar için `config/<ad>.local.yaml` oluşturun. Bu dosya `.example.yaml` üzerine yazılır ve gitignore'dadır.

## 2. Veri ve backtest

```bash
python scripts/download_data.py          # ~30–60 dk; data/ altına yazar, kaldığı yerden devam eder
python scripts/run_backtest.py           # iki bot, 500.000 TL; reports/ + docs/07-backtest-sonuclari.md
python scripts/run_backtest.py --quick   # küçük ızgara, hızlı kontrol
bisttrader synth data_synth && python scripts/run_backtest.py --data data_synth --quick   # sentetik veriyle deneme
```

| Veri | Kaynak | Not |
|---|---|---|
| BIST günlük (2017+), XU100/XU030 | borsapy (TradingView) | Yedek: yfinance `.IS` |
| BIST gün içi 1s / 15dk / 5dk | borsapy (TradingView), yedek yfinance 1s (730 gün) | TradingView'in verdiği geçmiş derinliği sınırlı |
| Mali tablolar (çeyreklik) | borsapy (İş Yatırım) | Yayın tarihi yasal süre + tamponla varsayılır |
| PPF, politika faizi, TÜFE/ÜFE | borsapy (TEFAS, TCMB) | PPF fiyatı 5 yıl geriye; öncesi faizden türetilir |
| USDTRY, VIX, S&P, ETF'ler, BTC/ETH | yfinance | |

## 3. Günlük Bot

```bash
bisttrader daily-live              # paper: gerçek fiyat akışı, sanal dolum
bisttrader daily-live --live       # gerçek emir (aşağıdaki koşullar sağlanırsa)
```

Gerçek emir için **hepsi** gerekli:
1. `config/daily_bot.local.yaml` içinde `mode: live`, `broker: matriks_iq`, `data.realtime_confirmed: true`
2. TradingView hesabı gerçek zamanlı BIST verisi veriyor (aksi halde veri ~15 dk gecikmeli → bot paper'a döner)
3. Aynı Windows makinede MatriksIQ açık, Midas hesabına giriş yapılmış, **dış emir API lisansı** aktif (TCP 18890)
4. Önce 1 lotla her komut (emir, iptal, pozisyon) MatriksIQ'da doğrulanmış

Zamanlama (Windows Görev Zamanlayıcı veya cron): hafta içi 07:25'te `bisttrader daily-live`. Süreç 17:56'da kendiliğinden biter.

## 4. Orta Vade Bot

```bash
bisttrader midterm-weekly --refresh     # Cuma 19:00: veri güncelle → skor → Telegram emir fişleri
bisttrader midterm-daily --refresh      # her gün 18:30: felaket stopu, T2/T3 tetikleri, KAP olayları
bisttrader midterm-record --sleeve bist --symbol THYAO --side buy --qty 120 --price 301.5 --tranche T1 --target-value 90000
```

Fişler Midas uygulamasında elle girilir (BIST komisyonu %0). Gerçekleşen her emir `midterm-record` ile `data/live/portfolio.json`'a işlenir. Başlangıç nakdi bu dosyada `cash_try` alanıdır.

LLM (opsiyonel): `config/midterm_bot.local.yaml` içinde `llm.enabled: true`. Yön skoru ≤ −2 olan KAP olayları "tez kırılması — ONAY GEREKLİ" fişi üretir. Bot asla kendiliğinden satmaz, son karar kullanıcıdadır.

## 5. Planla uygulamanın farkları (dürüstlük notu)

| Planda | Uygulamada | Neden |
|---|---|---|
| Tarihsel BIST100 bileşenleri | Bugünkü XU100 listesi | Ücretsiz tarihsel liste yok → survivorship yanlılığı, sonuçlar iyimser olabilir |
| VBTS tedbirli hisse filtresi | Backtestte yok, canlıda manuel | Tarihsel VBTS verisi yok |
| CDS rejim filtresi | Yok | Ücretsiz günlük CDS serisi yok |
| 5 dk açılış aralığı (2 yıl) | 2 yıl için 1 saatlik bar; 5/15 dk ancak verinin yettiği kısa dönemde | Ücretsiz gün içi verinin derinliği sınırlı |
| Analist + haber bileşeni | Backtestte kapalı, canlıda opsiyonel | Point-in-time veri yok + LLM look-ahead |
| Yurt dışı tekil hisse seçimi | Backtestte SPY/QQQ + sektör ETF momentumu | Bugünün büyük şirketlerini seçmek survivorship yanlılığı yaratır |
| Bedelli/denetim/döviz pozisyonu cezaları | Backtestte yalnızca sermaye artışı sinyali | Dipnot verisi sadece canlıda LLM ile okunabilir |
| Matriks emir API'si | Yazıldı, birim testli (sahte sunucu) | Gerçek MatriksIQ ile doğrulanmadı → `dry_run` varsayılan |
