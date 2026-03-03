# Smart Fan Controller

![Version](https://img.shields.io/badge/verzió-v1.2.0-blue)

ANT+ Power Meter adatokat fogad és BLE-n keresztül vezérel egy ventilátort teljesítmény zónák alapján.

## 🎯 Funkciók

- **ANT+ Power Meter** támogatás
- **4 teljesítmény zóna** (0, 1, 2, 3)
- **BLE ventilátor vezérlés** (ESP32)
- **Cooldown logika** (zóna csökkentésnél)
- **Dropout kezelés** (adatforrás kiesés)
- **Szívfrekvencia zónák** (HR alapú ventilátor vezérlés ANT+ HR adatok alapján)
- **Thread-safe BLE kommunikáció** (szálbiztos BLE vezérlés)
- **BLE adatforrás támogatás** (power és HR BLE-n keresztül is)

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

### 4. Beállítások módosítása:
Másold a `settings.example.jsonc` fájlt `settings.json` névre, és szerkeszd az igényeid szerint (FTP, zóna határok, stb.)

> 💡 A `settings.example.jsonc` egy részletesen kommentezett példa konfiguráció – ez a kiindulópontod. A részletes leírásért lásd a [`CONFIGURATION.md`](CONFIGURATION.md) fájlt.

---

## 📦 Függőségek

| Package | Verzió | Leírás | Státusz |
|---------|--------|--------|---------|
| **openant** | `1.2.0` | ANT+ Power Meter és Heart Rate kommunikáció | Kötelező |
| **bleak** | `≥0.21.0` | Bluetooth Low Energy (BLE) kliens | Kötelező |

### Verzió ellenőrzés:

```bash
pip list | findstr "openant bleak"
```

### Frissítés legújabb verzióra:

```bash
pip install --upgrade openant bleak
```

## 🚀 Használat

### Normál mód (ESP32 BLE-vel):
```bash
python smart_fan_controller.py
```

## ⚙️ Beállítások

### `settings.json`:

> A teljes beállítási leírásért lásd a [`CONFIGURATION.md`](CONFIGURATION.md) fájlt.

| Mező | Leírás | Alapértelmezett |
|------|--------|-----------------|
| `ftp` | Funkcionális teljesítmény (W) | 180 |
| `min_watt` | Minimális teljesítmény küszöb (W) | 0 |
| `max_watt` | Maximális teljesítmény (W, 0 = FTP alapú) | 1000 |
| `cooldown_seconds` | Cooldown idő zóna csökkentésnél (s) | 120 |
| `buffer_seconds` | Puffer idő zóna növelésnél (s) | 3 |
| `minimum_samples` | Minimum minták száma döntés előtt | 8 |
| `dropout_timeout` | Adatforrás kiesés timeout (s) | 5 |
| `zero_power_immediate` | 0W esetén azonnali leállás | false |
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

### BLE:
- BLE power meter támogatás
- BLE HR eszköz (óra/öv) támogatás
- Automatikus újracsatlakozás BLE adatforrásokhoz is

## 🐛 Hibaelhárítás

### ANT+ dongle nem található:
- Ellenőrizd hogy be van-e dugva
- Próbáld más USB portban
- Futtasd adminisztrátorként

### BLE kapcsolat sikertelen:
- Ellenőrizd hogy az ESP32 fut és látható
- Próbáld újraindítani a Bluetooth-t

## 📂 Projekt struktúra

```
smart_fan_controller/
├── smart_fan_controller.py         # Fő program
├── settings.json                   # Konfiguráció (személyes, nincs a repoban)
├── settings.example.json           # Példa konfiguráció (kommentek nélkül)
├── settings.example.jsonc          # Példa konfiguráció (kommentekkel)
├── CONFIGURATION.md                # Részletes beállítási leírás
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
