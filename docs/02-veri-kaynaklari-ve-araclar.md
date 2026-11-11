# 02 · Veri kaynakları, API'ler ve araçlar

## 1. Emir iletimi (execution)

| Yol | Piyasa | Nasıl | Maliyet | Kısıtlar | Kullanım |
|---|---|---|---|---|---|
| **MatriksIQ dış emir API'si + Midas hesabı** | BIST pay | MatriksIQ yerelde **TCP 18890** portunu dinler. JSON mesajlar `char(11)` ile biter. .NET için `MatriksIQ.ApiClient` NuGet paketi var. Komutlar: `NewOrder(3)`, `CancelOrder(4)`, `EditOrder(5)`. `TransactionType` normal/açığa satış/gün içi açığa satış destekler | %0,01 + BSMV + aylık lisanslar | Windows. MatriksIQ açık ve kuruma login olmalı. Rate limit ⚠ teyit | **Günlük Bot – birincil** |
| MatriksIQ Algo (C#) | BIST pay, VİOP | Strateji platform içinde C# ile yazılır. Backtest ve optimizasyon dahil | IQ Algo lisansı | Platforma bağımlı | Günlük Bot – alternatif |
| AlgoLab (Deniz Yatırım) | BIST, VİOP | REST + WebSocket | Kurum komisyonu | Açık kaynak örneklere göre **5 saniyede 1 istek** limiti ve her girişte SMS onayı | Yedek |
| Diğer API'li kurumlar (Gedik, İş Yatırım vb.) | BIST, VİOP | REST/WebSocket/FIX | Komisyon pazarlıkla | Kuruma göre değişir | S2 senaryosu |
| **Midas uygulaması (manuel)** | BIST, ABD, AB, kripto | Bot Telegram'a "emir fişi" yollar, kullanıcı uygulamadan girer | BIST %0, ABD 1,5 $ | Otomasyon yok | **Orta Vade Bot – birincil** |
| Interactive Brokers API | ABD/AB | TWS/IB Gateway API | Düşük komisyon | TL fonlama banka üzerinden (makas + BSMV + SWIFT) | Orta Vade – opsiyonel |

> **Midas'ın herkese açık bir trading API'si yok.** Otomasyon için MatriksIQ entegrasyonu kullanılır. Midas'ta açığa satış/kredili işlem desteği ⚠ teyit edilecek. Desteklenmiyorsa günlük bot Faz 1'de **yalnızca long** çalışır.

## 2. Piyasa verisi

| İhtiyaç | Kaynak | Derinlik / gecikme | Not |
|---|---|---|---|
| Canlı BIST tick/1 dk | **MatriksIQ** (lisanslı) | Gerçek zamanlı | Günlük bot için zorunlu. Ücretsiz kaynaklar 15 dk gecikmeli |
| Geçmiş BIST 1 dk / 5 dk (backtest) | MatriksIQ geçmiş veri dışa aktarımı | Yıllar (⚠ teyit) | 2 yıllık gün içi backtest için birincil |
| Geçmiş BIST gün içi (yedek) | `borsapy` (TradingView) | 1/3/5/15/30/45 dk barlar. Geçmiş derinliği sınırlı | Yalnızca kişisel/eğitim kullanımı |
| Geçmiş BIST 1 saat | Yahoo Finance (`.IS`) | ~730 gün | Kaba doğrulama |
| Günlük OHLCV, düzeltilmiş | `borsapy` (İş Yatırım), Yahoo | 10+ yıl | Bedelsiz/temettü düzeltmesi kontrol edilecek |
| BIST100 geçmiş bileşenleri | Borsa İstanbul endeks dönem listeleri | Çeyreklik | Survivorship bias'ı önlemek için şart |
| VBTS tedbir geçmişi | Borsa İstanbul duyuruları / KAP | Günlük | Brüt takas ve tek fiyat günleri hariç tutulur |
| VİOP XU030 vadeli | MatriksIQ | Gerçek zamanlı | Gün içi rejim filtresi |
| ABD/AB fiyatları | `yfinance`, Stooq, (ücretli) Polygon/FMP | Günlük 20+ yıl | |
| Kripto | BtcTurk / Binance açık API'leri | Dakikalık | Midas Kripto fiyatıyla fark izlenir |
| Makro | **TCMB EVDS API** (resmi, ücretsiz anahtar), TÜİK | Aylık/günlük | Politika faizi, TÜFE, Yİ-ÜFE, rezervler |
| PPF getirisi (benchmark) | **TEFAS** fon fiyatları (`borsapy.Fund`) | 5 yıl | Benchmark ve nakit kolu |
| Küresel risk | VIX, S&P vadeli, DXY, Brent, Türkiye 5Y CDS | Günlük | Rejim filtresi |

## 3. Temel analiz verisi

| İhtiyaç | Kaynak | Not |
|---|---|---|
| Çeyreklik bilanço, gelir tablosu, nakit akışı | `borsapy` (İş Yatırım mali tablolar), KAP | Bankalar UFRS formatında ayrı. Bankalarda nakit akışı yok |
| Yayın zamanı (point-in-time) | **KAP bildirim zaman damgası** | Backtestte veri, KAP yayın anından sonra kullanılır |
| Faaliyet raporu, dipnotlar | KAP PDF'leri | LLM ile okunur: döviz pozisyonu, ilişkili taraf, dava, karşılıklar |
| Analist hedef fiyat ve önerileri | `borsapy` (hedeffiyat.com.tr) | Konsensüs, revizyon sayısı |
| Aracı kurum raporları | Kurumların herkese açık PDF bültenleri (İş Yatırım, Garanti BBVA Yatırım, Yapı Kredi Yatırım vb.) | LLM ile hedef fiyat/öneri/gerekçe çıkarımı |
| ABD temel veri | **SEC EDGAR** (resmi, ücretsiz XBRL), FMP/Finnhub (ücretli) | |
| Kazanç takvimi | KAP etkinlik takvimi (`borsapy`), ABD için Nasdaq/FMP | Bilanço sürprizi stratejisi için |

**Enflasyon muhasebesi (TMS 29):** 2023 yıl sonundan beri finansal tablolar enflasyona göre düzeltiliyor. Net kâr, "net parasal pozisyon kazanç/kayıpları" yüzünden oynak. Bu yüzden faktörlerde **FAVÖK, faaliyet nakit akışı ve reel büyüme** öncelikli tutulur. Bankalar için uygulama BDDK kararına göre farklı olabilir (⚠ teyit).

## 4. Haber ve "uzman görüşü"

| Kaynak | Toplama yöntemi | İşlem |
|---|---|---|
| KAP bildirimleri (tümü) | `borsapy` KAP haberleri / KAP sayfaları | Olay sınıflandırma + duygu skoru |
| Haber siteleri (AA Finans, Bloomberg HT vb.) | RSS | Başlık filtresi (küçük model) → önemli olanlar büyük modele |
| Aracı kurum günlük bültenleri | PDF indirme | Öneri/hedef fiyat değişiklikleri |
| YouTube/podcast uzman yorumları (opsiyonel) | Transkript API | Yalnızca **geçmiş isabet oranı ölçülen** kaynaklar ağırlık alır |

### LLM boru hattı (Claude API)

1. **Filtre (küçük/ucuz model):** "Bu metin şu hisseyle ilgili mi, fiyatı etkileyecek bir olay mı?" sorusu. Çıktı JSON.
2. **Analiz (büyük model):** Olay türü, yönü (−2…+2), önemi (0–1), süre ufku, güven skoru. Bilançolarda kalite işaretleri de çıkarılır.
3. **Maliyet kontrolü:** Sistem promptu ve şemalar **prompt caching** ile önbelleğe alınır. Çeyreklik toplu bilanço okumaları **Batch API** ile yapılır. Her çağrının girdi hash'i ve çıktısı DB'ye yazılır, aynı metin bir daha analiz edilmez.
4. **Uyarı (look-ahead):** LLM'ler eğitim verilerinde 2024–2026 olaylarını görmüş olabilir. Bu yüzden LLM skorlarının geçmiş veride backtesti **güvenilir değildir**. Bu bileşenler yalnızca ileriye dönük paper trading ile doğrulanır (bkz. [05](05-backtest-plani.md) §5).

## 5. Yazılım kütüphaneleri

| Amaç | Kütüphane |
|---|---|
| Veri işleme | `polars`, `pandas`, `duckdb`, `pyarrow` |
| İndikatör | Kendi implementasyonumuz (tutarlılık için) + doğrulama için `ta-lib` |
| Backtest | Kendi olay tabanlı motorumuz. Hızlı tarama için `vectorbt` |
| İstatistik | `scipy`, `statsmodels`, `arch` (bootstrap) |
| Zamanlama | `APScheduler` |
| Bildirim | `python-telegram-bot` |
| LLM | `anthropic` SDK |
| Konfigürasyon | `pydantic-settings` + YAML |

## Kaynaklar

- [Matriks – MatriksIQ dışarıdan emir kabulü API dokümanı](https://www.matriksdata.com/website/uploads/0_matriksiq-disaridanemirkabuluapi.pdf)
- [Matriks – Algoritmik işlem ürünleri](https://www.matriksdata.com/website/urunlerimiz/kullanici-platformlari/algoritmik-islem-urunleri)
- [Midas – Matriks'te Midas](https://www.getmidas.com/midas-matrikste/)
- [borsapy (PyPI)](https://pypi.org/project/borsapy/) · [GitHub](https://github.com/saidsurucu/borsapy)
- [Merdiven – AlgoLab kullanan açık kaynak BIST botu](https://github.com/ElifPinarBalbal/Merdiven)
- [Deniz Yatırım – Algoritmik işlemler](https://www.denizyatirim.com/AlgorithmicOperations)
- [Lopez-Lira & Tang – Can ChatGPT Forecast Stock Price Movements?](https://arxiv.org/abs/2304.07619)
