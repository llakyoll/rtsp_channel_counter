# RTSP Kanal Sayacı

Yerel tarayıcı arayüzüyle RTSP cihazındaki kanal numaralarını dener. Her adresi
`gst-discoverer-1.0` ile açıp ses veya görüntü akışı bulunan **farklı** kanalları
sayar. Kolaj için GStreamer ve Python Pillow kullanır.

## Başlatma

```bash
python3 app.py
```

Tarayıcıda <http://127.0.0.1:8765> adresini açın. Farklı port için
`python3 app.py --port 9000` kullanın.

Örnek giriş:

```text
rtsp://kullanıcıadı:şifre@kamera-adresi:554/Streaming/Channels/101
```

Bu adres biçiminde `101`, kanal 1'in ana akışı (`01`) sayılır. Uygulama örneğin
kanal 2 için `201`, kanal 12 için `1201` üretir. Alt akış seçilirse `102`,
`202`, `1202` de denenir. Başka yol düzenleri için örneğin
`rtsp://kamera/live/{channel}/stream/{stream}` yazabilirsiniz. Şifre içindeki
`@`, `#`, `?` gibi URL özel karakterlerini yüzde kodlamasıyla girin.

Tarama tamamlanınca uygulama bulunan her farklı kanaldan bir görüntü alır ve
etiketli bir JPG kolajı otomatik hazırlar. Arayüzde önizleme ve **JPG indir**
bağlantısı görünür. Video karesi alınamayan kanal da kolajda açıklamalı bir
kutu olarak yer alır. Hiçbir kare alınamazsa boş kolaj sunulmaz; kanal bazında
hata nedeni gösterilir. Bu özellik için `gst-launch-1.0` ve Python `Pillow` gerekir.

`Bulunan farklı kanal` yalnızca doğrulanmış akışları sayar. Yetki, bağlantı ve
zaman aşımı hataları ayrıca gösterilir; bunlar kanalın bulunmadığı anlamına
gelmez. Tarama, seçilen aralıktaki her kanalı dener; ilk başarısızlıkta durmaz.
Sonuç adreslerinde kullanıcı adı ve şifre gizlenir. Sunucu yalnızca
`127.0.0.1` üzerinde dinler.

## Test

```bash
python3 -m unittest -v
```

Bu testler gerçek bir kameraya bağlanmaz. Gerçek tarama için erişilebilir bir
RTSP adresi ve doğru kimlik bilgileri gerekir.

## Bu makinedeki ortam

GStreamer 1.28.8, `rtspsrc` eklentisi, `gst-discoverer-1.0`, `gst-launch-1.0` ve Pillow
mevcut. Etkin
`vision` Python ortamındaki OpenCV şu anda `libOpenEXR-3_4.so.33` eksikliği
yüzünden içe aktarılamıyor; sistemde OpenEXR 3.5 var. Ayrıca aynı ortamda
`opencv-python` ve `opencv-python-headless` birlikte kurulu. Bu uygulama OpenCV
kullanmadığı için bundan etkilenmez.
