# 05 · Backtest planı (son 2 yıl)

## 1. Dönemler

| Bot | Dönem | Kullanım |
|---|---|---|
| Günlük | **11.11.2024 – 10.11.2026** | Walk-forward: 6 ay eğitim → 2 ay test, 2 ay kaydırarak **9 test katmanı**, toplam 18 ay örneklem dışı (OOS) |
| Orta vade | 11.2018 – 10.11.2024 | Kalibrasyon (parametre seçimi) |
| Orta vade | **11.11.2024 – 10.11.2026** | **Tamamen OOS rapor dönemi.** Bu dönemde hiçbir parametre ayarlanmaz |

> Neden orta vade için daha uzun geçmiş? 2 yılda yalnızca ~6 bağımsız 16 haftalık dönem var. Parametreleri bu kadar az veriyle seçmek aşırı uyuma (overfitting) yol açar. Son 2 yıl bu yüzden "sınav" olarak ayrılır.

**İçerilen stres dönemleri:** 19–21 Mart 2025 iç siyasi şok (BIST'te devre kesiciler, sert düşüş), Nisan 2025 küresel gümrük tarifesi şoku, TCMB faiz artırımı ve indirim döngüleri.

## 2. Veri kontrol listesi

| Veri | Kaynak | Gerekli kontrol |
|---|---|---|
| BIST 1 dk barlar (100+ hisse, 2 yıl) | MatriksIQ dışa aktarım. Yedek: `borsapy` 5 dk | Eksik bar, seans dışı bar, açılış müzayedesi barı ayrı mı? |
| Günlük düzeltilmiş fiyat | `borsapy` / İş Yatırım | Bedelsiz ve temettü düzeltmesi Yahoo ile çapraz kontrol |
| BIST100 bileşenleri (çeyreklik) | Borsa İstanbul | Her gün o günkü liste kullanılır (survivorship) |
| VBTS tedbir geçmişi | BIST duyuruları / KAP | Tedbir başlangıç-bitiş günleri |
| Finansal tablolar + **KAP yayın zaman damgası** | `borsapy` + KAP | Point-in-time: yayından önce kullanım yasak |
| XU100 getiri endeksi, TEFAS PPF fiyatları, USDTRY, TÜFE, Yİ-ÜFE | BIST, TEFAS, TCMB EVDS | Benchmark ve vergi hesabı |
| ABD fiyat + temel veri | `yfinance`, SEC EDGAR | Delist olan hisseler dahil (tekil hisse kolu için) |
| BTC/ETH günlük | BtcTurk / Binance | TL çevrimi |

## 3. Motor tasarımı

- **Olay tabanlı.** Strateji kodu canlıdakiyle aynı. `PaperBroker` simülasyon modunda çalışır.
- **Zaman:** Günlük bot 1 dk barlarla. Bar içi sıralama belirsizse **kötümser** varsayım: aynı barda hem stop hem TP görüldüyse önce stop vurulmuş sayılır.
- **Dolum modeli:**

| Emir | Dolum kuralı |
|---|---|
| Stop-limit alış | Bar yüksek ≥ tetik ise dolum = max(tetik, bar açılış) + 1 adım. Limit aşılıyorsa dolum yok |
| Limit (pasif) | Fiyat limiti **en az 1 adım geçerse** dolar (sadece dokunması yetmez) |
| Marketable limit / piyasa | Sonraki bar açılışı ± (½ spread + 0,5 adım) |
| Kapanış seansı (orta vade) | Kapanış fiyatı. Emir, kapanış seansı hacminin %10'unu aşamaz |
| Hacim tavanı | Bir emir, ilgili 1 dk bar hacminin %10'unu aşamaz. Fazlası sonraki barlara bölünür |

- **Maliyet modeli:** `config/costs.example.yaml`. Senaryolar S0–S3, örtük maliyet iyimser/baz.
- **Kurumsal işlemler:** Temettü nakit olarak (net %15 stopaj) eklenir. Bedelsiz ve bölünmelerde adet düzeltilir.
- **Vergi:** BIST %0. Yurt dışı kazançlar yıl sonunda TL bazında hesaplanıp endeksleme kuralıyla vergilenir ve özsermayeden düşülür.

## 4. Önyargılar ve önlemler

| Önyargı | Önlem |
|---|---|
| Survivorship | Günlük tarihsel BIST100 listesi. Delist olan hisseler veride tutulur |
| Look-ahead (fiyat) | Sinyal $t$ barı kapanışında, emir $t+1$'de |
| Look-ahead (bilanço) | KAP yayın zaman damgası + 1 seans |
| Look-ahead (LLM) | LLM bileşenleri backtestte **kullanılmaz**. İleri test edilir (§5) |
| Veri madenciliği | Sınırlı ızgara, walk-forward, Deflated Sharpe, PBO (CSCV) |
| Maliyetin hafife alınması | Baz örtük maliyet + 2× maliyet stres testi |
| Kapasite | Hacim tavanı + sermaye ölçek testi (0,5 / 1 / 5 milyon TL) |

## 5. LLM bileşenlerinin ileriye dönük testi

Paper trading başladığı günden itibaren her LLM skoru zaman damgasıyla kaydedilir. 3 ay sonra bilgi katsayısı (IC) hesaplanır:

$$
IC_t = \rho_{\text{Spearman}}\!\left(s_{i,t},\; r_{i,\,t\to t+h} - r_{XU100,\,t\to t+h}\right), \qquad h \in \{5, 20\}\ \text{gün}
$$

$$
t_{IC} = \frac{\overline{IC}}{\sigma_{IC}/\sqrt{n}}
$$

**Kabul:** $\overline{IC} > 0.03$ ve $t_{IC} > 2$. Sağlanmazsa bileşenin ağırlığı sıfırlanır.

## 6. Ölçütler

Günlük getiri $r_t$, TL risksiz getiri olarak PPF günlük getirisi $r^f_t$ kullanılır:

$$
SR = \frac{\overline{r_t - r^f_t}}{\sigma\!\left(r_t - r^f_t\right)}\sqrt{252}
$$

**Deflated Sharpe Ratio** ($N$ deneme, $T$ gözlem, $\hat\gamma_3$ çarpıklık, $\hat\gamma_4$ basıklık):

$$
DSR = \Phi\!\left(\frac{\left(\widehat{SR} - SR_0\right)\sqrt{T-1}}{\sqrt{1 - \hat\gamma_3\,\widehat{SR} + \frac{\hat\gamma_4 - 1}{4}\,\widehat{SR}^2}}\right)
$$

$$
SR_0 = \sqrt{\mathbb{V}\!\left[\widehat{SR}_n\right]}\left((1-\gamma_e)\,\Phi^{-1}\!\left(1 - \tfrac{1}{N}\right) + \gamma_e\,\Phi^{-1}\!\left(1 - \tfrac{1}{N e}\right)\right), \qquad \gamma_e \approx 0.5772
$$

Not: Bu formüldeki Sharpe değerleri yıllıklandırılmamış, gözlem frekansındaki Sharpe'tır.

| Grup | Ölçütler |
|---|---|
| Getiri | Toplam, yıllık (CAGR), **reel** (TÜFE'ye göre), USD bazlı, PPF'ye göre fazla getiri |
| Risk | Volatilite, max drawdown, en kötü gün/hafta, Sortino, Calmar |
| İşlem (günlük bot) | İşlem sayısı, kazanma oranı, ortalama R, beklenen değer, profit factor, ortalama elde tutma süresi |
| Maliyet | Maliyet / brüt kâr oranı, yıllık devir, emir/işlem oranı |
| Sağlamlık | DSR, PBO, katmanlar arası tutarlılık, parametre platosu |

## 7. Karşılaştırma tabanları

| Bot | Taban |
|---|---|
| Günlük | (a) PPF, (b) XU100 al-tut, (c) **rastgele giriş** (aynı sayıda işlem, aynı stop/çıkış ve boyutlandırma, 1.000 simülasyon), (d) "stocks in play" filtresi olmadan ORB |
| Orta vade | (a) XU100 getiri endeksi, (b) PPF, (c) %60 XU100 / %40 PPF sabit karışım, (d) USDTRY (TL bazında dolar tutmak) |

(c) ve (d) şunu test eder: Kâr gerçekten **seçimden** mi geliyor, yoksa şanstan ya da genel piyasa yönünden mi?

## 8. Stres ve Monte Carlo

- **Blok bootstrap** (blok uzunluğu 10 gün) ile 10.000 eğri → CAGR ve max DD için %5 / %50 / %95 dağılımı.
- **Maliyet ×1,5 ve ×2**, kayma +1 adım.
- **En iyi 5 günü çıkar:** Sonuç hâlâ pozitif mi?
- **Ölçek:** 0,5 / 1 / 5 milyon TL sermayede hacim tavanının etkisi.

## 9. Canlıya geçiş (go/no-go) kriterleri

### Günlük Bot (S1 maliyet, baz örtük, OOS)

| Kriter | Eşik |
|---|---|
| Yıllık net getiri (yedek dahil özkaynak üzerinden) | > PPF + 10 puan |
| Sharpe (PPF'ye göre) | ≥ 1,0 |
| Deflated Sharpe | ≥ 0,95 |
| Max drawdown | ≤ %15 |
| Profit factor / işlem sayısı | ≥ 1,3 / ≥ 300 |
| 2× maliyette | Hâlâ > PPF |
| Walk-forward katmanları | 9 katmanın ≥ 6'sında pozitif fazla getiri |
| Rastgele giriş tabanına karşı | p < 0,05 |

### Orta Vade Bot (son 2 yıl OOS)

| Kriter | Eşik |
|---|---|
| Net getiri (vergi sonrası) | > max(XU100 getiri endeksi, PPF) |
| Bilgi oranı (XU100'e göre) | ≥ 0,3 |
| Max drawdown | ≤ %25 |
| Yıllık devir | ≤ %400 |
| Kalibrasyon ↔ OOS tutarlılığı | OOS Sharpe ≥ kalibrasyon Sharpe'ının %50'si |

Kriterler sağlanmazsa: Strateji **canlıya alınmaz**. Önce neden çalışmadığı analiz edilir (maliyet mi, seçim mi, rejim mi), sonra yeni hipotezle tekrar test edilir. Aynı veriyle tekrar tekrar ayar yapmak DSR'deki $N$'i artırır ve bu kayda geçer.

## 10. Çıktılar

- `reports/backtest_<bot>_<tarih>.html`: Özkaynak eğrileri, drawdown, aylık getiri ısı haritası, parametre platosu ısı haritası, maliyet dökümü.
- `reports/trades_<bot>.parquet`: Tüm işlemler.
- Özet tablo bu dokümanın sonuna eklenecek.
