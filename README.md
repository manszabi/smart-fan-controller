# Smart Fan Controller

ANT+ / Zwift Power Meter adatokat fogad és BLE-n keresztül vezérel egy ventilátort teljesítmény zónák alapján.

## 🎯 Funkciók

- **ANT+ Power Meter** támogatás
- **Zwift UDP** fallback (ha ANT+ kiesik)
- **4 teljesítmény zóna** (0, 1, 2, 3)
- **BLE ventilátor vezérlés** (ESP32)
- **Cooldown logika** (zóna csökkentésnél)
- **Dropout kezelés** (adatforrás kiesés)
- **TEST MODE** (BLE nélküli tesztelés)
- **Szívfrekvencia zónák** (HR alapú ventilátor vezérlés ANT+ HR adatok alapján)
- **ANT+ → BLE Bridge** (ANT+ adatok BLE-n való továbbítása más eszközök felé)

## 📦 Telepítés

### 1. Repository klónozása:
```bash
git clone https://github.com/manszabi/smart-fan-controller.git
cd smart-fan-controller
```

### 2. Python virtual environment:
```bash
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/Mac
```

### 3. Függőségek telepítése:
```bash
pip install -r requirements.txt
```

### 4. Zwift protobuf generálás:
```bash
python -m grpc_tools.protoc -I. --python_out=. zwift.proto
```

### 5. Beállítások módosítása:
Másold a `settings.example.jsonc` fájlt `settings.json` névre, és szerkeszd az igényeid szerint (FTP, zóna határok, stb.)

> 💡 A `settings.example.jsonc` egy részletesen kommentezett példa konfiguráció – ez a kiindulópontod. A részletes leírásért lásd a [`CONFIGURATION.md`](CONFIGURATION.md) fájlt.

---

## 📦 Függőségek

| Package | Verzió | Leírás | Státusz |
|---------|--------|--------|---------|
| **openant** | `1.2.0` | ANT+ Power Meter és Heart Rate kommunikáció | Kötelező |
| **bleak** | `≥0.21.0` | Bluetooth Low Energy (BLE) kliens | Kötelező |
| **bless** | `≥0.3.0` | BLE periféria/szerver (ANT+ bridge-hez) | Opcionális* |
| **protobuf** | `≥4.25.0` | Protocol Buffers | Kötelező |
| **grpcio-tools** | `≥1.60.0` | Protobuf code generation | Kötelező |
| **psutil** | `≥5.9.0` | Folyamat figyelés (Zwift detektálás) | Opcionális* |

\* *Ha `bless` nincs telepítve, az ANT+ bridge funkció automatikusan kikapcsol.*
\* *Ha `psutil` nincs telepítve, a program feltételezi hogy a Zwift mindig fut.*

### Verzió ellenőrzés:

```bash
pip list | findstr "openant bleak protobuf grpcio psutil"
```

### Frissítés legújabb verzióra:

```bash
pip install --upgrade openant bleak bless protobuf grpcio-tools psutil
```

## 🚀 Használat

### Normál mód (ESP32 BLE-vel):
```bash
python smart_fan_controller.py
```

### TEST MODE (BLE nélkül):
Állítsd be `settings.json`-ban:
```json
"ble": {
  "skip_connection": true,
  ...
}
```

### Zwift szimulátor (teszteléshez):
```bash
python zwift_simulator.py
```

## ⚙️ Beállítások

### `settings.json`:

> A teljes beállítási leírásért lásd a [`CONFIGURATION.md`](CONFIGURATION.md) fájlt.

| Mező | Leírás | Alapértelmezett |
|------|--------|-----------------|
| `ftp` | Funkcionális teljesítmény (W) | 180 |
| `min_watt` | Minimális teljesítmény küszöb (W) | 0 |
| `max_watt` | Maximális teljesítmény (W, 0 = FTP alapú) | 0 |
| `cooldown_seconds` | Cooldown idő zóna csökkentésnél (s) | 120 |
| `buffer_seconds` | Puffer idő zóna növelésnél (s) | 0 |
| `minimum_samples` | Minimum minták száma döntés előtt | 1 |
| `dropout_timeout` | Adatforrás kiesés timeout (s) | 5 |
| `zero_power_immediate` | 0W esetén azonnali leállás | false |
| `heart_rate_source` | HR forrás (`antplus`/`none`) | none |
| `ble.skip_connection` | TEST MODE (BLE skip) | false |
| `data_source.primary` | Elsődleges forrás (`antplus`/`zwift`) | antplus |
| `data_source.fallback` | Másodlagos forrás (`zwift`/`none`) | zwift |
| `heart_rate_zones` | HR zónák határai és ventilátor szintek | – |

## 🔧 Zóna határok

Alapértelmezetten (FTP=180W):

| Zóna | Tartomány | Ventilátor szint |
|------|-----------|------------------|
| 0 | 0W | OFF |
| 1 | 1W - 108W (60% FTP) | LOW |
| 2 | 109W - 160W (89% FTP) | MEDIUM |
| 3 | 161W+ (89%+ FTP) | HIGH |

## 📡 Adatforrások

### ANT+ (Elsődleges):
- USB ANT+ dongle szükséges
- Automatikus újracsatlakozás
- 30s türelmi idő induláskor

### Zwift UDP (Fallback):
- Lokális UDP socket (127.0.0.1:3022)
- Automatikus folyamat figyelés
- Raw protobuf parsing

## 🐛 Hibaelhárítás

### "Port already in use" (3022):
```powershell
netstat -ano | findstr :3022
taskkill /PID <pid> /F
```

### ANT+ dongle nem található:
- Ellenőrizd hogy be van-e dugva
- Próbáld más USB portban
- Futtasd adminisztrátorként

### BLE kapcsolat sikertelen:
- Állítsd be `skip_connection: true` teszteléshez
- Ellenőrizd hogy az ESP32 fut és látható
- Próbáld újraindítani a Bluetooth-t

## 📂 Projekt struktúra

```
smart_fan_controller/
├── smart_fan_controller.py         # Fő program
├── zwift_simulator.py              # Zwift UDP szimulátor
├── settings.json                   # Konfiguráció (személyes, nincs a repoban)
├── settings.example.jsonc          # Példa konfiguráció (kommentekkel)
├── CONFIGURATION.md                # Részletes beállítási leírás
├── zwift.proto                     # Zwift protobuf definíció
├── zwift_pb2.py                    # Generált protobuf modul (generálandó)
├── requirements.txt                # Python függőségek
├── test_smart_fan_controller.py    # Egységtesztek
├── .gitignore                      # Git ignore fájl
└── README.md                       # Ez a fájl
```

## 📝 Licensz

MIT License

## 🤝 Közreműködés

Pull request-ek és issue-k szívesen fogadva!

## 📧 Kapcsolat

GitHub: [@manszabi](https://github.com/manszabi)
