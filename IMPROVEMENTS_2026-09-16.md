# Paper bot iyileştirmeleri — 16 Eylül 2026 UTC

Karar: pasif beklemek yerine muhasebe ve uygulanabilirlik hatalarını düzeltmek.
Sinyal parametreleri veya model seçimi geçmiş sonuca bakılarak optimize edilmedi.

## Değişiklikler

- **Adet/nakit muhasebesi:** eski ağırlık × eski NAV pozisyon tutarı, yeni
  fiyatla değerlenir. Beklerken adet sabit, ağırlık değişkendir. Nakit + işaretli
  pozisyon değerleri NAV ile uzlaşır. Süreç yeniden başlatıldığında da korunur.
- **Maliyet:** işlem tutarı, mevcut işaretli pozisyon ile işlem sonrası NAV'a
  göre hedef pozisyon arasındaki farktır. Maliyet NAV ile birlikte çözülür;
  borçlanma bedeli nakitten düşer. Masraf ödemek ücretsiz pozisyon küçültmez.
- **Son portföy sınırları:** mevcut %5 pozisyon, 1× brüt ve %10 ADV sınırları
  volatilite ölçeklemesinden sonra uygulanır. Spot kriptoda negatif pozisyon
  engellenir. ADV bilinmiyorsa yeni risk eklenmez; azaltma mümkündür.
- **S3:** güncel işaretli pozisyonlarını ve kullanılan brüt risk bütçesini
  görür; likidite hesabı başlangıç sermayesi yerine güncel NAV kullanır.
  Briefing sırası deterministiktir, fiyatlanabilir mevcut pozisyonların tamamı
  görünür. Mevcut 1× turnover sınırı son ölçeklemeden sonra da uygulanır;
  zorunlu risk azaltımları bu sınırın önüne geçer.
- **Düşüş kontrolü:** aynı drawdown koşulunda bekleyen S4 her saat yeniden
  yarıya indirilmez. Önceki azaltım yeniden başlatmalar arasında saklanır.
  Bekleme tick'inde toparlanma kendiliğinden risk artırmaz.
- **Eksik fiyat:** pozisyon yok sayılmaz veya hayali satış yapılmaz. Değeri
  dondurulur, nakit yaratamaz; geçerli fiyat gelince uzlaştırılır. Risk azaltma
  emirleri paper mirror için sonraki açık seansa taşınır.

## Ölçüm kararı

Muhasebe ve portföy davranışı değiştiği için eski ve yeni dönemi birleştirerek
istatistiksel üstünlük ilan etmek geçersizdir. Eski S3 karşılaştırması
**tamamlanmamış/sonuçsuz** olarak kapatıldı; kanıtlanmış üstünlük yok.
Başlangıç tarihi, iki eski reset kaydı ve geçmiş seriler korunur. Yeni sürüm
sınırı NAV başlangıçlarıyla kaydedilir; karma dönem dashboard'da
“VERSION CHANGE · DIAGNOSTIC ONLY” olarak görünür. Yeni bir 60 günlük
başarı vaadi veya otomatik deney reseti yapılmadı. Karar: DECISIONS §8.

## Gerçek portföy kopyasında geçiş denemesi

Kaynak: tick 2140, 16 Eylül 01:57 UTC. Fiyatlar sabit tutuldu; veri, model ve
broker çağrısı yapılmadı. Bu yalnızca düzeltmeye geçişin maliyet tahminidir.

| Kitap | Önce ADV ihlali | Sonra | Modellenen işlem maliyeti |
|---|---:|---:|---:|
| S1 | 3 | 0 | $397,60 |
| S2 | 0 | 0 | $0 |
| S3 | 0 | 0 | $0 |
| S4 | 2 | 0 | $130,66 |
| S5 | 0 | 0 | $0 |

Tüm kitaplarda spot short sayısı sıfır, nakit/pozisyon/NAV uzlaşma farkı
$0,000001 altında. Bu tablo gerçekleşmiş broker maliyeti veya kâr tahmini değil.
Ham hesap kayıtları public depoya konulmadı; türetilmiş kanıtlar
`research/audit_20260916/` içinde.

## Sınırlar

Bunlar kârlılık kanıtı değildir. S3'ün önceki zararını silmez. İç muhasebe hâlâ
karar anında modellenmiş işlem kabul eder; IBKR ise yalnızca S4'ün ilk 100
hisselik dilimini ayrı paper hesapta yürütür. Fiyatlar split-adjusted fiyat
getirisidir; tam temettü/corporate-action muhasebesi değildir. Eksik/kopuk
fiyat karantinasının eski rebase politikası korunur; gerçek aşırı hareketler
insan uzlaştırması gerektirebilir. Borçlanma için eski günlük /252 yaklaşımı,
ADV'nin son ağır veri alımından kalması ve hafta içi sayan seans takvimi
ayrı sınırlamalar olarak duruyor. Likidite sınırı pozisyon/ADV sınırıdır;
broker emirlerinin gerçek hacme katılımını modelleyen bir yürütme algoritması
bu değişiklikte yok. Covariance örneklemi ve risk eşitliği araştırması sürüyor.

## Test ve dağıtım sonucu

| Kontrol | Sonuç |
|---|---|
| Eski sunucu sürümü, izole test | 641 geçti, 1 atlandı |
| Yeni sürüm, yerel izole test | 656 geçti, 1 atlandı |
| Yeni sürüm, sunucuda izole test | 656 geçti, 1 atlandı |
| GitHub sürümü | 655 geçti, 2 atlandı |
| Kasıtlı hata denemeleri | 8/8 yakalandı |
| Dağıtım sonrası tick | 2140 → 2141 |
| Muhasebe sürümü | self_financing_v2 |
| Son portföylerde ADV ihlali / spot short | 0 / 0 |
| Nakit + pozisyon − NAV farkı | Her kitapta $0,000001 altında |
| Sağlayıcı / fiyat temeli | polygon / polygon |
| Paper servis / IB Gateway | active / active |
| Dashboard / tick hatası / halt | HTTP 200 / 0 / yok |
| Araştırma timer'ı | disabled |

Kod, **16 Eylül 02:41 UTC / 15 Eylül 22:41 New York** saatinde devreye alındı.
Sunucu kod commit'i: `b5bda8a08ae39c86df2a55f5c7930c5cc18343f8`.
İlk tick tamamlandı; broker yenilemesi 02:42 UTC'de başarılı, paper NAV
$936.679,59. Bu tutar yeni sürümün kâr ettiğini göstermez. İlk tick yeni bir
PM kararı üretmedi; S3 briefing değişikliği kontrollü entegrasyon testinde
kanıtlandı, sonraki doğal karar tick'inde kullanılacak.

Gerçek ilk tick'te modellenen geçiş işlem bedeli S1'de $403,67, S4'te $132,76
oldu; sabit-fiyat denemesinden fark, arada değişen kripto fiyatlarından geliyor.
Başlangıç tarihi 2026-08-18 ve iki reset kaydı korundu. Önceki kod/state sunucuda
yedeklendi. Dağıtılan dosyaların hash'leri test edilen adayla eşleşiyor.

Ek atlama public araştırma kuyruğunda bekleyen kayıt bulunmamasından
kaynaklanıyor. Engineer bağlam boyutu için mevcut uyarı sürüyor; otomatik
engineer kapalı. Test paketinin ilk taşınmasında `core/data` dizini arşiv
filtresinden çıkmıştı; gerçek sunucu kaynağıyla tamamlanıp testler yeniden
çalıştırıldı. Production dosyaları yalnızca başarılı doğrulamadan sonra yamalandı.
