# 06 · Yol haritası, riskler ve açık sorular

## 1. Fazlar

| Faz | İçerik | Çıktı | Tahmini süre |
|---|---|---|---|
| **0** | Plan (bu repo) | Dokümanlar + parametre dosyaları | ✔ |
| **1** | Veri katmanı: indiriciler, point-in-time depo, kalite kontrolleri | `data/` + veri kalite raporu | 2–3 hafta |
| **2** | Backtest motoru + maliyet modeli + iki botun strateji kodu | `backtest/`, `strategies/` | 3–4 hafta |
| **3** | Son 2 yıl backtest, walk-forward, stres testleri | HTML raporlar + **go/no-go kararı** | 1–2 hafta |
| **4** | Paper trading. Günlük bot MatriksIQ test ortamında, orta vade bot emir fişleriyle | Gerçekleşen kayma vs model, LLM IC ölçümü | 6–8 hafta |
| **5** | Küçük sermayeyle canlı (hedef sermayenin %10–20'si) | Canlı metrikler | 3 ay |
| **6** | Ölçekleme. Metrikler backtest güven aralığındaysa sermaye kademeli artırılır | – | – |

**Faz 4 → 5 geçiş şartı:** Gerçekleşen kayma, modeldeki kaymanın en fazla 1,5 katı olmalı. Paper getiri, backtest dağılımının %5'lik dilimin üzerinde kalmalı.

## 2. Riskler

| Risk | Etki | Önlem |
|---|---|---|
| Maliyetlerin edge'i yemesi (günlük) | Yüksek | S1 yolu, adım/fiyat filtresi, stop ≥ 6,7× maliyet, emir değiştirme limiti |
| Aşırı uyum (overfitting) | Yüksek | Walk-forward, DSR, PBO, plato seçimi, son 2 yılın orta vade için kör tutulması |
| Rejim değişimi (faiz döngüsü, siyasi şok) | Yüksek | Rejim filtreleri, kill-switch, PPF kolu |
| Teknik arıza (bağlantı, bot çökmesi) | Orta | Watchdog, idempotent emirler, kurum tarafı yedek şartlı emir |
| Gece pozisyonu kalması (devre kesici, taban kilidi) | Düşük olasılık, yüksek etki | Kapanış protokolü, %7 hareket filtresi, alarm |
| Platform/komisyon değişikliği (Midas kampanyaları) | Orta | Maliyet parametreleri config'de. Aylık kontrol |
| Vergi mevzuatı değişikliği (kripto, hisse stopajı) | Orta | Vergi modülü parametrik. Yıllık gözden geçirme |
| Veri lisansı ihlali | Orta | Ham veri repoya girmez. `borsapy` kişisel kullanım şartı |
| LLM hatası / halüsinasyon | Orta | Sayısal alanlar çapraz kontrol, IC testi, olay aksiyonlarında insan onayı |
| Mevzuat (SPK) | Düşük | Sadece kendi hesabında işlem. Sinyal yayımlama/satma yok |

## 3. Canlıya geçmeden önce teyit edilecek açık sorular

| # | Soru | Kime |
|---|---|---|
| 1 | Matriks üzerinden Midas emirlerinde borsa payı/MKK ücreti de Midas tarafından mı karşılanıyor? | Midas |
| 2 | Midas hesabında açığa satış ve kredili işlem var mı? | Midas |
| 3 | MatriksIQ dış emir API'si için ayrı lisans gerekiyor mu? Midas kullanıcısına aylık toplam ücret ne? Rate limit var mı? | Matriks / Midas |
| 4 | MatriksIQ'dan kaç yıllık 1 dk BIST verisi dışa aktarılabilir? | Matriks |
| 5 | Midas TL→USD çevriminde uygulanan makas ve BSMV | Midas (işlem dökümünden ölçülecek) |
| 6 | Midas'ta TL PPF / nema seçenekleri ve valörleri | Midas |
| 7 | Yurt dışı hisse ve kripto kazançlarının beyanı, zarar mahsubu | Mali müşavir |
| 8 | Bankalarda TMS 29 uygulaması (2026) | KAP/BDDK |

## 4. Bütçe önerisi

| Bot | Önerilen asgari sermaye | Gerekçe |
|---|---|---|
| Günlük (otomatik, S1) | **~0,6–1 milyon TL** | Sabit platform ücreti ≤ yılda %5 |
| Günlük (paper) | 0 TL | Veri lisansı paper için de gerekli (aylık ücret) |
| Orta vade | Sınır yok | Midas'ta BIST komisyonu %0. Yurt dışı pozisyonlar ≥ 1.500 $ |

## 5. Sonraki adım

Faz 1: Veri katmanı. İlk iş, MatriksIQ'dan 2 yıllık 1 dk veri alınıp alınamayacağının ve BIST100 tarihsel bileşen listelerinin netleştirilmesi. Günlük botun backtesti buna bağlı.
