# İki paper bot — 19 Eylül 2026 incelemesi

## Karar

RSI + Mavilim'de üç doğrulanmış veri/emir hatasını düzelt; XRP'yi geçmiş
veriyi değiştirmeden denetim kaydıyla kurtar. Ana botta muhasebe veya broker
sınırı ihlali bulunmadı. Kapalı otomatik mühendis modülündeki bağlam taşmasını
düzelt, modülü kapalı tut. Bu kısa sonuç penceresine göre sinyal, kaldıraç,
stop veya strateji ağırlığı optimize etme.

İnceleme kapsamı: çalışan sunucunun state/SQLite kayıtları, bütün açık
pozisyonlar, RSI işlemlerinin nakit/adet/finansman uzlaşması, 20 kripto
önbelleğinin sağlayıcı OHLC karşılaştırması, broker işlem uzlaşması, sinyal
ve emir yolları, test paketi, arıza senaryoları ve servis sağlığı. Bu kapsam
tüm kodda başka hata kalmadığı iddiası değildir.

## RSI + Mavilim: doğrulanmış düzeltmeler

| Bulgu | Etki | Düzeltme |
|---|---|---|
| Kraken yanıtının son satırı yalnızca saate göre kapanmış kabul edilebiliyordu | Mum devrinde kesinleşmemiş fiyat arşive girip sonraki revizyonda varlığı kilitleyebilir | Sağlayıcının son, kesinleşmemiş satırı her zaman dışarıda; eksik kapanışta yeniden dene; arşiv öncesi OHLC/zaman doğrulaması |
| Hisse sinyal geçmişi hatası dakika verisi işlemeyi de durduruyordu | Geçerli yürütme verisi varken stop kontrolü kaçabilir | Sinyal geçmişi ve dakika yürütme yolları ayrı; corporate action verisi hâlâ zorunlu |
| Kripto gösterge çıkışı likidite/fiyat hatasında kalıcı değildi | Sonraki mumda sinyal toparlanırsa satış kararı unutulabilir | Çıkış niyeti işlem tamamlanana kadar SQLite'ta kalır; restart ve veri kesintisini aşar |

XRP'nin 19 Eylül 04:00–08:00 UTC mumunda kapanış 1,40999'dan 1,41000'a
değişmiş; yaklaşık 0,071 baz puan. İlk veri mum devrinden bir saniyeden kısa
süre sonra alınmış. Sınırdaki kesinleşmemiş mum sorunu makul açıklama,
fakat orijinal ham yanıtın son satırı saklanmadığından kesin kök neden kanıtı
yok. 20 kriptonun taramasında tek OHLC revizyonu buydu.

Her iki fiyat da aynı al/sat kararını veriyor; XRP'de işlem, pozisyon veya
bekleyen emir yok. Yalnızca bu kesin fiyat farkına izin veren kayıtlı
inceleme uygulanır. İlk gözlenen fiyatlar, arşivlenmiş sinyaller, para ve
işlem geçmişi korunur. Kaçan mumlara geriye dönük işlem yazılmaz. Başka
bir revizyon yeniden engellenir. Araç süreç kilidi, tam önbellek hash'i,
pozisyonsuz varlık, aynı karar ve veritabanı/önbellek yedeği şartlarını arar.

[Kraken'in OHLC sözleşmesi](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
son satırın kesinleşmemiş olduğunu açıkça belirtir.

11 gerçekleşme ve 31 finansman kaydı bağımsız nakit tekrar hesabında
$0,000000001 toleransıyla uzlaştı. Adet farkı sıfır. Masraflar gerçekleşme
ücretleri + finansmanla uzlaşıyor. Hisse emir kararları hedef piyasa
dakikasından önce alınmış.

15:23 New York görüntüsü: başlangıç $250.000, NAV yaklaşık $254.008;
kapalı işlemler −$538, açık işlemler +$4.546. Bunun yaklaşık $3.483'ü UNI.
Üç kapanmış işlem ve tek açık kazanca dayanan sonuç, kalıcı üstünlük
göstermiyor. Yeni bir trailing stop veya kâr hedefini bu sonuca uydurmak
deneyi değiştirirdi. 2x pozisyonlarda finansman, ilk işlem tutarının tam
bir günde %0,24'ü; stratejinin tutma süresi bu maliyeti karşılamalı.

## Ana bot: zarar sadece komisyon değil

16 Eylül 02:41 UTC muhasebe sınırıyla eşleşen gerçek dağıtım öncesi yedek
(tick 2140), 19 Eylül 18:35 UTC state'iyle karşılaştırıldı. İlk sermayeden
bugüne karışık sürüm sonuçları bu tabloda kullanılmıyor. Model maliyetleri
fiyat etkisi, spread, komisyon ve ödünç maliyetini içerir; IBKR faturası değildir.

| Kitap | Net değişim | Modellenen maliyet | Maliyet eklenince fiyat kaynaklı değişim | Birikimli L1 turnover |
|---|---:|---:|---:|---:|
| S1 | −$10.115,79 | $2.440,34 | −$7.675,45 | 3,354 |
| S2 | −$31.330,27 | $2.150,06 | −$29.180,21 | 4,347 |
| S3 | −$18.522,37 | $3.963,44 | −$14.558,93 | 8,359 |
| S4 | −$23.135,45 | $3.144,77 | −$19.990,68 | 5,821 |
| S5 | +$4.750,84 | $698,27 | +$5.449,11 | 1,242 |

Maliyet ekleme ayrıştırması aynı gerçekleşen yolun muhasebe ayrıştırmasıdır;
masrafsız yeniden çalıştırılmış portföy karşılaştırması değildir. L1 turnover,
alış ve satış ağırlık değişimlerinin mutlak toplamıdır; tek yönlü işlem
oranı olarak yorumlanmamalı.

S4 kaybının yaklaşık %13,6'sı modellenen maliyet. Kalan yaklaşık $20 bin
fiyat/pozisyon seçiminden geliyor. Dolayısıyla yalnızca komisyonu azaltmak
bu gözlemde zararı kâra çevirmiyor. S3 ve S4 işlem sıklığı yüksek; sonraki
araştırma konusu işlem değişikliğinin tahmini maliyetini aşmasını isteyen
bir eşik olabilir. Bu üç günlük sonuçla eşik seçilip canlıya alınmadı.

Beş kitapta nakit + pozisyon − NAV ve ağırlık/adet uzlaşması temiz;
brüt/tek isim/ADV sınırı ihlali, eksik işaretli pozisyon veya spot kripto
short yok. Dört günlük IBKR gerçekleşme uzlaşmasında mevcut hacim ve
karşı yön işlem kontrolleri hata vermedi. Cumartesi broker günlük sayaç
günüyle cuma işlem günü eşleşmediği için son günlük adet karşılaştırması
"not comparable"; bu sıfır fark kanıtı olarak sunulmadı.

## Ana bot: küçük bakım düzeltmesi

Tam test paketi otomatik mühendis bağlamının büyüyen kaynak ağacında iki
watchdog testini düşürdüğünü yakaladı. 1,2 milyon karakter tavanına
ayırıcı/metin başlıkları da doğru dahil edilmiyordu. Tam metin 1,35 milyon
karakterlik sınırla kontrol edilir; sığmazsa model çağrısından önce açık
hata verir, kaynak dosyaları sessizce düşürmez. Bu değişiklik strateji veya
emir yürütmesini değiştirmez. Otomatik mühendis ve araştırma timer'ı kapalı.

## Devam eden sınırlar ve kararlar

- Ana botun iç kitapları günlük fiyat işaretleriyle karar anı işlem modeli
  kullanıyor; borsa kapanışında/hafta sonunda değişen hisse hedefleri gerçek
  bir gelecekteki seans fiyatıyla doldurulan bağımsız emir defteri değil.
  IBKR kopyasında seans kontrolü var, fakat yalnızca S4'ün hisse dilimini
  ayrı sermayeyle ölçüyor. İç kitap sonuçlarını birebir uygulanabilir getiri
  saymamak gerekir. Sonraki büyük mühendislik işi budur.
- Ana botta tam temettü/corporate-action muhasebesi, takvim bazlı ödünç
  maliyeti ve gerçek borrow availability/fee verisi hâlâ eksik. Bilinen
  /252 günlük ödünç maliyeti sınırlaması bu sürümde değişmedi.
- Haiku kullanım token'ları ölçülüyor ama fiyat alanı null; dashboard'daki
  fiyatlanmış LLM doları tam model gideri değildir. Mevcut çağrı sayısı
  tavanları aktif. Gerçek fatura uzlaşması yapılmadı.
- RSI hisse yürütmesi 15 dakika gecikmeli OHLC simülasyonu; kripto margin
  modeli gerçek broker tasfiye/borçlanma sisteminin tam kopyası değil.
- Eski S3 üstünlük deneyi sonuçsuz kapalı. Bu inceleme yeni bir 60 günlük
  vaat, reset, başarı ilanı veya risk artışı başlatmıyor.

## Test kayıtları

Ana botta düzeltme öncesi yerel test: 654 geçti, 1 atlandı, bağlamla ilgili
2 hata. İzole kaynak kopyasında 655 geçti, 1 atlandı, aynı dosya düşürme
sorunu nedeniyle 1 hata. Düzeltme sonrası: **657 geçti, 1 atlandı**.
RSI önce 79, sonra **96 test** geçti. Dört kasıtlı RSI regresyonu da
testlerle yakalandı. Sunucu testleri, dağıtım hash'leri ve son sağlık
durumu ayrı dağıtım kaydına eklenir.

Dağıtım tamamlandı: RSI kod sınırı **19 Eylül 19:35:58 UTC**. İlk yeni
döngüde 70/70 veri sağlıklı, XRP tekrar aktif, açık beş pozisyon ve 11
gerçekleşme korunuyor. Dashboard HTTP 200; iki bot ve broker servisi aktif.
Ana bot servisi yeniden başlatılmadı; kapalı mühendis modülüne kaynak/test
yaması uygulandı. Sunucuda ana bot **657 geçti, 1 atlandı**, RSI **96 geçti**.
Public ana depo testinde **656 geçti, 2 atlandı**; ek atlama public depoda
özel araştırma kuyruğu bulunmamasından kaynaklanıyor. Kaynak hash'leri,
sağlık ve test kanıtı `research/review_20260919/verification.json` içinde.
