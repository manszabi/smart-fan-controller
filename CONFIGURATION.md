# Smart Fan Controller – Konfigurációs útmutató

## Tartalomjegyzék

1. [Bevezetés](#1-bevezetés)
2. [Alapbeállítások](#2-alapbeállítások)
3. [Teljesítmény zónák](#3-teljesítmény-zónák-zone_thresholds)
4. [BLE beállítások](#4-ble-beállítások-ble)
5. [Adatforrások](#5-adatforrások-data_source)
6. [ANT+ Bridge](#6-ant-bridge-antplus_bridge)
7. [Szívfrekvencia zónák](#7-szívfrekvencia-zónák-heart_rate_zones)
8. [Példa konfigurációk](#8-példa-konfigurációk)
9. [Hibaelhárítás](#9-hibaelhárítás)

---

## 1. Bevezetés

A `settings.json` fájl a Smart Fan Controller összes beállítását tartalmazza.
A fájl a program könyvtárában kell legyen (ott, ahol a `smart_fan_controller.py` is van).

**Fontos szabályok:**
- A fájl formátuma **JSON** (nem JSONC – kommenteket nem támogat).
- Ha a `settings.json` nem létezik, a program automatikusan létrehozza az alapértelmezett értékekkel.
- Csak azokat a mezőket kell megadni, amelyeket módosítani szeretnél; a többi beállítás az alapértelmezett értéket veszi fel.
- Érvénytelen érték esetén a program figyelmeztetést ír ki és az alapértelmezett értékkel folytatja.

**Szerkesztési módszer:**
1. Nyisd meg a `settings.json` fájlt egy szövegszerkesztővel (pl. Notepad++, VS Code).
2. Módosítsd a kívánt értékeket.
3. Mentsd el a fájlt, majd indítsd újra a programot.

Részletes, kommentált példafájlért lásd a `settings.example.jsonc` fájlt a repo gyökerében.

---

## 2. Alapbeállítások

### `ftp`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 100–500 |
| Alapértelmezett | 180 |

Az FTP (Functional Threshold Power) értéked wattban. Ez az a teljesítmény, amelyet körülbelül egy óráig képes vagy fenntartani. A teljesítmény zóna határok ennek százalékában kerülnek kiszámításra.

**Tipp:** Végezz FTP tesztet, vagy becsüld meg az értéked (pl. 20 perces maximális teljesítmény × 0,95).

---

### `min_watt`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 0 vagy több |
| Alapértelmezett | 0 |

Minimális figyelembe vett teljesítmény wattban. Az ennél kisebb értékeket a program érvénytelennek tekinti és figyelmen kívül hagyja.

---

### `max_watt`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | `min_watt`-nál nagyobb |
| Alapértelmezett | 1000 |

Maximális figyelembe vett teljesítmény wattban. Az ennél nagyobb értékeket a program érvénytelennek tekinti. A Z3 zóna felső határa.

---

### `cooldown_seconds`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 0–300 |
| Alapértelmezett | 120 |

Cooldown idő másodpercben. Ha a teljesítmény csökken (alacsonyabb zónára kellene váltani), a program ennyi ideig vár, mielőtt ténylegesen csökkenti a ventilátor szintjét. Ez megakadályozza a felesleges zóna-váltásokat rövid teljesítmény-visszaesések esetén (pl. hegyi szakasz utáni lejtő).

**Megjegyzés:** Zóna növelésekor nincs cooldown – a ventilátor azonnal reagál.

---

### `buffer_seconds`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–10 |
| Alapértelmezett | 3 |

Az átlagolási ablak mérete másodpercben. A program az elmúlt `buffer_seconds × 4` adatpont átlagát számítja, és az alapján dönt a zónáról. Az ANT+ power meter körülbelül 4 adatpontot küld másodpercenként.

- **Kisebb érték** → gyorsabb reakció a teljesítményváltozásra, de több zóna-ugrás.
- **Nagyobb érték** → simább működés, de lassabb reakció.

---

### `minimum_samples`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1 vagy több (max `buffer_seconds × 4`) |
| Alapértelmezett | 8 |

A zónadöntéshez szükséges minimális minták száma. A program indulása után addig vár a döntéssel, amíg legalább ennyi adatpont összegyűlt az átlagolási pufferben.

---

### `dropout_timeout`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1 vagy több |
| Alapértelmezett | 5 |

Dropout időkorlát másodpercben. Ha ennyi ideig nem érkezik adat az adatforrásoktól, a ventilátor azonnal 0-s szintre (ki) kapcsol. Ez megakadályozza, hogy az adatforrás elvesztésekor a ventilátor az utolsó aktív szinten maradjon.

---

### `zero_power_immediate`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Logikai (true/false) |
| Alapértelmezett | false |

Vezérli a 0 wattos olvasás kezelését:
- `false`: 0W esetén a cooldown timer indul (a ventilátor csak `cooldown_seconds` másodperc után kapcsol ki).
- `true`: 0W detektálásakor a ventilátor azonnal kikapcsol, cooldown nélkül.

**Tipp:** `true` értékkel állítsd be, ha szeretnéd, hogy a ventilátor azonnal leálljon, amikor befejezed az edzést.

---

## 3. Teljesítmény zónák (`zone_thresholds`)

A ventilátor 4 szintje (0–3) a teljesítmény zónákhoz igazodik:

| Zóna | Szint | Leírás |
|------|-------|--------|
| Z0 | 0 (ki) | Leállás vagy dropout |
| Z1 | 1 (alacsony) | 1W – FTP × z1_max_percent% |
| Z2 | 2 (közepes) | Z1_max+1W – FTP × z2_max_percent% |
| Z3 | 3 (magas) | Z2_max+1W – max_watt |

**Példa FTP=180 esetén (alapértelmezett beállítások):**

| Zóna | Watttartomány |
|------|---------------|
| Z0 | 0W (leállás) |
| Z1 | 1W – 108W (60% FTP) |
| Z2 | 109W – 160W (89% FTP) |
| Z3 | 161W – 1000W |

### `zone_thresholds.z1_max_percent`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–100, kisebb kell legyen mint `z2_max_percent` |
| Alapértelmezett | 60 |

A Z1 zóna felső határa az FTP százalékában. Az ennél kisebb teljesítmény Z1 zónát jelent (alacsony ventilátor szint).

### `zone_thresholds.z2_max_percent`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–100, nagyobb kell legyen mint `z1_max_percent` |
| Alapértelmezett | 89 |

A Z2 zóna felső határa az FTP százalékában. Az ennél nagyobb teljesítmény Z3 zónát jelent (magas ventilátor szint).

---

## 4. BLE beállítások (`ble`)

A BLE szekció az ESP32 ventilátor vezérlővel való Bluetooth kommunikációt konfigurálja.

### `ble.skip_connection`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Logikai (true/false) |
| Alapértelmezett | false |

**TEST MODE kapcsoló.** Ha `true`, a program nem próbál BLE kapcsolatot létesíteni – a parancsok csak a konzolon jelennek meg (`🧪 TEST MODE - Parancs: LEVEL:n`). Hasznos fejlesztéshez és konfiguráláshoz ESP32 hardver nélkül.

### `ble.device_name`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg |
| Alapértelmezett | "FanController" |

A BLE eszköz neve, amelyhez csatlakozni kell. Pontosan egyeznie kell azzal, ahogy az ESP32 firmware hirdeti magát Bluetooth-on.

### `ble.scan_timeout`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–60 |
| Alapértelmezett | 10 |

BLE keresési időkorlát másodpercben. A program ennyi ideig keres BLE eszközöket indításkor.

### `ble.connection_timeout`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–60 |
| Alapértelmezett | 15 |

BLE csatlakozási időkorlát másodpercben. Ennyi ideig próbál csatlakozni a megtalált eszközhöz.

### `ble.reconnect_interval`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–60 |
| Alapértelmezett | 5 |

Újracsatlakozási próbálkozások közötti várakozási idő másodpercben. Ha a BLE kapcsolat megszakad, ennyi másodpercenként próbál újra csatlakozni.

### `ble.max_retries`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–100 |
| Alapértelmezett | 10 |

Maximális újracsatlakozási kísérletek száma. Ha eléri ezt a számot, 30 másodpercet vár, majd újraindul a számlálás.

### `ble.command_timeout`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–30 |
| Alapértelmezett | 3 |

BLE parancs küldési időkorlát másodpercben. Ha a `LEVEL:n` parancs küldése nem sikerül ennyi idő alatt, timeout hibát jelez és bontja a kapcsolatot.

### `ble.service_uuid`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg (UUID formátum) |
| Alapértelmezett | "0000ffe0-0000-1000-8000-00805f9b34fb" |

A BLE GATT szerviz UUID-je. Egyeznie kell az ESP32 firmware GATT szerviz UUID-jével.

**Megjegyzés:** Ezt csak akkor kell módosítani, ha az ESP32 firmware más UUID-t használ.

### `ble.characteristic_uuid`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg (UUID formátum) |
| Alapértelmezett | "0000ffe1-0000-1000-8000-00805f9b34fb" |

A BLE GATT karakterisztika UUID-je, amelyre a `LEVEL:n` parancsok íródnak.

### `ble.pin_code`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám vagy null |
| Érvényes tartomány | 0–999999 vagy null |
| Alapértelmezett | null |

BLE PIN kód párosításhoz. Ha `null`, nem történik PIN-alapú párosítás. Csak akkor szükséges, ha az ESP32 firmware PIN kódot igényel a csatlakozáshoz.

---

## 5. Adatforrások (`data_source`)

### `data_source.primary`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg |
| Érvényes értékek | "antplus", "zwift" |
| Alapértelmezett | "antplus" |

Az elsődleges teljesítmény adatforrás:
- `"antplus"`: ANT+ power meter (USB ANT+ dongle szükséges)
- `"zwift"`: Zwift szimulátorból vett UDP adat

### `data_source.fallback`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg |
| Érvényes értékek | "zwift", "none" |
| Alapértelmezett | "zwift" |

Tartalék adatforrás, amelyre az elsődleges kiesése esetén vált:
- `"zwift"`: Ha az ANT+ kiesik, Zwift UDP adatokat használ
- `"none"`: Nincs fallback; kiesés esetén a dropout mechanizmus kezeli a helyzetet

**Megjegyzés:** Az elsődlegessel nem lehet azonos.

### `data_source.heart_rate_source`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg |
| Érvényes értékek | "antplus", "zwift", "both" |
| Alapértelmezett | "antplus" |

A szívfrekvencia (HR) adatforrása:
- `"antplus"`: Csak ANT+ HR monitor adatait használja
- `"zwift"`: Csak a Zwift UDP csomagból olvassa a HR-t
- `"both"`: Mindkettőt figyeli; ha az ANT+ HR aktív, azt preferálja; ha kiesett, Zwift HR-t használ

### `data_source.zwift`

A Zwift UDP kapcsolat albeállításai.

#### `data_source.zwift.port`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–65535 |
| Alapértelmezett | 3022 |

UDP port, amelyen a Zwift adatokat sugározza.

#### `data_source.zwift.host`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg (IP cím) |
| Alapértelmezett | "127.0.0.1" |

UDP fogadási hálózati cím. Általában `"127.0.0.1"` (localhost), ha a Zwift és a kontroller ugyanazon a gépen fut.

#### `data_source.zwift.process_name`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg |
| Alapértelmezett | "ZwiftApp.exe" |

A Zwift futó folyamatának neve. A program ezzel ellenőrzi, hogy a Zwift valóban fut-e. Linuxon/macOS-en `"ZwiftApp"` lehet a helyes érték.

#### `data_source.zwift.check_interval`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–60 |
| Alapértelmezett | 5 |

Milyen gyakran ellenőrzi másodpercenként, hogy a Zwift fut-e. Ha a Zwift leáll, a program lezárja az UDP socket-et és vár.

---

## 6. ANT+ Bridge (`antplus_bridge`)

Az ANT+ Bridge funkció az ANT+ adatokat BLE GATT szabványos szolgáltatásokként sugározza, hogy más BLE-kompatibilis eszközök (pl. Garmin okosóra, telefon, edzésalkalmazások) is lássák az adatokat.

### `antplus_bridge.enabled`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Logikai (true/false) |
| Alapértelmezett | false |

Ha `true`, az ANT+ Bridge funkció aktív. Szükséges hozzá a `bless` Python könyvtár.

### `antplus_bridge.heart_rate`

ANT+ szívfrekvencia monitor beállítások.

#### `antplus_bridge.heart_rate.enabled`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Logikai (true/false) |
| Alapértelmezett | true |

Ha `true`, a program ANT+ HR monitort is figyel (a power meter mellett). Az ANT+ bridge-en belül.

#### `antplus_bridge.heart_rate.device_id`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 0–65535 |
| Alapértelmezett | 0 |

ANT+ HR eszköz azonosítója. `0` = bármely elérhető HR monitor.

### `antplus_bridge.ble_broadcast`

BLE GATT sugárzás beállításai.

#### `antplus_bridge.ble_broadcast.enabled`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Logikai (true/false) |
| Alapértelmezett | true |

Ha `true`, a BLE GATT sugárzás aktív (az `antplus_bridge.enabled=true` mellett).

#### `antplus_bridge.ble_broadcast.power_service`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Logikai (true/false) |
| Alapértelmezett | true |

Ha `true`, kerékpáros teljesítmény GATT szolgáltatást sugároz (Cycling Power Service, UUID: 0x1818).

#### `antplus_bridge.ble_broadcast.heart_rate_service`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Logikai (true/false) |
| Alapértelmezett | true |

Ha `true`, szívfrekvencia GATT szolgáltatást sugároz (Heart Rate Service, UUID: 0x180D).

#### `antplus_bridge.ble_broadcast.device_name`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg |
| Alapértelmezett | "SmartFanBridge" |

A BLE bridge eszköz neve, ahogy más eszközök látják.

---

## 7. Szívfrekvencia zónák (`heart_rate_zones`)

A HR zóna rendszer lehetővé teszi, hogy a ventilátor a szívfrekvencia alapján is vezérelje magát, nem csak a teljesítmény alapján.

### `heart_rate_zones.enabled`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Logikai (true/false) |
| Alapértelmezett | false |

Ha `true`, a HR zóna rendszer aktív. Ha `false`, a HR adat csak a konzolon jelenik meg, de nem befolyásolja a ventilátor szintjét.

### `heart_rate_zones.max_hr`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 100–220 |
| Alapértelmezett | 185 |

Maximális szívfrekvencia bpm-ben. A HR zóna határok ennek százalékában kerülnek kiszámításra.

**Tipp:** Becsülhető a `220 - életkor` képlettel, vagy mérhető maximális terheléses teszttel.

### `heart_rate_zones.resting_hr`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 30–100 |
| Alapértelmezett | 60 |

Pihenő szívfrekvencia bpm-ben. Ez alatt a rendszer 0-s HR zónát (pihenő) jelez. Kisebb kell legyen, mint a Z1 határból számított érték.

### `heart_rate_zones.zone_mode`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Szöveg |
| Érvényes értékek | "power_only", "hr_only", "higher_wins" |
| Alapértelmezett | "power_only" |

A HR és teljesítmény zóna összevonásának módja:

| Mód | Leírás |
|-----|--------|
| `"power_only"` | Csak a teljesítmény zóna dönt. A HR adat megjelenik a konzolon, de nem hat a ventilátorra. |
| `"hr_only"` | Csak a HR zóna dönt. A teljesítmény adatot csak a dropout detektáláshoz figyeli. |
| `"higher_wins"` | A teljesítmény és HR zóna közül a nagyobb értékű dönt. Pl. ha Z2 teljesítmény + Z3 HR, akkor Z3 lesz. |

### `heart_rate_zones.z1_max_percent`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–100, kisebb kell legyen mint `z2_max_percent` |
| Alapértelmezett | 70 |

HR Z1 zóna felső határa a `max_hr` százalékában.

**Példa:** `max_hr=185`, `z1_max_percent=70` → Z1 max = 185 × 70% = 129 bpm

### `heart_rate_zones.z2_max_percent`
| Tulajdonság | Érték |
|-------------|-------|
| Típus | Egész szám |
| Érvényes tartomány | 1–100, nagyobb kell legyen mint `z1_max_percent` |
| Alapértelmezett | 80 |

HR Z2 zóna felső határa a `max_hr` százalékában.

**Példa:** `max_hr=185`, `z2_max_percent=80` → Z2 max = 185 × 80% = 148 bpm

**HR zóna táblázat (alapértelmezett: max_hr=185):**

| HR zóna | Szívfrekvencia | Ventilátor szint |
|---------|----------------|-----------------|
| Z0 | < 60 bpm (pihenő) | 0 (ki) |
| Z1 | 60–129 bpm | 1 (alacsony) |
| Z2 | 130–148 bpm | 2 (közepes) |
| Z3 | > 148 bpm | 3 (magas) |

---

## 8. Példa konfigurációk

### 8.1 Alap ANT+ power meter + ESP32 ventilátor

A legegyszerűbb konfiguráció: ANT+ power meter adatai alapján vezérli a ventilátort.

```json
{
  "ftp": 250,
  "min_watt": 0,
  "max_watt": 1000,
  "cooldown_seconds": 120,
  "buffer_seconds": 3,
  "minimum_samples": 8,
  "dropout_timeout": 5,
  "zero_power_immediate": false,
  "zone_thresholds": {
    "z1_max_percent": 60,
    "z2_max_percent": 89
  },
  "ble": {
    "skip_connection": false,
    "device_name": "FanController",
    "scan_timeout": 10,
    "connection_timeout": 15,
    "reconnect_interval": 5,
    "max_retries": 10,
    "command_timeout": 3,
    "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
    "characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
    "pin_code": null
  },
  "data_source": {
    "primary": "antplus",
    "fallback": "none",
    "heart_rate_source": "antplus",
    "zwift": {
      "port": 3022,
      "host": "127.0.0.1",
      "process_name": "ZwiftApp.exe",
      "check_interval": 5
    }
  },
  "antplus_bridge": {
    "enabled": false,
    "heart_rate": {
      "enabled": true,
      "device_id": 0
    },
    "ble_broadcast": {
      "enabled": true,
      "power_service": true,
      "heart_rate_service": true,
      "device_name": "SmartFanBridge"
    }
  },
  "heart_rate_zones": {
    "enabled": false,
    "max_hr": 185,
    "resting_hr": 60,
    "zone_mode": "power_only",
    "z1_max_percent": 70,
    "z2_max_percent": 80
  }
}
```

---

### 8.2 Zwift-only mód

Ha csak Zwifttel edzel, ANT+ dongle nélkül.

```json
{
  "ftp": 200,
  "cooldown_seconds": 90,
  "buffer_seconds": 3,
  "ble": {
    "skip_connection": false,
    "device_name": "FanController",
    "scan_timeout": 10,
    "connection_timeout": 15,
    "reconnect_interval": 5,
    "max_retries": 10,
    "command_timeout": 3,
    "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
    "characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
    "pin_code": null
  },
  "data_source": {
    "primary": "zwift",
    "fallback": "none",
    "heart_rate_source": "zwift",
    "zwift": {
      "port": 3022,
      "host": "127.0.0.1",
      "process_name": "ZwiftApp.exe",
      "check_interval": 5
    }
  }
}
```

---

### 8.3 ANT+ elsődleges + Zwift fallback + HR zónák

Teljes konfiguráció: ANT+ power meter, Zwift tartalék, szívfrekvencia alapú zónák is.

```json
{
  "ftp": 230,
  "cooldown_seconds": 120,
  "buffer_seconds": 3,
  "minimum_samples": 8,
  "dropout_timeout": 5,
  "zero_power_immediate": false,
  "zone_thresholds": {
    "z1_max_percent": 60,
    "z2_max_percent": 89
  },
  "ble": {
    "skip_connection": false,
    "device_name": "FanController",
    "scan_timeout": 10,
    "connection_timeout": 15,
    "reconnect_interval": 5,
    "max_retries": 10,
    "command_timeout": 3,
    "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
    "characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
    "pin_code": null
  },
  "data_source": {
    "primary": "antplus",
    "fallback": "zwift",
    "heart_rate_source": "both",
    "zwift": {
      "port": 3022,
      "host": "127.0.0.1",
      "process_name": "ZwiftApp.exe",
      "check_interval": 5
    }
  },
  "antplus_bridge": {
    "enabled": false,
    "heart_rate": {
      "enabled": true,
      "device_id": 0
    },
    "ble_broadcast": {
      "enabled": true,
      "power_service": true,
      "heart_rate_service": true,
      "device_name": "SmartFanBridge"
    }
  },
  "heart_rate_zones": {
    "enabled": true,
    "max_hr": 185,
    "resting_hr": 60,
    "zone_mode": "higher_wins",
    "z1_max_percent": 70,
    "z2_max_percent": 85
  }
}
```

---

### 8.4 TEST MODE (BLE nélkül, fejlesztéshez)

Fejlesztéshez és konfiguráláshoz, ESP32 hardver nélkül. A BLE parancsok csak a konzolon jelennek meg.

```json
{
  "ftp": 180,
  "cooldown_seconds": 30,
  "buffer_seconds": 1,
  "minimum_samples": 2,
  "dropout_timeout": 10,
  "ble": {
    "skip_connection": true,
    "device_name": "FanController",
    "scan_timeout": 10,
    "connection_timeout": 15,
    "reconnect_interval": 5,
    "max_retries": 10,
    "command_timeout": 3,
    "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
    "characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
    "pin_code": null
  },
  "data_source": {
    "primary": "antplus",
    "fallback": "zwift",
    "heart_rate_source": "antplus",
    "zwift": {
      "port": 3022,
      "host": "127.0.0.1",
      "process_name": "ZwiftApp.exe",
      "check_interval": 5
    }
  }
}
```

---

## 9. Hibaelhárítás

### A program nem találja a BLE eszközt

**Tünetek:** `✗ Nem található: FanController`

**Megoldás:**
1. Ellenőrizd, hogy az ESP32 be van-e kapcsolva és Bluetooth hirdetés módban van-e.
2. Ellenőrizd, hogy a `ble.device_name` pontosan egyezik-e az ESP32 által hirdetett névvel (kis-nagybetű érzékeny).
3. Növeld a `ble.scan_timeout` értékét (pl. 20-ra).
4. Ellenőrizd, hogy a Bluetooth adapter engedélyezve van-e a számítógépen.

---

### Az ANT+ eszköz nem csatlakozik

**Tünetek:** `✗ ANT+ indítási hiba: ...`

**Megoldás:**
1. Ellenőrizd, hogy az USB ANT+ dongle be van-e dugva.
2. Ellenőrizd, hogy az ANT+ driver telepítve van-e (Windows: Zadig, FTDI driver).
3. Linuxon ellenőrizd az USB jogosultságokat (`/dev/ttyUSB*` vagy `udev` szabályok).
4. Indítsd újra a programot.

---

### Érvénytelen beállítás figyelmeztetés

**Tünetek:** `⚠ FIGYELMEZTETÉS: Érvénytelen 'ftp' érték: ...`

**Megoldás:**
1. Ellenőrizd a `settings.json` fájlt – a figyelmeztetés megmutatja, melyik mező hibás.
2. Győződj meg arról, hogy az érték a megadott tartományon belül van.
3. Ellenőrizd a JSON szintaxist (vesszők, idézőjelek, kapcsos zárójelek).

---

### A ventilátor nem reagál

**Tünetek:** A BLE parancsok elküldésre kerülnek (`✓ Parancs elküldve: LEVEL:2`), de a ventilátor nem változtat.

**Megoldás:**
1. Ellenőrizd az ESP32 firmware-t és a `service_uuid` / `characteristic_uuid` beállításokat.
2. Ellenőrizd a BLE kapcsolat stabilitását – esetleg növeld a `max_retries` értékét.

---

### A program indulás után azonnal Z0-ra vált

**Tünetek:** Azonnal dropout üzenet jelenik meg.

**Megoldás:**
1. Ellenőrizd, hogy az adatforrás (ANT+ vagy Zwift) küld-e adatot.
2. Növeld a `dropout_timeout` értékét, ha az adatforrás lassan indul el.
3. ANT+ esetén: várj néhány másodpercet, amíg az eszköz csatlakozik.

---

### Zwift adatok nem érkeznek

**Tünetek:** `⚠ Zwift leállt, UDP figyelés szünetel`

**Megoldás:**
1. Ellenőrizd, hogy a Zwift valóban fut-e.
2. Ellenőrizd a `data_source.zwift.process_name` értéket – Linuxon/macOS-en eltérhet.
3. Ellenőrizd a `data_source.zwift.port` és `host` értékeket.
4. Ellenőrizd, hogy a tűzfal nem blokkolja-e az UDP port-ot.
