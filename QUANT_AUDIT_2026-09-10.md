# Trading sistemi: quant mühendisliği incelemesi

**11 Eylül UTC güncellemesi:** düzeltmeler paper sunucuya dağıtıldı; sabit
emir sahipliği ve süresi dolan broker işleri de düzeltildi. Aşağıdaki
“yerel / dağıtılmadı” ifadeleri ilk denetimin tarihsel durumunu anlatıyor.
Güncel durum ve doğrulama: [dağıtım kaydı](DEPLOYMENT_2026-09-11.md).

**Karar: kârlılık kanıtlanmış değil; mevcut ölçüm ve uygulama sorunları çözülmeden gerçek sermayeye geçişi desteklemiyorum.** Sistemde faydalı bir araştırma ve paper işlem altyapısı var. Fakat çalışan servis, çok sayıda geçen test ve geçmiş bir “confirmation” sonucu, stratejinin uygulanabilir bir avantajı olduğunu göstermiyor.

İnceleme 9 Eylül'de alındığı haliyle sunucu kodu, kayıtlı portföyler, fiyat önbelleği, araştırma sonuçları ve IBKR paper emirleri üzerinde yapıldı; yerel düzeltmeler 10 Eylül'de tamamlandı. **Aşağıdaki performans rakamları 9 Eylül 2026, 13:44 New York anlık görüntüsüdür; bugünün veya gün sonunun rakamları değildir.**

Bu teslimatta 11 uygulama/araştırma dosyası düzeltildi, üç test dosyası eklendi/güncellendi, iki çevrimdışı denetim komutu ve kanıt dosyaları hazırlandı. Sunucuya dağıtım yapılmadı. Emir gönderilmedi veya iptal edilmedi; araştırma sonuçları yeniden derecelendirilmedi, deney başlangıcı sıfırlanmadı.

## 1. Verinin söylediği

Her iç simülasyon hesabının başlangıç sermayesi 3 milyon dolar:

| Portföy | Kayıt başlangıcından getiri | NAV | 18 Ağustos–8 Eylül getirisi |
|---|---:|---:|---:|
| S1 — Quant | −%7,40 | $2.777.924 | −%1,35 |
| S2 — Haber | +%4,17 | $3.125.011 | +%0,93 |
| S3 — LLM | −%4,68 | $2.859.637 | −%2,23 |
| S4 — Birleşik | −%1,86 | $2.944.284 | −%0,90 |
| S5 — 8-K olayları | −%5,75 | $2.827.609 | −%2,57 |

İlk sütun farklı kod/konfigürasyon dönemlerini birleştiriyor. S1–S4 kaydı 18 Haziran, S5 kaydı 26 Temmuz'da başlıyor. Son sütun aynı başlangıç ve tamamlanmış UTC günleri üzerinden hesaplandı; borsa seansı takvimiyle düzeltilmiş bir başarı testi değildir. SPY kaydı 2 Temmuz'da başladığı için onu ilk sütunla doğrudan karşılaştırmak da hatalı olur.

**“Hiçbiri kazanmıyor” doğru değil: S2 artıda. “S2'nin kalıcı avantajı kanıtlandı” da bu veriden çıkmıyor.** Getiriler henüz aşağıdaki muhasebe ve uygulanabilirlik sorunlarından bağımsız doğrulanmış değil.

Sunucunun S3−S1 karşılaştırması günlük −3,85 baz puan, p=0,7555, n=16 bildiriyor. Bu, LLM lehine istatistiksel kanıt sunmuyor; LLM'nin hiçbir koşulda faydalı olamayacağını da kanıtlamıyor. Üstelik gerçekleşen yıllıklandırılmış oynaklık S1'de %2,57, S3'te %8,53: **risk oranı 3,32 kat.** İkisine aynı %10 hedefi yazmak gerçekleşen risklerini eşitlememiş.

Deney sayacı yalnızca hafta sonlarını çıkarıyor. 7 Eylül Labor Day tatili ve henüz bitmemiş 9 Eylül de raporlanan gözlemlere girmiş. İki portföyün ABD hisse seansları açısından, 9 Eylül öncesi tamamlanmış ortak aralık bu nedenle n=16 değil, 14 getiri aralığıdır. Kripto içeren S1 için ayrıca hafta sonu getirilerinin nasıl birleştirileceği açıkça tanımlanmalı. [NYSE 2026 takvimi](https://www.nyse.com/publicdocs/nyse/ICE_NYSE_2026_Yearly_Trading_Calendar.pdf).

## 2. En ciddi araştırma hatası: S5 geçmişte henüz bilinmeyen tepkiyi kullanıyordu

S5'in yönü, açıklama günü ve sonraki işlem gününün toplam anormal getirisi olan AR(0,1)'den seçiliyor. Eski araştırma fonksiyonu ise takvim günü +2'yi getirinin ilk günü sayıyordu.

Somut karşı örnek: açıklama cuma günü; yönü belirleyen fiyat hareketi pazartesi kapanışında tamamlanıyor. Eski kod pazartesinin kazancını, ancak pazartesi kapanışında seçilebilecek pozisyona yazıyordu. Dört hisseli sentetik örnekte sonraki fiyatlar hiç değişmediği halde %10 kazanç oluştu. Düzeltilmiş kodda bu hayali kazanç sıfır.

Mevcut arşivdeki **1.529 olay ve 752 günlük aynı veri**, aynı 2/10 günlük parametreler ve aynı yaklaşık maliyet modeliyle:

| Zamanlama | Sharpe | Birikimli getiri |
|---|---:|---:|
| Eski fonksiyon | 1,52 | %78,91 |
| Bilgi elde edildikten sonraki getiri | 0,65 | %26,04 |

Bu, 2023-07-05–2026-07-02 fiyat önbelleğinde yapılmış bir hata teşhisidir. **N0021'in 2021–2023 lockbox testini yeniden çalıştırmış değilim.** Dolayısıyla “eski lockbox Sharpe'ı kesin olarak 1,52'den 0,65'e düştü” sonucu çıkarılamaz. Çıkarılabilecek sonuç, eski fonksiyonla üretilen güven iddialarının tekrar doğrulanması gerektiğidir.

Düzeltme, AR(0,1)'in iki gözlemini de zorunlu tutuyor, sinyalin bilindiği tarihi kaydediyor ve pozisyonun yalnızca sonraki barın getirisini kazanmasına izin veriyor. Ayrıca SEC arşiv sayfaları artık izleniyor; yalnızca “recent” verisi eski dönem için eksiksiz örneklem sağlamaz. [SEC API açıklaması](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

**Açık kalan:** düzeltilmiş araştırma hâlâ birim ağırlıklar ve yaklaşık maliyet kullanıyor. Canlı S5'in kovaryansla ölçeklenmiş pozisyonlarını, sınırlarını, gerçekleşen devir hızını, açığa satış maliyetini ve emir gecikmesini birebir üretmiyor. “Canlı stratejinin aynı backtesti” ifadesi kaldırıldı. Veri, geçmişteki evren üyeliğini ve sonradan kaybolan şirketleri de eksiksiz temsil etmiyor.

## 3. Uygulanabilirlik: kripto pozisyonları seçilen piyasanın kapasitesini aşıyor

Konfigürasyonda pozisyon/ADV sınırı %10. Fakat son portföy ölçeklemesinden sonra bu sınır tüm canlı hesaplarda uygulanmıyor. ADV, ilgili işlem yerinin 20 günlük ortalama günlük dolar hacmi:

| Pozisyon | Dolar büyüklüğü | Kullanılan ADV | Pozisyon/ADV |
|---|---:|---:|---:|
| S1 AVAX/USDT | $35.128 | $33.684 | **%104,3** |
| S1 LINK/USDT | $35.128 | $129.567 | **%27,1** |
| S4 AVAX/USDT | $19.698 | $33.684 | **%58,5** |
| S4 LINK/USDT | $19.698 | $129.567 | **%15,2** |

Örneğin S1 AVAX pozisyonu, yazılmış sınırın yaklaşık 10,4 katı. Bir varlığın dünya genelinde likit olması, Binance US'teki ilgili paritenin bu büyüklüğü taşıyabildiği anlamına gelmiyor. Ayrıca “spot only” tasarımına rağmen S1 ve S4 negatif kripto ağırlıkları tutuyor; spot envanter olmadan bunlar uygulanabilir açığa satışlar değil.

Yerel eski PaperBroker'daki eksik ADV'nin sınırsız emir anlamına gelmesi düzeltildi. **Bu modülün düzelmesi, ayrı hesaplama yapan runtime'daki yukarıdaki ihlalleri çözmüyor.** Son ölçeklemeden sonra pozisyon ve emir kapasitesi birlikte kontrol edilmeli; spot portföy yalnızca uygulanabilir envanter taşımalı. Bunlar mevcut deneyin portföyünü değiştireceğinden, ayrı sürüm ve ölçüm dönemi gerektiriyor.

Maliyet sayaçları S1'de $111.920, S4'te $64.911 gösteriyor. Başlangıç zamanları kaydedilmediği için bunları tüm tarihsel zararın açıklaması veya yıllık maliyet tahmini olarak kullanmıyorum. Varlık bazında maliyet dökümü olmadan “zararın tamamı kripto maliyeti” demek de mümkün değil. Maliyet katsayılarını düşürerek kâğıt üzerinde kâr yaratmak uygun bir çözüm olmaz.

## 4. IBKR paper uygulaması: görünmeyen emirler ve yüksek devir

9 Eylül'de salt okunur bir IBKR bağlantısıyla yeni istemcinin yerel açık emir listesi **0**, hesap genelindeki sorgu **3 aktif emir** döndürdü. Bunlar başka client ID'ye aitti. Bu, eski iptal yönteminin “açık emir yok” sonucuna güvenilemeyeceğini doğrudan gösteriyor.

Yerel düzeltme hesap genelini sorguluyor; eski emirler aynı hesapta başka istemciye aitse, iptal başarısızsa veya iptal onayı sonrası hâlâ aktif emir varsa yeni emir neslini durduruyor. Diğer hesapların emirlerine dokunmuyor. IBKR'de hesap genelini görmek emir sahipliğini otomatik devretmez. [IBKR emir değiştirme ve sahiplik kuralları](https://www.interactivebrokers.com/docs/tws-api/doc/orders/modifying-orders).

**Dağıtım sınırı:** runtime hâlâ istemci kimliklerini döndürüyor. Bu koruma devreye alındığında, eski emirler dolana veya doğru istemciyle uzlaştırılana kadar mirror durabilir. Bu davranış, riskin yanlışlıkla iki kez açılmasını engeller; yürütme mimarisinin bütünüyle onarıldığı anlamına gelmez. Sabit bir emir istemcisi ve açık emir sahipliği politikası gerekiyor.

Ek açık sorun: zaman aşımına uğrayan broker işi arka planda bırakılıyor. Thread'in zaman aşımına uğraması gerçekten durduğu anlamına gelmiyor; gecikmiş işin sonradan eski hedefle emir göndermesi mümkün. İptal edilebilir işler veya tek sahipli, seri emir yürütme gerekli.

8 Eylül kayıtlarında 887 gerçekleşme, $3,75 milyon brüt işlem ve %76,35 karşılıklı alış/satış oranı var. Oran, sembol bazında net yönlü işlem tutarı ile brüt işlem arasındaki farktan hesaplanıyor; tümünün gereksiz olduğunu veya üç görünmeyen emrin tümünü açıkladığını kanıtlamıyor. Ancak stratejinin fiyat avantajının devir maliyetine yetip yetmediğini sorgulatacak büyüklükte.

21.401 kaydın 21.385'inde komisyon sıfır. Kayıt kodu ilk gerçekleşmeyi saklayıp sonradan gelen komisyon bilgisini aynı kayıt üzerinde zenginleştirmediğinden, sıfırlar eksik/asenkron bilgi olabilir. Bunlardan “işlem bedava” sonucu çıkarılamaz. Broker ekstresiyle uzlaşma gerekli. IBKR hesabı iç simülasyonların 3 milyon dolarlık tüm portföyünü taşımıyor; yalnızca hisse alt kümesini aynalıyor. İki NAV'ı aynı hesap gibi karşılaştırmamak gerekiyor. [IBKR paper işlem sınırlamaları](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/paper-trading).

## 5. Risk ve canlı muhasebede açık kalanlar

**Kovaryans örneklemi:** 505 sembollü, 661 tarihli birleşik fiyat panelinde tüm sütunlarda veri isteyen getiri temizliği yalnızca **38 ortak günlük gözlem** bırakıyor. Yeni listelenmiş isimler ve hisse/kripto takvimlerinin birleşimi bunda etkili. Bu, %10 hedef ölçeklemesinin geniş bir örneklem üzerinde kalibre edildiği varsayımını bozuyor. Yaşlı evrenin ortak seansları ve yeni varlıkların ayrı kabul kuralları tanımlanmalı; hedefi büyütmek çözüm değil.

**Canlı ağırlık muhasebesi:** runtime pozisyonları pay adedi ve nakit yerine ağırlıklarla taşıyor; fiyat değişince elde tutulan ağırlıkların sürüklenmesini tam olarak taşımıyor. Bu da işlem yapılmadığı halde sabit ağırlıklı yeniden dengeleme varsayımı yaratabilir. Genel backtestte bu sorun düzeltildi; runtime'ın ayrı gölge muhasebesi için henüz düzeltilmedi. Fiyatı kaybolan varlıkların değeri, tekrar görünme hareketi, bölünmeler ve temettüler de ortak muhasebe politikasıyla çözülmeli. Son 200 saklanan veri olayının 193'ü STALE-MARK: bu, olay tamponudur; tüm çalışma süresinin oranı değildir.

**Borçlanma:** runtime takvim günleri için yıllık oranı 252'ye bölüyor ve kripto eksi ağırlıklarına da hisse oranını uyguluyor. Yıl boyunca her gün işletilirse 365/252 çarpanı oluşur. Tek bir gün sayımı politikası, gerçek kısa satış bulunabilirliği, temettü yükümlülükleri ve nakit faizi gerekiyor. Backtestte ücretsiz açığa satış kaldırıldı; kullanılan mevcut /252 bar sözleşmesi gerçek broker finansmanının tam modeli değil.

**Governor:** güncel fiyat kaybı eski NAV üzerinden bir sonraki tick'e kadar görünmüyordu; bu düzeltildi. Bununla birlikte kontrol hâlâ işlem maliyeti sonrası nihai NAV'dan önce yapılır, kovaryans belleği yeniden başlatmada kaybolabilir ve “riski yarıya indir” kararının yinelenen tick'lerde nasıl uygulanacağı netleştirilmelidir. Bir düzeltme tüm risk denetimini kanıtlamaz.

**S3 girdileri:** briefing'e mevcut pozisyonlar, S3 engine'e mevcut ağırlıklar aktarılmıyor; devir sınırı fiilî portföy yerine boş başlangıca göre hesaplanabiliyor. Aday listesi sırasız set üzerinden 60'a kesiliyor; süreçler arasında seçim değişebilir. Mevcut pozisyonları önceleyen deterministik seçim, gerçek NAV ve mevcut ağırlık aktarımı sonraki portföy sürümünde gerekli. Bu sürümde PM modeli veya portföy karar kuralı değiştirilmedi.

## 6. Uygulanan yerel düzeltmeler ve kanıtları

| Alan | Düzeltme | Davranış kontrolü |
|---|---|---|
| Genel backtest | Pay değerlerini fiyatla taşıyan, güncel NAV ve yeniden dengeleme sürüklenmesine göre maliyet alan muhasebe; warmup ölçümden çıkarıldı; borrow eklendi | %50 hisse/%50 nakit, 100→110→100 fiyat yolu sahte kâr üretmiyor; değişmeyen hedefte gerçek yeniden dengeleme ücretli |
| Fiyat/likidite doğrulama | Backtest ve eski Ledger eksik açık pozisyon fiyatını reddediyor; PaperBroker geçersiz ADV'yi reddediyor, istenen miktarı kırpmadan önce saklıyor | Eksik/NaN/sıfır fiyat ve geçersiz ADV örnekleri |
| DSR | Tek denemede tanımsız uç-değer formülü yerine sıfır Sharpe'a karşı PSR | Zarar eden tek deneme artık DSR=1 üretmiyor |
| HAC | Bartlett otokovaryanslarında ortak n paydası | Bağımsız kernel matrisinden hesaplanan standart hatayla eşitlik |
| S5 araştırması | Gözlemlenmiş tam tepki ve sonraki bar getirisi; SEC tarihsel sayfalar | Cuma/pazartesi karşı örneği ve sahte SEC arşivi |
| Araştırma kimliği | İçe aktarılan matematik, sinyal, maliyet ve konfigürasyon kodu da sürüm hash'ine dahil | Metrik kodu değişince araştırma kimliği değişiyor |
| Veri sağlayıcı | Kabul edilmiş fiyat temeli son gözlenen sağlayıcıdan ayrı saklanıyor; boş portföy de kilidi aşamıyor | İki ardışık fallback tick'i ve yeniden başlatma |
| Risk durdurma | Yeni riskten önce bu tick'in S4 fiyat kaybını değerlendiriyor | Bu tick'teki %5 NAV kaybı aynı tick'te durduruyor |
| LLM bütçesi | Ücretli çağrının geçersiz/fallback cevabı da sayılıyor; kalan günlük scorer kotası batch içinde uygulanıyor | 399/400 kullanımdayken yalnızca bir ek çağrı; ücretli geçersiz PM cevabının tokenları kaybolmuyor |
| IBKR | Hesap genelinde eski emirler bitmeden yeni nesil yok; sorgu/iptal/onay hatasında duruyor | Başka client'ın emri, iptal hatası, eksik onay, diğer hesap ve dry-run örnekleri |
| Sunum | Risk eşit değilken dashboard ve digest üstünlük iddiası göstermiyor | Risk eşitsiz/bilinmiyor örnekleri; JavaScript sözdizimi kontrolü |

DSR'nin tek deneme hatası gerçektir; mevcut araştırma kayıtlarındaki çoklu-deneme sonuçlarının tamamını açıklamaz. HAC düzeltmesi de geçmiş karar eşiklerini değiştirmez. [DSR özgün çalışma](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf), [HAC referansı](https://www.statsmodels.org/dev/generated/statsmodels.stats.sandwich_covariance.cov_hac.html).

**Doğrulama:** orijinal sunucu kopyasında 608 test geçti, 1 atlandı. Bağımsız karşı örnekler önce hataları yeniden üretti. Düzeltmeler sonrasında 636 test geçti, 1 atlandı; mevcut engineer bağlam boyutu uyarısı kaldı. Sekiz hedefli mutasyonun tamamı testlerce yakalandı; S5 zamanlaması büyük, küçük ve aynı sabiti koruyan üç bozulmayla denendi. Bu, tüm depo için eksiksiz mutasyon taraması veya gerçek brokerda iptal davranışı testi değildir. Testler kimlik bilgileri içermeyen izole kopyada çalıştırıldı.

## 7. Bundan sonra izlenecek sıra

1. **Ölçümü düzelt:** tek pay/nakit/kurumsal olay muhasebesi, tamamlanmış seanslar ve karar anında gerçekten mevcut veriler. Aynı girdinin backtest ve paper hesapta aynı pozisyon/P&L'yi ürettiği günlük replay olmadan yeni Sharpe sonucu kabul etme.
2. **Portföyü uygulanabilir yap:** son ölçeklemeden sonra piyasa bazında ADV ve emir kapasitesi; spot envanter sınırı; kısa satış bulunabilirliği; açık emir sahipliği ve zaman aşımı uzlaşması. Eski ölçümü koruyarak yeni sürüm başlat; mevcut saati sessizce sıfırlama.
3. **Maliyeti ölç:** karar ve emir anı fiyatı, spread, gerçekleşme, gecikme, komisyon ve finansmanı ayrı kaydet. Önceki günün kapanışıyla fill farkını doğrudan “slippage” sayma. Kötü günleri çıkararak maliyet modelini iyileştirmiş görünme.
4. **Sinyali yeniden değerlendir:** düzeltilmiş motor üzerinde aynı kuralları önce yeniden üret; sonra daha önce seçilmemiş gelecek dönemde test et. Ortak tarih/riskte nakit, piyasa maruziyeti ve basit deterministik kontrolle karşılaştır. S2'yi aday olarak izle; kısa süreli artı getiriyi strateji seçimi için yeterli sayma.
5. **LLM'yi en son optimize et:** önce mevcut pozisyonları ve devir maliyetini doğru göster. Daha pahalı modele geçmek için net performans kanıtı yok. “60 gün doldu” tek başına kârlılık veya düzgün deney tasarımı sağlamaz.

Gerçek portföy düzeltmeleri performans serisini değiştirecek. Konfigürasyon dondurulmuş mevcut dönemle yeni sürümü karıştırmadan, sürüm ve başlangıç sermayesi açık yeni bir değerlendirme yapılmalı. Eski araştırma kayıtları silinmemeli; yanlış yorumlar CORRECTIONS kaydında açıklanmalı.

## 8. Kanıtlar ve yeniden üretim

- [Sayısal anlık görüntü analizi](research/audit_20260909/snapshot_analysis.json)
- [S5 zamanlama karşılaştırması](research/audit_20260909/event_timing_audit.json)
- [Mutasyon sonuçları](research/audit_20260909/mutations.json)
- [Test çıktısı](research/audit_20260909/verification.txt)
- [Kaynak ve veri SHA-256 kimlikleri](research/audit_20260909/manifest.json)
- [Bağımsız regresyon testleri](tests/test_quant_audit.py)
- [Araştırma kaydına eklenen düzeltme](research/CORRECTIONS.md)

Ham kayıtlar, git tarafından dışlanan data/quant_audit_20260909 altında saklandı; kaynak arşivine .env ve broker kimlik bilgileri alınmadı. Yerel başlangıç a1a9bf3, sunucu başlangıcı c2d8d9c. Değiştirilen dosyaların başlangıç içeriklerinin iki ortamda aynı olduğu kontrol edildi.

Trading klasöründen, yalnızca bu güvenilir yerel arşivlerle yeniden hesaplama:

    venv/bin/python scripts/quant_audit_snapshot.py --evidence-dir data/quant_audit_20260909/evidence --output /tmp/quant_snapshot.json
    venv/bin/python scripts/quant_audit_event_timing.py --original-source data/quant_audit_20260909/original_source.tgz --output /tmp/quant_event_timing.json

Bu komutlar broker bağlantısı kurmaz, veri indirmez veya araştırma siciline yeni deneme yazmaz. Sayısal dosyadaki “completed weekdays” alanları borsa takvimiyle eş anlamlı değildir; mirror gross_over_nav alanı ilgili günün NAV'ı yerine anlık görüntü NAV'ını kullanır. Bu nedenle rapordaki yorumlarda bu iki alan güçlü karşılaştırma kanıtı olarak kullanılmadı.
