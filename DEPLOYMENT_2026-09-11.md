# Paper bot dağıtımı — 11 Eylül 2026 UTC

Dağıtım 11 Eylül 02:26 UTC'de, New York saatiyle 10 Eylül 22:26'da yapıldı.
Sunucu kod sürümü: **aa3f4a107030b413f5372d1993a0f949e0a2d01a**.

## Devreye alınanlar

[Quant incelemesindeki](QUANT_AUDIT_2026-09-10.md) hesaplama, veri sağlayıcı,
güncel fiyatla risk değerlendirmesi, model bütçesi, emir iptal doğrulaması ve
dashboard düzeltmeleri paper servise uygulandı.

Dağıtım öncesinde emir yürütmesi de tamamlandı:

- Emir yazan istemci artık sabit: client ID 18. Salt okunur yenileme ayrı
  istemci aralığında kalıyor.
- Her senkron IB isteğine 15 saniyelik sınır eklendi.
- Süresi dolan broker işi sonraki emirleri gönderemez; önceki iş bitmeden
  ikinci broker işi başlatılmaz. Gecikmiş rapor yeni runtime durumuna yazılmaz.
- Kontrat sorgusundan dönüşte ve her emir öncesinde süre kontrol edilir;
  emir açıkça doğrulanmış paper hesabına atanır.
- Gönderilmiş bir emir zaman aşımıyla geri alınmış sayılmaz. Sonraki sync
  açık emirleri ve gerçekleşmiş pozisyonları tekrar uzlaştırır.

Bu ekler, ilk raporda açık bırakılmış sabit emir sahipliği ve geç gelen
iş sorunlarını ele alır. Canlı pay/nakit muhasebesi, nihai likidite/spot
sınırları, seans takvimi ve S3 mevcut pozisyon girdileri hâlâ ayrı çalışmadır.

## Doğrulama

| Kontrol | Sonuç |
|---|---|
| Sunucudaki orijinal sürüm, izole test | 608 geçti, 1 atlandı |
| Sunucudaki yeni sürüm, izole test | 641 geçti, 1 atlandı |
| Yerel yeni sürüm, mutasyonlar geri alındıktan sonra | 641 geçti, 1 atlandı |
| GitHub için hazırlanan sürüm | 640 geçti, 2 atlandı |
| İlk denetimin hedefli mutasyonları | 8/8 yakalandı |
| Yeni emir yürütme mutasyonları | 3/3 yakalandı |
| Paper broker dry-run | İstemci 18 ile bağlandı; 101 emir planlandı, 0 gönderildi; API hatası yok |
| Geçiş anındaki açık emirler | 0 |
| Yeniden başlatma sonrası tick | 2022 → 2023 |
| Veri sağlayıcısı / kabul edilmiş fiyat temeli | polygon / polygon |
| Risk durdurma kilidi | Yok |
| Broker yenilemesi | Başarılı; 02:28:26 UTC |
| Dashboard HTTP | 200 |
| paper-trader / ib-gateway | İkisi de active |
| fund-research.timer | disabled |

Sunucu ve yerel testlerde mevcut engineer bağlam boyutu uyarısı var; dosya
atlanması veya test hatası yok. GitHub sürümündeki ek atlama, public araştırma
kuyruğunda bekleyen kayıt olmamasından kaynaklanıyor; canlı kayıt kopyalanmadı.

İlk tamamlanan tick 133,6 saniye sürdü; yeniden dengeleme gerektirmeyen bir
tick'ti. Bu doğrulama gece yapıldı. Yeni sürümün seans içi gerçekleşme kalitesi
ve kârlılığı henüz ölçülmüş değil.

## Kayıt ve geri dönüş

Sunucunun gerçek dosyaları yeniden alındı, denetim temeliyle karşılaştırıldı;
31 dosyaya bağlamlı yama uygulandı. Değiştirilen dosyaların hash'leri test
edilmiş adayla birebir doğrulandı. Önceki kod ve state, sunucunun mevcut
yedek dizinine alındı; kesin konum özel dağıtım kaydında tutuluyor.

Deney başlangıcı **2026-08-18**, iki eski reset kaydı, portföy geçmişleri,
model seçimi ve risk sayıları korundu. State'e yalnızca kod geçiş zamanı,
önceki/yeni commit ve tick numarasını taşıyan ek bir code_releases kaydı
yazıldı. Geçmiş araştırma sonuçları yeniden derecelendirilmedi.

GitHub deposu sunucunun gerisindeydi; güncel çalışan kaynak ve testler public
sürüme taşındı. Public README ve mevcut ek dosyalar korundu, hatalı “kanıtlanmış
avantaj” ifadeleri düzeltildi. Hesap kimliği yer tutucu; .env, kimlik bilgileri,
ham state/fill kayıtları ve fiyat önbellekleri gönderilmiyor.

Kod dağıtımı, paper stratejisinin kârlı olduğunu göstermez. Açık araştırma
sorunları ve ölçüm sınırları ana quant raporunda kayıtlıdır.
