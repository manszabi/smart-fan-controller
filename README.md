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

## 📦 Telepítés

### 1. Repository klónozása:
```bash
git clone https://github.com/manszabiigen/smart-fan-controller.git
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
Szerkeszd a `settings.json` fájlt (FTP, zóna határok, stb.)

---

## 📦 Függőségek

| Package | Verzió | Leírás | Státusz |
|---------|--------|--------|---------|
| **openant** | `1.2.0` | ANT+ Power Meter kommunikáció | Kötelező |
| **bleak** | `≥0.21.0` | Bluetooth Low Energy (BLE) | Kötelező |
| **protobuf** | `≥4.25.0` | Protocol Buffers | Kötelező |
| **grpcio-tools** | `≥1.60.0` | Protobuf code generation | Kötelező |
| **psutil** | `≥5.9.0` | Folyamat figyelés (Zwift detektálás) | Opcionális* |

\* *Ha `psutil` nincs telepítve, a program feltételezi hogy a Zwift mindig fut.*

### Verzió ellenőrzés:

```bash
pip list | findstr "openant bleak protobuf grpcio psutil"
```

### Frissítés legújabb verzióra:

```bash
pip install --upgrade openant bleak protobuf grpcio-tools psutil
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

| Mező | Leírás | Alapértelmezett |
|------|--------|-----------------|
| `ftp` | Funkcionális teljesítmény (W) | 180 |
| `cooldown_seconds` | Cooldown idő zóna csökkentésnél (s) | 120 |
| `dropout_timeout` | Adatforrás kiesés timeout (s) | 5 |
| `zero_power_immediate` | 0W esetén azonnali leállás | false |
| `ble.skip_connection` | TEST MODE (BLE skip) | false |
| `data_source.primary` | Elsődleges forrás (`antplus`/`zwift`) | antplus |
| `data_source.fallback` | Másodlagos forrás (`zwift`/`none`) | zwift |

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
├── smart_fan_controller.py    # Fő program
├── zwift_simulator.py          # Zwift UDP szimulátor
├── settings.json               # Konfiguráció
├── zwift.proto                 # Zwift protobuf definíció
├── zwift_pb2.py                # Generált protobuf modul (generálandó)
├── requirements.txt            # Python függőségek
├── .gitignore                  # Git ignore fájl
└── README.md                   # Ez a fájl
```

## 📝 Licensz

MIT License

## 🤝 Közreműködés

Pull request-ek és issue-k szívesen fogadva!

## 📧 Kapcsolat

GitHub: [@manszabiigen](https://github.com/manszabiigen)
