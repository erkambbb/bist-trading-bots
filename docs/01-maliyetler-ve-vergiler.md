# 01 · Türkiye işlem maliyetleri, vergiler ve başabaş hesapları

> Kasım 2026 itibarıyla kamuya açık kaynaklardan derlenmiştir. Oranlar değişebilir. "⚠ teyit" işaretli kalemler canlıya geçmeden önce kurumdan yazılı olarak doğrulanacak. Vergi kısmı genel bilgidir, mali müşavir görüşünün yerini tutmaz.

## 1. Maliyet bileşenleri

### 1.1 BIST pay piyasası

| Kalem | Oran / tutar | Kim öder | Kaynak |
|---|---|---|---|
| Aracı kurum komisyonu | Kuruma göre değişir: **Midas uygulaması %0**, Midas'ın Matriks üzerinden emirleri **%0,01 + BSMV**, tipik internet kurumu %0,05–0,18 | Yatırımcı | Midas ücretler sayfası, GCM ve Ata Yatırım tarifeleri |
| BSMV | Komisyonun **%5**'i | Yatırımcı | Kurum tarifeleri |
| Borsa payı | İşlem hacminin **yüzbinde 2,5**'i (0,25 bps) | Yatırımcı. **Midas'ta Midas öder** | BIST ücret tarifesi |
| Takasbank payı | **Milyonda 4** | Aynı | Ata Yatırım tarifesi |
| MKK alım-satım ücreti | Kayıt başına ~0,11 TL | Aynı | MKK tarifesi (Ata Yatırım üzerinden) |
| Emir iptal/değiştirme ücreti | Fiyat seviyesinde 0–5 sn içinde iptal: 0,75 bps, 6–10 sn: 0,45 bps, 11 sn ve sonrası: 0,30 bps. Fiyat seviyesi dışında: 0,12 bps | Kuruma yansır, kurum yatırımcıya yansıtabilir | BIST tarifesi |
| Emir/İşlem Oranı (OTR) cezası | Girilen + değiştirilen + iptal edilen emir sayısı, gerçekleşen işlem sayısının **5 katını** aşarsa aşan her işlem için **0,50 TL** | Aynı | BIST tarifesi |

**Bot tasarımına etkisi:** Trailing stop'u her tikte güncellemek hem iptal ücreti hem OTR cezası doğurur. Stoplar istemci tarafında tutulur ve emirler en fazla dakikada bir değiştirilir (bkz. [03](03-gunluk-bot.md) §7).

### 1.2 Örtük maliyetler: spread, kayma, fiyat adımı

BIST pay fiyat adımları:

| Fiyat aralığı (TL) | Fiyat adımı | Adım/fiyat |
|---|---|---|
| 0,01 – 19,99 | 0,01 | %0,05 – %100 |
| 20,00 – 49,98 | 0,02 | %0,04 – %0,10 |
| 50,00 – 99,95 | 0,05 | %0,05 – %0,10 |
| 100,00 ve üzeri | 0,10 | ≤ %0,10 |

Gidiş-dönüş örtük maliyet modeli. Giriş stop tetiklemeli olduğu için yarım spread + 1 adım kayma, çıkış marketable limit olduğu için yarım spread + 0,5 adım kayma:

$$
c_{\text{örtük}} = \underbrace{\left(\tfrac{1}{2} + 1\right)\frac{\text{adım}}{P}}_{\text{giriş}} + \underbrace{\left(\tfrac{1}{2} + \tfrac{1}{2}\right)\frac{\text{adım}}{P}}_{\text{çıkış}} = 2.5\,\frac{\text{adım}}{P}
$$

| Fiyat (TL) | Adım | Adım/fiyat | Gidiş-dönüş örtük maliyet |
|---|---|---|---|
| 8 | 0,01 | %0,125 | **%0,31** |
| 15 | 0,01 | %0,067 | %0,17 |
| 35 | 0,02 | %0,057 | %0,14 |
| 75 | 0,05 | %0,067 | %0,17 |
| 150 | 0,10 | %0,067 | %0,17 |
| 300 | 0,10 | %0,033 | %0,08 |
| 600 | 0,10 | %0,017 | %0,04 |

**Sonuç:** Gün içi botta **adım/fiyat ≤ %0,08** filtresi şart. Düşük fiyatlı hisseler, komisyon sıfır bile olsa, maliyet yüzünden elenir.

### 1.3 Yurt dışı (Midas)

| Kalem | Değer |
|---|---|
| ABD hisse/ETF | İşlem başına **1,5 USD** |
| Avrupa hisse/ETF | İşlem başına **1,5 EUR** |
| 1 USD altı hisse, 300 adet üstü | Ek adet başına 0,005 USD |
| ADR saklama | Hisse başına dönemsel 0,01–0,03 USD |
| Dışarı transfer | ACATS 25 USD, DTC hisse başına 5 USD |
| TL→USD çevrimi | Kur makası, **⚠ teyit**. Midas yayımlamıyor. İşlem anında TCMB/piyasa kuru ile karşılaştırılarak ölçülecek |
| Döviz alımında BSMV | **%0,2** (bireysel döviz alımı) **⚠ teyit** |
| ABD'de gün içi al-sat kısıtı | Midas'ta "günlük al-sat limiti" var (ABD kuralı). Orta vade bot gün içi al-sat yapmaz |

Komisyonun pozisyon büyüklüğüne oranı:

| Pozisyon | Tek yön | Gidiş-dönüş |
|---|---|---|
| 500 $ | %0,30 | %0,60 |
| 1.000 $ | %0,15 | %0,30 |
| 2.000 $ | %0,075 | %0,15 |
| 5.000 $ | %0,03 | %0,06 |
| 10.000 $ | %0,015 | %0,03 |

**Kural:** Yurt dışında **minimum pozisyon 1.500 $** (komisyon ≤ %0,1). TL↔USD dönüşüm maliyeti (makas + BSMV), makas %0,10–0,50 için gidiş-dönüş **%0,4–1,2** tutar. Bu yüzden USD kolu sürekli tutulur, her işlemde TL'ye geri dönülmez. Boştaki USD Midas'ın dolar nema ürününde bekler.

### 1.4 Kripto (Midas Kripto)

| Aylık hacim | Komisyon (tek yön) |
|---|---|
| 0 – 10 milyon TL | %0,15 |
| 10 – 100 milyon TL | %0,12 |
| 100 milyon TL üstü | %0,10 |

Gidiş-dönüş ≈ **%0,30** + spread. Haftalık rebalans yerine ±2 puanlık **tolerans bandı** kullanılır, böylece işlem sayısı azalır.

### 1.5 Sabit maliyetler (gün içi bot)

| Kalem | Aylık (örnek) | Not |
|---|---|---|
| MatriksIQ veri terminali (hisse yüzeysel) | ~1.900 TL | Osmanlı Yatırım 2026 tarifesi. Midas kullanıcıları için ⚠ teyit |
| MatriksIQ Algo lisansı | ~2.250 TL | Aynı kaynak |
| Windows VPS | ~500–1.000 TL | İstanbul lokasyonu |
| Claude API (LLM) | ~10–40 $ | Prompt caching + Batch API ile |

Toplam sabit maliyet **F ≈ 2.500–4.500 TL/ay**. Sermayeye oranı:

| Sermaye K | F = 2.500 TL/ay | F = 4.500 TL/ay |
|---|---|---|
| 100.000 TL | %30,0 / yıl | %54,0 / yıl |
| 250.000 TL | %12,0 | %21,6 |
| 500.000 TL | %6,0 | %10,8 |
| 1.000.000 TL | %3,0 | %5,4 |
| 2.000.000 TL | %1,5 | %2,7 |

**Kural:** Gün içi bot, sabit maliyet yıllık sermayenin **≤ %5**'i olacak şekilde, yani yaklaşık **K ≥ 0,6–1 milyon TL** ile canlıya alınır. Daha küçük sermayede paper trading ile başlanır.

## 2. Gün içi bot: maliyet senaryoları

### 2.1 Açık maliyet (tek yön)

| Senaryo | Açıklama | Tek yön | Gidiş-dönüş |
|---|---|---|---|
| **S0** | Midas uygulaması (manuel, otomasyon yok) | %0 | %0 |
| **S1** | Midas + MatriksIQ API: %0,01 + BSMV, borsa payı Midas'ta | %0,0105 | %0,021 |
| **S2** | API'li kurum, pazarlıklı %0,02 + BSMV + borsa payı | %0,024 | %0,048 |
| **S3** | API'li kurum, standart %0,09 (borsa payı dahil) + BSMV | %0,0945 | %0,189 |

### 2.2 Toplam gidiş-dönüş maliyeti (açık + örtük)

| Senaryo | İyimser örtük (%0,08) | Baz örtük (%0,15) |
|---|---|---|
| S0 | %0,080 | %0,150 |
| S1 | %0,101 | **%0,171** |
| S2 | %0,128 | %0,198 |
| S3 | %0,269 | %0,339 |

### 2.3 Yıllık maliyet yükü

Yıllık maliyet yükü, aktif gün sayısı $D$, ortalama işlem gören özkaynak oranı $X$ (gün içi devir) ve gidiş-dönüş maliyet $c_{rt}$ ile hesaplanır:

$$
\text{Yük}_{\text{yıllık}} = D \cdot X \cdot c_{rt}
$$

Baz örtük maliyetle (%0,15):

| Senaryo | 250 gün, %80 | 200 gün, %60 | 150 gün, %50 |
|---|---|---|---|
| S0 | %30,0 | %18,0 | %11,2 |
| S1 | %34,2 | **%20,5** | %12,8 |
| S2 | %39,6 | %23,8 | %14,9 |
| S3 | %67,8 | %40,7 | %25,4 |

### 2.4 PPF'yi geçmek için gereken brüt getiri

%20'lik yedek PPF'de durursa (net ≈ %28,5/yıl varsayımı), botun PPF'yi geçmesi için gereken brüt işlem getirisi:

$$
R_{\text{brüt}} > \text{Yük} + 0.8 \cdot r_{\text{PPF}}
$$

| Senaryo (200 gün, %60) | Gereken brüt getiri |
|---|---|
| S0 | > %40,8 |
| **S1** | **> %43,3** |
| S2 | > %46,6 |
| S3 | > %63,5 |

### 2.5 İşlem başına beklenen değer ve maliyetin R cinsinden payı

Bir işlemin beklenen değeri, kazanma oranı $p$, ortalama kazanç $\bar W$ ve ortalama kayıp $\bar L$ (R cinsinden), maliyet $c_{rt}$ ve stop mesafesi $d$ ile:

$$
E[R] = p\,\bar W - (1-p)\,\bar L - \frac{c_{rt}}{d}
$$

Örnek: $p = 0.45$, $\bar W = 2R$, $\bar L = 1R$ olsun. Maliyet öncesi $E = 0.35R$.

| $c_{rt}$ \ stop mesafesi $d$ | %0,4 | %0,6 | %1,0 | %1,5 |
|---|---|---|---|---|
| %0,10 | 0,29R | 0,17R | 0,10R | 0,07R |
| %0,17 | 0,49R | 0,28R | 0,17R | 0,11R |
| %0,28 | 0,80R | 0,47R | 0,28R | 0,19R |

**Tasarım kuralı:**

$$
\frac{c_{rt}}{d} \le 0.15 \quad\Longrightarrow\quad d \ge \frac{c_{rt}}{0.15} \approx 6.7\,c_{rt}
$$

S1 baz senaryosunda stop mesafesi **≥ %1,1** olmalı. ABD ORB çalışmasındaki 0,10×ATR stop (BIST'te ~%0,35) bu yüzden kullanılamaz.

## 3. Orta vade bot: maliyetler

| Varlık | Tipik gidiş-dönüş | Yıllık devir varsayımı | Yıllık yük |
|---|---|---|---|
| BIST (Midas uygulaması, kapanış seansı) | ~%0,05–0,10 (sadece örtük) | %300 | %0,15–0,30 |
| ABD/AB hisse/ETF (≥ 1.500 $) | %0,10–0,20 | %200 | %0,2–0,4 |
| TL↔USD dönüşümü | %0,4–1,2 (makas + BSMV) | Yılda 1–2 kez | %0,4–1,2 (yalnızca USD kolu için) |
| Kripto | ~%0,35 | %300 | ~%1,0 (kripto kolu için) |

Orta vadede maliyet ikincil. Asıl belirleyiciler **vergi** ve **seçim kalitesi**.

## 4. Vergiler (bireysel, tam mükellef, 2026)

| Gelir | Vergi | Beyan |
|---|---|---|
| BIST'te işlem gören hisse alım-satım kazancı (2006 sonrası edinim) | **%0 stopaj** (GVK geçici 67) | Yok |
| BIST hisse temettüsü | **%15 stopaj**. Brüt temettünün yarısı istisna. Kalan, 2026 için 400.000 TL eşiği aşılırsa beyan edilir | Eşik üstü |
| Hisse senedi yoğun fon (TEFAS) | %0 | Yok |
| Diğer fonlar / PPF | Genel fon oranı **%17,5** (9.7.2025 sonrası edinim) **⚠ teyit (PPF bireysel)** | Yok |
| Yurt dışı hisse/ETF alım-satım kazancı | TL bazında hesaplanır, **beyan edilir**, %15–40 artan oranlı. İstisna tutarı yok. Yİ-ÜFE artışı **≥ %10** ise maliyet endekslenir | Mart ayında yıllık beyan |
| Yurt dışı temettü | Beyan edilir. 2026 beyan eşiği 22.000 TL. Yurt dışında ödenen vergi mahsup edilebilir (ABD kaynak kesintisi ~%20, **⚠ teyit**) | Eşik üstü |
| Kripto | **Kriptoya özel vergi yürürlükte değil.** 2026 tasarısındaki %0,03 işlem vergisi ve %10 stopaj maddeleri 27.03.2026'da geri çekildi. Sürekli alım-satım ticari kazanç sayılabilir. CARF ile yurt dışı borsa verisi paylaşılabilir | Belirsiz. **Mali müşavire sorulacak** |

### 4.1 Yurt dışı hisse: kısa vadede kur kazancı da vergilenir

TL bazlı vergiye tabi kazanç (endeksleme yoksa):

$$
G_{TL} = Q\left(P_s \cdot K_s - P_a \cdot K_a\right) - \text{komisyonlar}
$$

Burada $K$ işlem günündeki kurdur. Endeksleme koşulu sağlanırsa (alış ayı dahil, satış ayı hariç Yİ-ÜFE artışı ≥ %10):

$$
G_{TL}^{\text{end.}} = Q\left(P_s K_s - P_a K_a \cdot \frac{\text{ÜFE}_{s-1}}{\text{ÜFE}_{a-1}}\right)
$$

| Senaryo | TL kazanç | Vergiye tabi | %15 dilimde net | %27 dilimde net |
|---|---|---|---|---|
| 12 hafta: USD +%8, kur +%6, ÜFE +%7 (endeksleme yok) | %14,5 | %14,5 | %12,3 | %10,6 |
| 1 yıl: USD +%12, kur +%20, ÜFE +%22 (endeksleme var) | %34,4 | %12,4 | %32,5 | %31,1 |

**Sonuç:** 1–16 haftalık yurt dışı işlemlerde endeksleme çoğu zaman devreye girmez. Bu yüzden yurt dışı pozisyonlar daha yüksek bir getiri eşiğini geçmeli. Model bunu `foreign_tax_drag` parametresiyle hesaba katar. Aynı kategorideki yurt dışı zararlar aynı yıl içinde kazançtan düşülebilir. Bu nedenle yıl sonunda **zarar hasadı (tax-loss harvesting)** kuralı eklenir.

### 4.2 Diğer yapısal riskler

- **ABD veraset vergisi:** ABD dışı yerleşiklerin ABD'de tuttuğu varlıklarda 60.000 $ üstü için veraset vergisi riski var. Büyük USD kolu için **İrlanda menşeli UCITS ETF'ler** (Midas Avrupa borsaları, 1,5 EUR) tercih edilir.
- **Temettü stopajı** brüt getiriyi düşürür. Backtestte temettüler net tutardan yeniden yatırılır.

## 5. Gerçek eşik: para piyasası

TCMB politika faizi **%36** (22.10.2026 kararı, gecelik borç verme %39). BIST100 de 2025'te enflasyonu yenemedi. Bu yüzden:

- Her iki bot da **XU100 toplam getirisi**, **TEFAS PPF endeksi** ve **USDTRY** ile karşılaştırılır.
- Orta vade botun "nakit" kolu boşta TL değil, **PPF**'dir.
- Günlük botun kullanılmayan %20'si PPF'de tutulabilir (T+1 valör göz önüne alınır).

## Kaynaklar

- [Midas – Ücretler](https://www.getmidas.com/ucretler/)
- [Midas – Matriks entegrasyonu](https://www.getmidas.com/midas-matrikste/)
- [Borsa İstanbul – Borsa payı ve diğer ücretler tarifesi](https://borsaistanbul.com/files/borsa-payi-ve-diger-ucretler-tarifesi.pdf)
- [Borsa İstanbul – Fiyat adımları ve marjlar](https://borsaistanbul.com/en/price-bands)
- [Ata Yatırım – Ücret ve tarifeler](https://atayatirim.com.tr/sss/ucret-ve-tarifeler)
- [GCM Yatırım – Borsa işlem koşulları](https://www.gcmyatirim.com.tr/borsa/borsa-islem-kosullari)
- [Osmanlı Yatırım – Data ve platform kullanım ücretleri](https://www.osmanlimenkul.com.tr/hisse-ve-viop/platformlarimiz/canli-data-ve-platformlar/data-ve-platform-kullanim-ucretleri)
- [Garanti BBVA Yatırım – Menkul kıymet vergilendirmesi 2026](https://www.garantibbva.com.tr/content/dam/public-website/pdf/yatirim/vergilendirme/mk-vergilendirme-2026.pdf)
- [Yapı Kredi – Yabancı hisse senedi gelirlerinde 2026 vergi durumu](https://www.yapikredi.com.tr/medium/file/yabanci-hisse-senedi-gelirlerinde-2026-yili-vergi-durumu_71999/view)
- [PwC – GVK geçici 67 stopaj değişiklikleri (2026/01)](https://www.pwc.com.tr/tr/sektorler/bankacilik-sermaye-piyasalari/bultenler/2026/gvk-gecici-67nci-madde-uyarinca-uygulanan-stopaj-oranlarinda-degisiklikler.html)
- [Gökay Gül SMMM – Kripto vergisi 2026: yürürlükteki durum](https://gokaygul.com/tr/blog/kripto-varlik-vergilendirme-2026/)
- [Para Dergi – TCMB politika faizini %36'ya indirdi (22.10.2026)](https://www.paradergi.com.tr/finans/2026/10/22/merkez-bankasi-politika-faizini-yuzde-36ya-indirdi)
- [Paratic – Borsa için 2025 sona erdi: endeks enflasyonu yenemedi](https://paratic.com/borsa-icin-2025-sona-erdi-endeks-enflasyonu-yenemedi/)
- [Habertürk – Döviz alımında BSMV binde 2](https://www.haberturk.com/son-dakika-banka-bonosu-doviz-ve-altin-alim-satiminda-vergi-ne-oldu-haberler-2819385-ekonomi)
