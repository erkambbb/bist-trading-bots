# bist-trading-bots

Borsa İstanbul odaklı iki açık kaynak trading botunun **tasarım ve planlama** reposu.

> **Durum:** Faz 0, yani sadece plan. Henüz kod yok. Sıradaki adım, son 2 yıllık backtest ile planın işe yarayıp yaramadığını ölçmek.
>
> **Yatırım tavsiyesi değildir.** Buradaki her şey eğitim ve araştırma amaçlıdır. Gerçek parayla kullanmadan önce kendi testlerinizi yapın.

## İki bot

| | **Günlük Bot** (`daily`) | **Orta Vade Bot** (`midterm`) |
|---|---|---|
| Piyasa | BIST100 hisseleri | BIST + yurt dışı hisse/ETF + kripto (≤ %10) |
| Vade | Gün içi. Her pozisyon aynı gün **17:30–17:55** arasında kapanır | 1–16 hafta (gerekirse daha uzun) |
| Bütçe kullanımı | Toplam bütçenin en fazla **%80**'i | Varlık sınıfı bantları + nakit (PPF) kolu |
| Ana yöntem | Açılış aralığı kırılımı ("stocks in play") + VWAP geri çekilme | Çok faktörlü skor (değer, kalite, momentum, bilanço sürprizi, analist, haber) + kademeli alım |
| Karar sıklığı | Dakikalık | Haftalık skor + günlük haber/KAP taraması |
| Emir iletimi | MatriksIQ API (Midas hesabı) veya API'li aracı kurum | Telegram emir fişi ile Midas'ta manuel, ya da MatriksIQ API |

## Dokümanlar

1. [Genel bakış ve mimari](docs/00-genel-bakis-ve-mimari.md)
2. [Türkiye işlem maliyetleri, vergiler ve başabaş hesapları](docs/01-maliyetler-ve-vergiler.md)
3. [Veri kaynakları, API'ler ve araçlar](docs/02-veri-kaynaklari-ve-araclar.md)
4. [Günlük Bot: tam spesifikasyon](docs/03-gunluk-bot.md)
5. [Orta Vade Bot: tam spesifikasyon](docs/04-orta-vade-bot.md)
6. [Backtest planı (son 2 yıl)](docs/05-backtest-plani.md)
7. [Yol haritası, riskler ve açık sorular](docs/06-yol-haritasi-ve-riskler.md)

Parametrelerin varsayılan değerleri `config/` altında:

- [`config/daily_bot.example.yaml`](config/daily_bot.example.yaml)
- [`config/midterm_bot.example.yaml`](config/midterm_bot.example.yaml)
- [`config/costs.example.yaml`](config/costs.example.yaml)

## Plandan çıkan en önemli 6 sonuç

1. **Gün içi botu maliyet belirler.** Açık maliyetin yanında spread ve kayma da var. API'li yollarda gidiş-dönüş toplam maliyet **%0,10–0,34** arasında. Yıllık maliyet yükü, işlem sıklığına ve kuruma göre özkaynağın **%8–68**'i olabiliyor (bkz. [01](docs/01-maliyetler-ve-vergiler.md)). Kârlılığın ilk şartı düşük komisyonlu bir API yolu ve fiyat adımı/fiyat oranı düşük hisseler.
2. **ABD'deki ORB parametreleri BIST'e olduğu gibi taşınamaz.** ABD çalışmasındaki 0,10×ATR stop BIST'te işlem başına 0,3–0,5R maliyete denk geliyor. Stop mesafesi gidiş-dönüş maliyetin en az ~6 katı olmalı.
3. **Gerçek eşik %0 değil, para piyasası fonu.** TCMB politika faizi %36 (22.10.2026). Botların başarısı XU100'e ve PPF getirisine karşı ölçülecek.
4. **Vergi asimetrisi var.** BIST hisse kazancında stopaj %0. Yurt dışı hisse kazancı ise TL bazında beyan ediliyor ve kur artışı da kazanca dahil. Yİ-ÜFE endekslemesi ancak artış %10'u geçince devreye giriyor, bu yüzden 1–16 haftalık işlemlerde genellikle yok.
5. **Sabit platform ücreti küçük sermayeyi bitirir.** Ayda 2.500–4.500 TL'lik veri + algo lisansı, 250 bin TL sermayede yıllık %12–22 demek.
6. **LLM'li haber skorları geçmiş veride güvenilir test edilemez.** Model eğitim verisinde geleceği "biliyor" (look-ahead). Bu bileşenler yalnızca ileriye dönük paper trading ile doğrulanacak.

## Lisans

[MIT](LICENSE)
