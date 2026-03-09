#!/usr/bin/env python3
"""
swift_fan_controller_new.py

Smart Fan Controller – moduláris, párhuzamos implementáció.

Minden fő funkció különálló aszinkron feladatban/szálban fut:
  - ANT+ bemenő adatkezelés (HR, power)        → ANTPlusInputHandler (daemon szál + asyncio bridge)
  - BLE ventilátor kimenő vezérlés              → BLEFanOutputController (asyncio korrutin)
  - BLE bemenő adatok (HR, power)               → BLEPowerInputHandler, BLEHRInputHandler (asyncio)
  - Zwift UDP bejövő adatkezelés                → ZwiftUDPInputHandler (asyncio DatagramProtocol)
  - Power átlag számítás                        → PowerAverager + power_processor_task
  - HR átlag számítás                           → HRAverager + hr_processor_task
  - higher_wins logika                          → apply_zone_mode() (tiszta függvény)
  - Cooldown logika                             → CooldownController (állapotgép)
  - Zona számítás                               → zone_for_power(), zone_for_hr() (tiszta függvények)
  - Zona elküldése                              → zone_controller_task + send_zone()
  - Konzolos kiírás                             → ConsolePrinter (throttle-olt)

Architektúra:
  - Egyetlen asyncio event loop a fő vezérlési logikához
  - Saját daemon szál az ANT+ számára (blokkoló könyvtár)
  - asyncio.Queue a komponensek közötti adatátvitelhez
  - asyncio.Event a zóna újraszámítás jelzéséhez
  - asyncio.Lock a megosztott állapot védelméhez
  - Tiszta (mellékhatás-mentes) függvények a logikához (jól tesztelhetők)

Verziószám: 1.0.0
"""

import asyncio
import copy
import json
import logging
import math
import signal
import sys
import threading
import time
import atexit
from collections import deque
from typing import Any, Dict, Optional, Tuple

Node: Any = None
ANTPLUS_NETWORK_KEY: Any = None
PowerMeter: Any = None
PowerData: Any = None
HeartRate: Any = None
HeartRateData: Any = None

BleakClient: Any = None
BleakScanner: Any = None

# --- Külső könyvtárak (opcionális importok – a program importálható marad teszteléshez) ---
try:
    from openant.easy.node import Node
    from openant.devices import ANTPLUS_NETWORK_KEY
    from openant.devices.power_meter import PowerMeter, PowerData
    from openant.devices.heart_rate import HeartRate, HeartRateData
    _ANTPLUS_AVAILABLE = True
except ImportError:
    _ANTPLUS_AVAILABLE = False

try:
    from bleak import BleakClient, BleakScanner
    _BLEAK_AVAILABLE = True
except ImportError:
    _BLEAK_AVAILABLE = False

__version__ = "1.0.0"

logger = logging.getLogger("swift_fan_controller_new")


# ============================================================
# ALAPÉRTELMEZETT BEÁLLÍTÁSOK
# ============================================================

DEFAULT_SETTINGS: Dict[str, Any] = {
    "ftp": 180,                    # Funkcionális küszöbteljesítmény wattban (100–500)
    "min_watt": 0,                 # Minimális érvényes teljesítmény (0 vagy több)
    "max_watt": 1000,              # Maximális érvényes teljesítmény
    "cooldown_seconds": 120,       # Zóna csökkentés előtti várakozási idő (s), 0–300
    "buffer_seconds": 3,           # Átlagolási ablak mérete (s), 1–10
    "minimum_samples": 6,          # ← MÓDOSÍTOTT: volt 8, most 6 (= buffersize // 2)
    "buffer_rate_hz": 4,           # Várható adatbeérkezési ráta (Hz), 1–60
    "dropout_timeout": 5,          # Adatnélküli idő (s), ami után Z0-ra vált
    "zero_power_immediate": False, # True: 0W esetén azonnali leállás cooldown nélkül
    "zone_thresholds": {
        "z1_max_percent": 60,      # Z1 felső határ: FTP×60%
        "z2_max_percent": 89,      # Z2 felső határ: FTP×89%
    },
    "ble": {
        "device_name": "FanController",
        "scan_timeout": 10,
        "connection_timeout": 15,
        "reconnect_interval": 5,
        "max_retries": 10,
        "command_timeout": 3,
        "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
        "characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
        "pin_code": None,
    },
    "data_source": {
        "power_source": "antplus",
        "hr_source": "antplus",
        "ble_power_device_name": None,
        "ble_power_scan_timeout": 10,
        "ble_power_reconnect_interval": 5,
        "ble_power_max_retries": 10,
        "ble_hr_device_name": None,
        "ble_hr_scan_timeout": 10,
        "ble_hr_reconnect_interval": 5,
        "ble_hr_max_retries": 10,
        "zwift_udp_port": 7878,
        "zwift_udp_host": "127.0.0.1",
        "zwift_udp_buffer_seconds": 10,
        "zwift_udp_minimum_samples": 2,
        "zwift_udp_dropout_timeout": 15,
    },
    "heart_rate_zones": {
        "enabled": False,
        "max_hr": 185,
        "resting_hr": 60,
        # zone_mode: "power_only" | "hr_only" | "higher_wins"
        "zone_mode": "power_only",
        "z1_max_percent": 70,
        "z2_max_percent": 80,
        "valid_min_hr": 30,    # ← ÚJ: ez alatt fizikailag érvénytelen
        "valid_max_hr": 220,   # ← ÚJ: ez felett fizikailag érvénytelen
    },
}


# ============================================================
# BEÁLLÍTÁSOK BETÖLTÉSE
# ============================================================

def load_settings(settings_file: str = "settings.json") -> Dict[str, Any]:
    """Betölti és validálja a JSON beállítási fájlt.

    Alapértelmezett értékekből indul ki (DEFAULT_SETTINGS), majd felülírja
    az érvényes, fájlból betöltött értékekkel. Hibás mezőnél az alapértelmezett
    marad érvényben (figyelmeztetéssel).

    Ha a fájl nem létezik, automatikusan létrehozza az alapértelmezettekkel.

    Args:
        settings_file: A JSON beállítások fájl elérési útja.

    Returns:
        Validált beállítások dict-je.
    """
    settings = copy.deepcopy(DEFAULT_SETTINGS)

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        print(f"⚠ '{settings_file}' nem található, alapértelmezett beállítások használata.")
        _save_default_settings(settings_file, settings)
        return settings
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠ '{settings_file}' beolvasási hiba: {exc}. Alapértelmezés használata.")
        return settings

        # --- Egyszerű skaláris mezők ---
        load_int(loaded, settings, "ftp", 100, 500)
        load_int(loaded, settings, "min_watt", 0, 9999)
        load_int(loaded, settings, "max_watt", 1, 100000)
        load_int(loaded, settings, "cooldown_seconds", 0, 300)
        load_int(loaded, settings, "buffer_seconds", 1, 10)
        load_int(loaded, settings, "minimum_samples", 1, 1000)
        load_int(loaded, settings, "buffer_rate_hz", 1, 60)    # ← ÚJ sor!
        load_int(loaded, settings, "dropout_timeout", 1, 120)
        load_bool(loaded, settings, "zero_power_immediate")


    # --- Zóna határok ---
    if isinstance(loaded.get("zone_thresholds"), dict):
        zt = loaded["zone_thresholds"]
        _load_int(zt, settings["zone_thresholds"], "z1_max_percent", 1, 100)
        _load_int(zt, settings["zone_thresholds"], "z2_max_percent", 1, 100)

    # --- BLE kimeneti beállítások ---
    if isinstance(loaded.get("ble"), dict):
        b = loaded["ble"]
        if isinstance(b.get("device_name"), str) and b["device_name"]:
            settings["ble"]["device_name"] = b["device_name"]
        _load_int(b, settings["ble"], "scan_timeout", 1, 60)
        _load_int(b, settings["ble"], "connection_timeout", 1, 60)
        _load_int(b, settings["ble"], "reconnect_interval", 1, 60)
        _load_int(b, settings["ble"], "max_retries", 1, 100)
        _load_int(b, settings["ble"], "command_timeout", 1, 30)
        if isinstance(b.get("service_uuid"), str) and b["service_uuid"]:
            settings["ble"]["service_uuid"] = b["service_uuid"]
        if isinstance(b.get("characteristic_uuid"), str) and b["characteristic_uuid"]:
            settings["ble"]["characteristic_uuid"] = b["characteristic_uuid"]
        if "pin_code" in b:
            pc = b["pin_code"]
            if pc is None:
                settings["ble"]["pin_code"] = None
            elif isinstance(pc, int) and not isinstance(pc, bool) and 0 <= pc <= 999999:
                settings["ble"]["pin_code"] = str(pc)
            elif isinstance(pc, str) and pc.isdigit() and 0 < len(pc) <= 20:
                settings["ble"]["pin_code"] = pc
            else:
                print(f"⚠ Érvénytelen 'pin_code' érték: {pc}")

    # --- Adatforrás beállítások ---
    if isinstance(loaded.get("data_source"), dict):
        ds = loaded["data_source"]
        if ds.get("power_source") in ("antplus", "ble", "zwift_udp"):
            settings["data_source"]["power_source"] = ds["power_source"]
        if ds.get("hr_source") in ("antplus", "ble", "zwift_udp"):
            settings["data_source"]["hr_source"] = ds["hr_source"]
        for key in ("ble_power_device_name", "ble_hr_device_name"):
            if key in ds and (ds[key] is None or isinstance(ds[key], str)):
                settings["data_source"][key] = ds[key]
        for key in ("ble_power_scan_timeout", "ble_power_reconnect_interval",
                    "ble_hr_scan_timeout", "ble_hr_reconnect_interval"):
            _load_int(ds, settings["data_source"], key, 1, 60)
        for key in ("ble_power_max_retries", "ble_hr_max_retries"):
            _load_int(ds, settings["data_source"], key, 1, 100)
        if isinstance(ds.get("zwift_udp_host"), str) and ds["zwift_udp_host"]:
            settings["data_source"]["zwift_udp_host"] = ds["zwift_udp_host"]
        _load_int(ds, settings["data_source"], "zwift_udp_port", 1024, 65535)
        _load_int(ds, settings["data_source"], "zwift_udp_buffer_seconds", 1, 60)
        _load_int(ds, settings["data_source"], "zwift_udp_minimum_samples", 1, 20)
        _load_int(ds, settings["data_source"], "zwift_udp_dropout_timeout", 1, 120)

    # --- Szívfrekvencia zóna beállítások ---
    if isinstance(loaded.get("heart_rate_zones"), dict):
        hrz = loaded["heart_rate_zones"]
        _load_bool(hrz, settings["heart_rate_zones"], "enabled")
        _load_int(hrz, settings["heart_rate_zones"], "max_hr", 100, 220)
        _load_int(hrz, settings["heart_rate_zones"], "resting_hr", 30, 100)
        if hrz.get("zone_mode") in ("power_only", "hr_only", "higher_wins"):
            settings["heart_rate_zones"]["zone_mode"] = hrz["zone_mode"]
        _load_int(hrz, settings["heart_rate_zones"], "valid_min_hr", 1, 60)
        _load_int(hrz, settings["heart_rate_zones"], "valid_max_hr", 150, 300)
        _load_int(hrz, settings["heart_rate_zones"], "z1_max_percent", 1, 100)
        _load_int(hrz, settings["heart_rate_zones"], "z2_max_percent", 1, 100)

    # Zwift UDP felülírja a buffer/min_samples/dropout értékeket
    ds_cfg = settings["data_source"]
    if ds_cfg["power_source"] == "zwift_udp" or ds_cfg["hr_source"] == "zwift_udp":
        settings["buffer_seconds"] = ds_cfg["zwift_udp_buffer_seconds"]
        settings["minimum_samples"] = ds_cfg["zwift_udp_minimum_samples"]
        settings["dropout_timeout"] = ds_cfg["zwift_udp_dropout_timeout"]


    # --- Kereszt-validációk a végleges beállításokon ---
    # 1) minimum_samples <= buffer_seconds * BUFFER_RATE_HZ
    try:
        buffer_seconds = int(settings.get("buffer_seconds", 0))
        minimum_samples = int(settings.get("minimum_samples", 0))
        buffer_rate_hz = int(settings.get("buffer_rate_hz", 4))
        if buffer_seconds > 0 and buffer_rate_hz > 0:
            max_samples = buffer_seconds * buffer_rate_hz
            if minimum_samples > max_samples:
                print(
                    f"⚠ Érvénytelen minimum_samples ({minimum_samples}) – "
                    f"nagyobb, mint buffer_seconds * BUFFER_RATE_HZ ({buffer_seconds} * {buffer_rate_hz} = {max_samples}). "
                    f"minimum_samples {max_samples}-re állítva."
                )
                settings["minimum_samples"] = max_samples
    except Exception as exc:
        # Ha bármi váratlan hiba történik, nem dobunk kivételt konfiguráció betöltéskor.
        print(f"⚠ minimum_samples/buffer_seconds kereszt-validáció sikertelen: {exc}")
    # 2) Power zóna: min_watt < max_watt
    try:
        zt = settings.get("zone_thresholds") or {}
        min_watt = zt.get("min_watt")
        max_watt = zt.get("max_watt")
        if isinstance(min_watt, int) and isinstance(max_watt, int):
            if min_watt > max_watt:
                print(
                    f"⚠ Érvénytelen watt tartomány (min_watt={min_watt}, max_watt={max_watt}). "
                    f"Feltételezett felcserélés, értékek megfordítva."
                )
                zt["min_watt"], zt["max_watt"] = max_watt, min_watt
            elif min_watt == max_watt:
                print(
                    f"⚠ min_watt és max_watt azonos értékű ({min_watt}). "
                    f"max_watt {min_watt + 1}-re állítva."
                )
                zt["max_watt"] = min_watt + 1
    except Exception as exc:
        print(f"⚠ Watt zóna kereszt-validáció sikertelen: {exc}")
    # 3) HR zónák: z1_max_percent < z2_max_percent és resting_hr < max_hr
    try:
        hrz = settings.get("heart_rate_zones") or {}
        z1p = hrz.get("z1_max_percent")
        z2p = hrz.get("z2_max_percent")
        if isinstance(z1p, int) and isinstance(z2p, int):
            if z1p >= z2p:
                print(
                    f"⚠ Érvénytelen HR zóna százalékok (z1_max_percent={z1p}, z2_max_percent={z2p}). "
                    f"Értékek rendezése és legalább 1% különbség biztosítása."
                )
                low = min(z1p, z2p)
                high = max(z1p, z2p)
                # biztosítsuk, hogy low < high
                if low == high:
                    high = min(100, low + 1)
                hrz["z1_max_percent"] = low
                hrz["z2_max_percent"] = high
        max_hr = hrz.get("max_hr")
        resting_hr = hrz.get("resting_hr")
        if isinstance(max_hr, int) and isinstance(resting_hr, int):
            if resting_hr >= max_hr:
                new_rest = max(30, max_hr - 1)
                print(
                    f"⚠ Érvénytelen HR értékek (resting_hr={resting_hr}, max_hr={max_hr}). "
                    f"resting_hr {new_rest}-re állítva, hogy resting_hr < max_hr legyen."
                )
                hrz["resting_hr"] = new_rest
    except Exception as exc:
        print(f"⚠ HR zóna kereszt-validáció sikertelen: {exc}")

    return settings


def _load_int(src: dict, dst: dict, key: str, lo: int, hi: int) -> None:
    """Helper: int/float mezőt tölt be érvényes tartomány esetén."""
    if key in src:
        v = src[key]
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            dst[key] = int(v)
        else:
            print(f"⚠ Érvénytelen '{key}' érték: {v} ({lo}–{hi} közötti egész kell)")


def _load_bool(src: dict, dst: dict, key: str) -> None:
    """Helper: bool mezőt tölt be."""
    if key in src:
        if isinstance(src[key], bool):
            dst[key] = src[key]
        else:
            print(f"⚠ Érvénytelen '{key}' érték: {src[key]} (true/false kell)")


def _save_default_settings(path: str, settings: Dict[str, Any]) -> None:
    """Létrehozza az alapértelmezett settings.json fájlt."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        print(f"✓ Alapértelmezett '{path}' létrehozva.")
    except OSError as exc:
        print(f"✗ Nem sikerült létrehozni a '{path}' fájlt: {exc}")


# ============================================================
# TISZTA FÜGGVÉNYEK – ZÓNA SZÁMÍTÁS
# ============================================================

def calculate_power_zones(
    ftp: int,
    min_watt: int,
    max_watt: int,
    z1_pct: int,
    z2_pct: int,
) -> Dict[int, Tuple[int, int]]:
    """Kiszámítja a teljesítmény zóna határokat.

    Args:
        ftp: Funkcionális küszöbteljesítmény (W).
        min_watt: Minimális érvényes pozitív teljesítmény (W).
        max_watt: Maximális érvényes teljesítmény (W).
        z1_pct: Z1 felső határ az FTP %-ában.
        z2_pct: Z2 felső határ az FTP %-ában.

    Returns:
        Dict formátum: {0: (0,0), 1: (1, z1_max), 2: (z1_max+1, z2_max), 3: (z2_max+1, max_watt)}
    """
    z1_max = int(ftp * z1_pct / 100)
    z2_max = min(int(ftp * z2_pct / 100), max_watt)
    z1_max = min(z1_max, z2_max - 1)
    return {
        0: (0, 0),
        1: (1, z1_max),
        2: (z1_max + 1, z2_max),
        3: (z2_max + 1, max_watt),
    }


def calculate_hr_zones(
    max_hr: int,
    resting_hr: int,
    z1_pct: int,
    z2_pct: int,
) -> Dict[str, int]:
    """Kiszámítja a HR zóna határokat bpm-ben.

    Args:
        max_hr: Maximális szívfrekvencia (bpm).
        resting_hr: Pihenő szívfrekvencia (bpm); ez alatt Z0.
        z1_pct: Z1 felső határ a max_hr %-ában.
        z2_pct: Z2 felső határ a max_hr %-ában.

    Returns:
        Dict: {'resting': int, 'z1_max': int, 'z2_max': int}
    """
    return {
        "resting": resting_hr,
        "z1_max": int(max_hr * z1_pct / 100),
        "z2_max": int(max_hr * z2_pct / 100),
    }


def zone_for_power(power: float, zones: Dict[int, Tuple[int, int]]) -> int:
    """Meghatározza a teljesítmény zónát (0–3) az adott wattértékhez.

    Args:
        power: Teljesítmény wattban.
        zones: Zóna határok dict-je (calculate_power_zones kimenetele).

    Returns:
        Zóna szám (0–3).
    """
    if power == 0:
        return 0
    for zone_num in sorted(zones):
        lo, hi = zones[zone_num]
        if lo <= power <= hi:
            return zone_num
    return 3


def zone_for_hr(hr: int, hr_zones: Dict[str, int]) -> int:
    """Meghatározza a HR zónát (0–3) az adott bpm értékhez.

    Args:
        hr: Szívfrekvencia bpm-ben.
        hr_zones: HR zóna határok dict-je (calculate_hr_zones kimenetele).

    Returns:
        Zóna szám (0–3).
    """
    if hr <= 0 or hr < hr_zones["resting"]:
        return 0
    if hr < hr_zones["z1_max"]:
        return 1
    if hr < hr_zones["z2_max"]:
        return 2
    return 3


def is_valid_power(power: Any, min_watt: int, max_watt: int) -> bool:
    """Ellenőrzi, hogy az érték érvényes teljesítmény adat-e.

    Args:
        power: Az ellenőrizendő érték.
        min_watt: Minimális érvényes pozitív watt (0 és min_watt között elutasítva).
        max_watt: Maximális érvényes watt.

    Returns:
        True, ha érvényes teljesítmény adat.
    """
    if isinstance(power, bool):
        return False
    if not isinstance(power, (int, float)):
        return False
    if math.isnan(power) or math.isinf(power):
        return False
    if power < 0 or power > max_watt:
        return False
    if 0 < power < min_watt:
        return False
    return True

def is_valid_hr(hr: Any, valid_min_hr: int, valid_max_hr: int) -> bool:
    """Ellenőrzi, hogy az érték érvényes szívfrekvencia adat-e.

    Args:
        hr: Az ellenőrizendő érték.
        valid_min_hr: Minimális érvényes HR érték (bpm).
        valid_max_hr: Maximális érvényes HR érték (bpm).

    Returns:
        True, ha érvényes HR adat.
    """
    if isinstance(hr, bool):
        return False
    if not isinstance(hr, (int, float)):
        return False
    if math.isnan(hr) or math.isinf(hr):
        return False
    if hr < valid_min_hr or hr > valid_max_hr:
        return False
    return True

# ============================================================
# TISZTA FÜGGVÉNYEK – ÁTLAGSZÁMÍTÁS
# ============================================================

def compute_average(samples: deque) -> Optional[float]:
    """Kiszámítja a minták számtani átlagát.

    Args:
        samples: Mintákat tartalmazó deque.

    Returns:
        Az átlag float értéke, vagy None, ha nincs minta.
    """
    if not samples:
        return None
    return sum(samples) / len(samples)


# ============================================================
# TISZTA FÜGGVÉNYEK – ZÓNA LOGIKA (higher_wins, zone_mode)
# ============================================================

def higher_wins(zone_a: int, zone_b: int) -> int:
    """A két zóna közül a nagyobbat adja vissza.

    Args:
        zone_a: Első zóna (0–3).
        zone_b: Második zóna (0–3).

    Returns:
        A nagyobb zóna szám.
    """
    return max(zone_a, zone_b)


def apply_zone_mode(
    power_zone: Optional[int],
    hr_zone: Optional[int],
    zone_mode: str,
) -> Optional[int]:
    """A zone_mode alapján kombinálja a power és HR zónákat.

    Zóna módok:
        "power_only"  – csak a teljesítmény zóna dönt (HR figyelmen kívül)
        "hr_only"     – csak a HR zóna dönt (power figyelmen kívül)
        "higher_wins" – a kettő közül a nagyobb dönt

    Args:
        power_zone: Teljesítmény zóna (0–3), vagy None ha nem elérhető.
        hr_zone: HR zóna (0–3), vagy None ha nem elérhető.
        zone_mode: A kombinálási mód ("power_only", "hr_only", "higher_wins").

    Returns:
        A végső zóna szám (0–3), vagy None ha nincs elég adat.
    """
    if zone_mode == "power_only":
        return power_zone
    if zone_mode == "hr_only":
        return hr_zone
    # higher_wins: mindkét forrásból a nagyobb
    if power_zone is not None and hr_zone is not None:
        return higher_wins(power_zone, hr_zone)
    if power_zone is not None:
        return power_zone
    return hr_zone


# ============================================================
# COOLDOWN LOGIKA
# ============================================================

class CooldownController:
    """Cooldown logika kezelője zóna csökkentés esetén.

    Zóna csökkentésekor nem vált azonnal, hanem cooldown_seconds
    másodpercig vár. Zóna növelésekor azonnal vált, cooldown nélkül.

    Adaptív cooldown módosítások:
        - Nagy zónaesés (>= 2 szint) vagy 0W → cooldown felezés (gyorsabb leállás)
        - Pending zóna emelkedik → cooldown duplázás (lassabb emelkedés)

    Attribútumok:
        cooldown_seconds: A cooldown időtartama másodpercben.
        active: True, ha a cooldown timer fut.
        start_time: A cooldown indítási ideje (time.monotonic()).
        pending_zone: A cooldown lejárta után alkalmazandó zóna.
        can_halve: True, ha a cooldown felezés még elvégezhető.
        can_double: True, ha a cooldown duplázás még elvégezhető.
    """

    PRINT_INTERVAL = 10.0

    def __init__(self, cooldown_seconds: int) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.active = False
        self.start_time = 0.0
        self.pending_zone: Optional[int] = None
        self.can_halve = True
        self.can_double = False
        self._last_print = 0.0

    def process(
        self,
        current_zone: Optional[int],
        new_zone: int,
        zero_immediate: bool,
    ) -> Optional[int]:
        """Feldolgozza az új zóna javaslatot és alkalmazza a cooldown logikát.

        Args:
            current_zone: Az aktuális zóna (None = még nincs döntés).
            new_zone: Az új javasolt zóna (0–3).
            zero_immediate: True, ha 0W esetén azonnali leállás szükséges.

        Returns:
            A küldendő zóna szintje, ha változás szükséges; None egyébként.
        """
        now = time.monotonic()

        # Első döntés – nincs előző zóna
        if current_zone is None:
            self._reset()
            return new_zone

        # 0W azonnali leállás (zero_power_immediate=True)
        if new_zone == 0 and zero_immediate:
            if current_zone != 0:
                self._reset()
                print("✓ 0W detektálva: azonnali leállás (cooldown nélkül)")
                return 0
            return None

        # Aktív cooldown kezelése
        if self.active:
            return self._handle_active(current_zone, new_zone, now)

        # Nincs cooldown – normál zónaváltás logika
        if new_zone == current_zone:
            return None
        if new_zone > current_zone:
            return new_zone
        # Zóna csökkentés → cooldown indul
        return self._start(current_zone, new_zone, now)

    def _start(self, current_zone: int, new_zone: int, now: float) -> None:
        """Cooldown indítása zóna csökkentésnél."""
        self.active = True
        self.start_time = now
        self.pending_zone = new_zone
        self.can_halve = True
        self.can_double = False
        print(f"🕐 Cooldown indítva: {self.cooldown_seconds}s várakozás (cél: {new_zone})")
        # Nagy zónaesés esetén azonnali felezés
        if new_zone == 0 or (current_zone - new_zone >= 2):
            self._halve(now)
        return None

    def _handle_active(
        self, current_zone: int, new_zone: int, now: float
    ) -> Optional[int]:
        """Aktív cooldown feldolgozása."""
        # Zóna emelkedés → cooldown törlése
        if new_zone >= current_zone:
            self._reset()
            if new_zone > current_zone:
                print(f"✓ Teljesítmény emelkedés: cooldown törölve → zóna: {new_zone}")
                return new_zone
            return None

        elapsed = now - self.start_time

        # Cooldown lejárt
        if elapsed >= self.cooldown_seconds:
            target = new_zone
            self._reset()
            if target != current_zone:
                print(f"✓ Cooldown lejárt! Zóna váltás: {current_zone} → {target}")
                return target
            print("✓ Cooldown lejárt, nincs zónaváltás (már a célzónában)")
            return None

        remaining = self.cooldown_seconds - elapsed

        # Pending zóna frissítése + adaptív cooldown módosítás
        if new_zone != self.pending_zone:
            old_pending = self.pending_zone
            self.pending_zone = new_zone
            if old_pending is not None and new_zone > old_pending and self.can_double:
                self._double(now)
                remaining = self.cooldown_seconds - (now - self.start_time)
                print(f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})")
            elif (new_zone == 0 or (current_zone - new_zone >= 2)) and self.can_halve:
                self._halve(now)
                remaining = self.cooldown_seconds - (now - self.start_time)
                print(f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})")
            else:
                print(f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})")
            self._last_print = now
        elif now - self._last_print >= self.PRINT_INTERVAL:
            print(f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó: {self.pending_zone})")
            self._last_print = now

        return None

    def _halve(self, now: float) -> None:
        """Felezi a maradék cooldown időt."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = remaining / 2
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_halve = False
        self.can_double = True
        print(f"🕐 Cooldown felezve: {remaining:.0f}s → {new_remaining:.0f}s")

    def _double(self, now: float) -> None:
        """Duplázza a maradék cooldown időt."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = min(remaining * 2, float(self.cooldown_seconds))
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_double = False
        self.can_halve = True
        print(f"🕐 Cooldown duplázva: {remaining:.0f}s → {new_remaining:.0f}s")

    def _reset(self) -> None:
        """Törli a cooldown állapotát."""
        self.active = False
        self.pending_zone = None
        self.can_halve = True
        self.can_double = False


# ============================================================
# POWER ÁTLAGOLÁS
# ============================================================

class PowerAverager:
    """Gördülő átlagot számít a bejövő teljesítmény mintákból.

    buffer_rate_hz mintát vár másodpercenként (alapértelmezett: 4 Hz),
    és buffer_seconds másodpercnyi ablakot tart. Az effective_minimum
    automatikusan alkalmazkodik a valódi buffer méretéhez, így akkor is
    számol átlagot, ha kevesebb adat érkezik, mint minimum_samples.

    Attribútumok:
        buffer: Mintákat tároló deque (maxlen = buffer_seconds × buffer_rate_hz).
        minimum_samples: Kívánt minimum mintaszám érvényes átlaghoz.
        effective_minimum: Ténylegesen alkalmazott minimum (max: buffersize // 2).
        buffersize: A buffer maximális mérete.
    """
    def __init__(self, buffer_seconds: int, minimum_samples: int,
        buffer_rate_hz: int = 4) -> None:
        rate = max(1, int(buffer_rate_hz))
        self.buffersize = max(1, int(buffer_seconds) * rate)
        self.buffer: deque = deque(maxlen=self.buffersize)
        self.minimum_samples = minimum_samples
        # Védelem: effective_minimum soha nem nagyobb, mint a buffer fele
        self.effective_minimum = min(self.minimum_samples, max(1, self.buffersize // 2))

    def add_sample(self, power: float) -> Optional[float]:
        self.buffer.append(power)
        if len(self.buffer) < self.effective_minimum:
            logging.debug(
                "Power adatok gyűjtése: %d/%d (effective min)",
                len(self.buffer), self.effective_minimum,
            )
            return None
        return compute_average(self.buffer)

    def clear(self) -> None:
        """Törli az összes pufferelt mintát."""
        self.buffer.clear()


# ============================================================
# HR ÁTLAGOLÁS
# ============================================================

class HRAverager:
    """Gördülő átlagot számít a bejövő HR mintákból.

    buffer_rate_hz mintát vár másodpercenként (alapértelmezett: 4 Hz),
    és buffer_seconds másodpercnyi ablakot tart. Az effective_minimum
    automatikusan alkalmazkodik a valódi buffer méretéhez, így akkor is
    számol átlagot, ha kevesebb adat érkezik, mint minimum_samples.

    Attribútumok:
        buffer: Mintákat tároló deque (maxlen = buffer_seconds × buffer_rate_hz).
        minimum_samples: Kívánt minimum mintaszám érvényes átlaghoz.
        effective_minimum: Ténylegesen alkalmazott minimum (max: buffersize // 2).
        buffersize: A buffer maximális mérete.
    """
    def __init__(self, buffer_seconds: int, minimum_samples: int,
        buffer_rate_hz: int = 4) -> None:
        rate = max(1, int(buffer_rate_hz))
        self.buffersize = max(1, int(buffer_seconds) * rate)
        self.buffer: deque = deque(maxlen=self.buffersize)
        self.minimum_samples = minimum_samples
        # Védelem: effective_minimum soha nem nagyobb, mint a buffer fele
        self.effective_minimum = min(self.minimum_samples, max(1, self.buffersize // 2))

    def add_sample(self, hr: int) -> Optional[float]:
        """Új minta hozzáadása és az átlag visszaadása, ha elég minta van."""
        self.buffer.append(hr)
        if len(self.buffer) < self.effective_minimum:
            logging.debug(
                "HR adatok gyűjtése: %d/%d (effective min)",
                len(self.buffer), self.effective_minimum,
            )
            return None
        return compute_average(self.buffer)

    def clear(self) -> None:
        """Törli az összes pufferelt mintát."""
        self.buffer.clear()

# ============================================================
# KONZOLOS KIÍRÁS (throttle-olt)
# ============================================================

class ConsolePrinter:
    """Throttle-olt konzol kiírás – ugyanaz az üzenet nem jelenhet meg túl sűrűn.

    Minden üzenettípushoz (key) külön időzítőt tart. Az üzenet csak
    akkor kerül kiírásra, ha az utolsó kiírás óta legalább interval
    másodperc telt el.

    Attribútumok:
        _last_times: Utolsó kiírás ideje üzenetkulcsonként.
    """

    def __init__(self) -> None:
        self._last_times: Dict[str, float] = {}

    def print(self, key: str, message: str, interval: float = 1.0) -> bool:
        """Kiírja az üzenetet, ha az interval eltelt.

        Args:
            key: Egyedi kulcs az üzenet azonosításához (pl. "power_raw").
            message: A kiírandó szöveg.
            interval: Minimális másodpercek száma két azonos kulcsú kiírás között.

        Returns:
            True, ha az üzenet kiírásra kerül; False, ha throttle-olt.
        """
        now = time.monotonic()
        if now - self._last_times.get(key, 0.0) >= interval:
            print(message)
            self._last_times[key] = now
            return True
        return False


# ============================================================
# MEGOSZTOTT ÁLLAPOT
# ============================================================

class ControllerState:
    """A vezérlő megosztott állapota, asyncio.Lock-kal védve.

    Minden olyan mezőt tartalmaz, amelyet több asyncio korrutin is olvas
    vagy módosít. A lock biztosítja, hogy az olvasás-módosítás-írás
    műveletek atomikusak legyenek.

    Attribútumok:
        current_zone: Az aktuálisan aktív ventilátor zóna (None = nincs döntés még).
        current_power_zone: A legutóbb kiszámított power zóna.
        current_hr_zone: A legutóbb kiszámított HR zóna.
        current_avg_power: A legutóbbi átlagolt teljesítmény (W).
        current_avg_hr: A legutóbbi átlagolt HR (bpm).
        last_power_time: Utolsó power adat érkezési ideje (monotonic).
        last_hr_time: Utolsó HR adat érkezési ideje (monotonic), vagy None.
        lock: asyncio.Lock a párhuzamos módosítások ellen.
    """

    def __init__(self) -> None:
        self.current_zone: Optional[int] = None
        self.current_power_zone: Optional[int] = None
        self.current_hr_zone: Optional[int] = None
        self.current_avg_power: Optional[float] = None
        self.current_avg_hr: Optional[float] = None
        self.last_power_time: float = time.monotonic()
        self.last_hr_time: Optional[float] = None
        self.lock = asyncio.Lock()


# ============================================================
# ZÓNA ELKÜLDÉSE (helper)
# ============================================================

async def send_zone(zone: int, zone_queue: asyncio.Queue) -> None:
    """Zóna parancsot küld a BLE fan kimenet queue-ba.

    Ha a queue teli (maxsize=1), a régi parancsot elveti és az újat
    teszi be, hogy mindig a legfrissebb zóna kerüljön küldésre.

    Args:
        zone: Ventilátor zóna szintje (0–3).
        zone_queue: A BLE fan output asyncio.Queue-ja.
    """
    try:
        zone_queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        zone_queue.put_nowait(zone)
    except asyncio.QueueFull:
        logger.warning(f"Zóna queue teli, parancs elvetve: LEVEL:{zone}")


# ============================================================
# BLE VENTILÁTOR KIMENET VEZÉRLŐ
# ============================================================

class BLEFanOutputController:
    """BLE alapú ventilátor kimenet vezérlő (LEVEL:N parancsok küldése).

    Asyncio korrutin alapú implementáció. A parancsokat egy
    asyncio.Queue-n keresztül fogadja, és a BLE GATT karakterisztikára
    írja ki az ESP32 vezérlőnek. PIN autentikáció is támogatott.

    Attribútumok:
        device_name: A keresett BLE eszköz neve.
        is_connected: True, ha a BLE kapcsolat aktív.
        last_sent: Az utoljára sikeresen elküldött zóna szint.
    """

    RETRY_RESET_SECONDS = 30
    DISCONNECT_TIMEOUT = 5.0

    def __init__(self, settings: Dict[str, Any]) -> None:
        ble = settings["ble"]
        self.device_name: str = ble["device_name"]
        self.scan_timeout: int = ble["scan_timeout"]
        self.connection_timeout: int = ble["connection_timeout"]
        self.reconnect_interval: int = ble["reconnect_interval"]
        self.max_retries: int = ble["max_retries"]
        self.command_timeout: int = ble["command_timeout"]
        self.service_uuid: str = ble["service_uuid"]
        self.characteristic_uuid: str = ble["characteristic_uuid"]
        self.pin_code: Optional[str] = ble.get("pin_code")

        self.is_connected: bool = False
        self.last_sent: Optional[int] = None
        self._client: Optional[Any] = None
        self._device_address: Optional[str] = None
        self._retry_count: int = 0
        self._retry_reset_time: Optional[float] = None
        self._auth_failed: bool = False

    async def run(self, zone_queue: asyncio.Queue) -> None:
        """A BLE fan kimenet fő korrutinja – olvassa a zone_queue-t és küldi a parancsokat.

        Indításkor megpróbál csatlakozni a BLE eszközhöz, majd folyamatosan
        olvassa a zone_queue-t és elküldi a zóna parancsokat.

        Args:
            zone_queue: asyncio.Queue, amelyből a zóna parancsokat olvassa.
        """
        if not _BLEAK_AVAILABLE:
            logger.error("BLE Fan: bleak könyvtár nem elérhető – BLE kimenet letiltva!")
            return

        logger.info("BLE Fan Output korrutin elindítva")
        await self._initial_connect()

        while True:
            zone = await zone_queue.get()
            await self._send_zone(zone)

    async def _initial_connect(self) -> None:
        """Kezdeti BLE csatlakozás indításkor (hiba esetén folytatja)."""
        ok = await self._scan_and_connect()
        if not ok:
            logger.warning(
                "BLE Fan: kezdeti csatlakozás sikertelen, automatikus újrapróbálkozás parancs küldéskor."
            )

    async def _scan_and_connect(self) -> bool:
        """BLE eszköz keresése és csatlakozás.

        Returns:
            True, ha a csatlakozás sikeres.
        """
        if not _BLEAK_AVAILABLE:
            return False

        try:
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for d in devices:
                if d.name == self.device_name:
                    self._device_address = d.address
                    logger.info(f"BLE Fan eszköz megtalálva: {d.name} ({d.address})")
                    return await self._connect()

            logger.error(f"BLE Fan eszköz nem található: {self.device_name}")
            return False

        except Exception as exc:
            logger.error(f"BLE Fan keresési hiba: {exc}")
            return False

    async def _connect(self) -> bool:
        """Csatlakozás a korábban megtalált BLE eszközhöz.

        Returns:
            True, ha a csatlakozás sikeres.
        """
        if not _BLEAK_AVAILABLE:
            return False
        if not self._device_address:
            return False

        try:
            client = self._client
            if client and client.is_connected:
                return True

            client = BleakClient(
                self._device_address,
                timeout=self.connection_timeout,
                disconnected_callback=self._on_disconnect,
            )
            self._client = client

            await client.connect()

            if self.pin_code is not None:
                ok = await self._authenticate()
                if not ok:
                    return False

            self.is_connected = True
            self._retry_count = 0
            self._retry_reset_time = None
            self.last_sent = None
            logger.info(f"BLE Fan csatlakozva: {self._device_address}")
            return True

        except Exception as exc:
            logger.error(f"BLE Fan csatlakozási hiba: {exc}")
            self.is_connected = False
            self._client = None
            return False

    async def _authenticate(self) -> bool:
        """Alkalmazás szintű BLE PIN autentikáció.

        Returns:
            True, ha az autentikáció sikeres (vagy timeout esetén is folytatja).
        """
        client = self._client
        if client is None:
            logger.error("BLE AUTH hiba: nincs aktív BLE kliens")
            return False

        try:
            auth_event = asyncio.Event()
            auth_result: list = [None]

            def _notify_cb(sender: Any, data: bytes) -> None:
                auth_result[0] = data.decode("utf-8", errors="replace").strip()
                auth_event.set()

            await client.start_notify(self.characteristic_uuid, _notify_cb)
            try:
                try:
                    await asyncio.wait_for(
                        client.write_gatt_char(
                            self.characteristic_uuid,
                            f"AUTH:{self.pin_code}".encode("utf-8"),
                        ),
                        timeout=self.command_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.error("BLE AUTH write timeout")
                    return False

                try:
                    await asyncio.wait_for(
                        auth_event.wait(),
                        timeout=self.command_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning("BLE AUTH válasz timeout - folytatás autentikáció nélkül")
                    return True

                resp = auth_result[0]
                if resp is None:
                    logger.error("BLE AUTH: üres válasz")
                    return False
                if resp == "AUTH_OK":
                    logger.info("BLE AUTH sikeres")
                    return True
                if resp in ("AUTH_FAIL", "AUTH_LOCKED"):
                    logger.error(
                        f"BLE AUTH sikertelen: {resp} - ellenorizd a pin_code erteket!"
                    )
                    self._auth_failed = True
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    return False

                logger.warning(f"BLE AUTH ismeretlen válasz: {resp} - folytatás")
                return True

            finally:
                try:
                    await client.stop_notify(self.characteristic_uuid)
                except Exception:
                    pass

        except Exception as exc:
            logger.error(f"BLE AUTH hiba: {exc}")
            return False

    def _on_disconnect(self, client: Any) -> None:
        """Callback: BLE kapcsolat váratlan megszakadásakor."""
        logger.warning("BLE Fan kapcsolat megszakadt")
        self.is_connected = False
        self.last_sent = None
        self._client = None

    async def _send_zone(self, zone: int) -> None:
        """Zóna parancs küldése BLE-n, szükség esetén újracsatlakozással.

        Args:
            zone: Ventilátor zóna szintje (0–3).
        """
        if self._auth_failed:
            logger.error("BLE Fan: AUTH hiba, parancs elutasítva! Javítsd a pin_code-ot.")
            return

        if self.last_sent == zone and self.is_connected:
            return

        if not self.is_connected:
            ok = await self._reconnect()
            if not ok:
                return

        await self._write_level(zone)

    async def _reconnect(self) -> bool:
        """Újracsatlakozás a BLE eszközhöz, retry logikával.

        Returns:
            True, ha az újracsatlakozás sikeres.
        """
        now = time.monotonic()

        if self._retry_reset_time is not None:
            elapsed = now - self._retry_reset_time
            if elapsed >= self.RETRY_RESET_SECONDS:
                self._retry_count = 0
                self._retry_reset_time = None
            else:
                await asyncio.sleep(
                    min(self.RETRY_RESET_SECONDS - elapsed, self.reconnect_interval)
                )
                return False

        if self._retry_count >= self.max_retries:
            if self._retry_reset_time is None:
                self._retry_reset_time = now
                logger.warning(
                    f"BLE Fan: max újracsatlakozás elérve ({self.max_retries})! "
                    f"{self.RETRY_RESET_SECONDS}s múlva újrapróbálkozik..."
                )
            return False

        self._retry_count += 1
        logger.info(
            f"BLE Fan újracsatlakozás... ({self._retry_count}/{self.max_retries})"
        )

        if self._device_address:
            return await self._connect()
        return await self._scan_and_connect()

    async def _write_level(self, zone: int) -> None:
        """LEVEL:N parancs írása a BLE GATT karakterisztikára.

        Args:
            zone: Ventilátor zóna szintje (0–3).
        """
        client = self._client
        if client is None or not client.is_connected:
            self.is_connected = False
            return

        try:
            msg = f"LEVEL:{zone}"
            await asyncio.wait_for(
                client.write_gatt_char(
                    self.characteristic_uuid,
                    msg.encode("utf-8"),
                ),
                timeout=self.command_timeout,
            )
            self.last_sent = zone
            logger.info(f"BLE Fan parancs elküldve: {msg}")

        except asyncio.TimeoutError:
            logger.error(f"BLE Fan parancs küldés timeout ({self.command_timeout}s)")
            self.is_connected = False

        except Exception as exc:
            logger.error(f"BLE Fan küldési hiba: {exc}")
            self.is_connected = False

    async def disconnect(self) -> None:
        """Bontja a BLE kapcsolatot és felszabadítja a klienst."""
        client = self._client
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=self.DISCONNECT_TIMEOUT,
                )
            except Exception:
                pass
            finally:
                self.is_connected = False
                self._client = None

# ============================================================
# ANT+ BEMENŐ ADATKEZELÉS
# ============================================================

class ANTPlusInputHandler:
    """ANT+ power és HR adatforrás kezelője saját daemon szálban.

    Az openant könyvtár blokkoló API-t használ, ezért saját daemon szálban fut.
    Az érkező adatokat az asyncio event loop-ba hídalkotja
    (asyncio.run_coroutine_threadsafe) és az asyncio queue-kba teszi.

    Attribútumok:
        power_queue: asyncio.Queue a power adatokhoz.
        hr_queue: asyncio.Queue a HR adatokhoz.
        loop: A fő asyncio event loop referenciája.
    """

    RECONNECT_DELAY = 5
    MAX_RETRIES = 10
    MAX_RETRY_COOLDOWN = 30

    def __init__(
        self,
        settings: Dict[str, Any],
        power_queue: asyncio.Queue,
        hr_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.settings = settings
        self.ds = settings["data_source"]
        self.hr_enabled = settings.get("heart_rate_zones", {}).get("enabled", False)
        self.power_queue = power_queue
        self.hr_queue = hr_queue
        self.loop = loop
        self._running = threading.Event()
        self._node: Any = None
        self._devices: list = []
        self._last_data = 0.0

    def start(self) -> threading.Thread:
        """Elindítja az ANT+ daemon szálat.

        Returns:
            A létrehozott daemon threading.Thread objektum.
        """
        self._running.set()
        t = threading.Thread(
            target=self._thread_loop, daemon=True, name="ANTPlus-Thread"
        )
        t.start()
        logger.info("ANT+ szál elindítva")
        return t

    def stop(self) -> None:
        """Leállítja az ANT+ szálat és az ANT+ node-ot."""
        self._running.clear()
        self._stop_node()

    def _put_power(self, power: float) -> None:
        """Power értéket tesz az asyncio queue-ba (thread-safe)."""
        asyncio.run_coroutine_threadsafe(self.power_queue.put(power), self.loop)

    def _put_hr(self, hr: int) -> None:
        """HR értéket tesz az asyncio queue-ba (thread-safe)."""
        asyncio.run_coroutine_threadsafe(self.hr_queue.put(hr), self.loop)

    def _on_data(self, page: Any, page_name: str, data: Any) -> None:
        """ANT+ adatcsomag callback – power és HR adatokat irányít a queue-kba."""
        if not _ANTPLUS_AVAILABLE:
            return
        self._last_data = time.monotonic()
        if isinstance(data, PowerData):
            self._put_power(data.instantaneous_power)
        elif isinstance(data, HeartRateData):
            self._put_hr(data.heart_rate)

    def _init_node(self) -> None:
        """Inicializálja az ANT+ node-ot és regisztrálja az eszközöket."""
        if not _ANTPLUS_AVAILABLE:
            raise RuntimeError("openant könyvtár nem elérhető")
        self._node = Node()
        self._node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
        self._devices = []

        if self.ds.get("power_source", "antplus") == "antplus":
            meter = PowerMeter(self._node)
            meter.on_found = lambda: None
            meter.on_device_data = self._on_data
            self._devices.append(meter)

        if self.ds.get("hr_source", "antplus") == "antplus" and self.hr_enabled:
            hr_monitor = HeartRate(self._node)
            hr_monitor.on_found = lambda: None
            hr_monitor.on_device_data = self._on_data
            self._devices.append(hr_monitor)

    def _stop_node(self) -> None:
        """Leállítja és felszabadítja az ANT+ node-ot."""
        try:
            for d in self._devices:
                try:
                    d.close_channel()
                except Exception:
                    pass
            if self._node:
                self._node.stop()
                self._node = None
            self._devices = []
        except Exception:
            pass

    def _thread_loop(self) -> None:
        """Az ANT+ szál fő ciklusa – újracsatlakozási logikával."""
        retry_count = 0
        while self._running.is_set():
            try:
                self._init_node()
                self._last_data = 0.0
                self._node.start()  # Blokkoló hívás – itt vár, amíg az ANT+ fut

                if not self._running.is_set():
                    break

                # Ha volt sikeres adat, reseteljük a számolót
                if self._last_data > 0:
                    retry_count = 0
                else:
                    retry_count += 1
                logger.warning(
                    f"ANT+ node leállt, újraindítás... ({retry_count}/{self.MAX_RETRIES})"
                )

            except Exception as exc:
                if not self._running.is_set():
                    break
                retry_count += 1
                logger.warning(f"ANT+ hiba ({retry_count}/{self.MAX_RETRIES}): {exc}")

            if not self._running.is_set():
                break

            if retry_count >= self.MAX_RETRIES:
                logger.warning(
                    f"ANT+ max próbálkozások ({self.MAX_RETRIES}), "
                    f"{self.MAX_RETRY_COOLDOWN}s várakozás..."
                )
                time.sleep(self.MAX_RETRY_COOLDOWN)
                if not self._running.is_set():
                    break
                retry_count = 0

            self._stop_node()
            time.sleep(self.RECONNECT_DELAY)

        self._stop_node()
        logger.info("ANT+ szál leállítva")


# ============================================================
# BLE POWER BEMENŐ ADATKEZELÉS
# ============================================================

class BLEPowerInputHandler:
    """BLE Cycling Power Service (UUID: 0x1818) fogadó.

    Asyncio korrutin alapú implementáció. Standard BLE Cycling Power
    Measurement (UUID: 0x2A63) notificationöket fogad, és a nyers
    teljesítmény értékeket az asyncio queue-ba teszi.

    Parse: flags (2 bájt LE) → instantaneous power (2 bájt LE, signed int16).

    Attribútumok:
        device_name: A keresett BLE power meter neve.
        is_connected: True, ha a BLE kapcsolat aktív.
    """

    CYCLING_POWER_MEASUREMENT_UUID = "00002a63-0000-1000-8000-00805f9b34fb"
    RETRY_RESET_SECONDS = 30

    def __init__(self, settings: Dict[str, Any], power_queue: asyncio.Queue) -> None:
        ds = settings["data_source"]
        self.device_name: Optional[str] = ds.get("ble_power_device_name")
        self.scan_timeout: int = ds.get("ble_power_scan_timeout", 10)
        self.reconnect_interval: int = ds.get("ble_power_reconnect_interval", 5)
        self.max_retries: int = ds.get("ble_power_max_retries", 10)
        self.power_queue = power_queue
        self.is_connected = False
        self._retry_count = 0

    async def run(self) -> None:
        """A BLE power fogadó fő korrutinja – újracsatlakozási logikával."""
        if not _BLEAK_AVAILABLE:
            logger.error("BLE Power: bleak könyvtár nem elérhető!")
            return
        if not self.device_name:
            logger.warning("BLE Power: nincs 'ble_power_device_name' megadva, leállítva.")
            return

        loop = asyncio.get_event_loop()
        logger.info(f"BLE Power fogadó elindítva: {self.device_name}")

        while True:
            try:
                await self._scan_and_subscribe(loop)
                self._retry_count = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._retry_count += 1
                self.is_connected = False
                logger.warning(
                    f"BLE Power kapcsolat hiba ({self._retry_count}/{self.max_retries}): {exc}"
                )
                if self._retry_count >= self.max_retries:
                    logger.warning(
                        f"BLE Power: max újracsatlakozás elérve, "
                        f"{self.RETRY_RESET_SECONDS}s várakozás..."
                    )
                    await asyncio.sleep(self.RETRY_RESET_SECONDS)
                    self._retry_count = 0
                else:
                    await asyncio.sleep(self.reconnect_interval)

    async def _scan_and_subscribe(self, loop: asyncio.AbstractEventLoop) -> None:
        if not _BLEAK_AVAILABLE:
            return
        """BLE power meter keresése, csatlakozás, notification feliratkozás.

        Args:
            loop: A fő asyncio event loop (thread-safe put számára).
        """
        logger.info(f"BLE Power keresés: {self.device_name}...")
        devices = await BleakScanner.discover(timeout=self.scan_timeout)
        addr = None
        for d in devices:
            if d.name == self.device_name:
                addr = d.address
                logger.info(f"BLE Power eszköz megtalálva: {d.name} ({d.address})")
                break

        if not addr:
            raise Exception(f"BLE Power eszköz nem található: {self.device_name}")

        async with BleakClient(addr) as client:
            self.is_connected = True
            self._retry_count = 0
            logger.info(f"BLE Power csatlakozva: {addr}")

            def _handler(sender: Any, data: bytes) -> None:
                try:
                    if len(data) < 4:
                        return
                    # flags: 2 bájt LE; instantaneous power: 2 bájt LE signed int16
                    power = int.from_bytes(data[2:4], byteorder="little", signed=True)
                    future = asyncio.run_coroutine_threadsafe(
                        self.power_queue.put(power), loop
                    )
                    future.add_done_callback(
                        lambda f: logger.debug(f"BLE Power queue put hiba: {f.exception()}")
                        if not f.cancelled() and f.exception() else None
                    )
                except Exception as exc:
                    logger.warning(f"BLE Power notification feldolgozási hiba: {exc}")

            await client.start_notify(self.CYCLING_POWER_MEASUREMENT_UUID, _handler)
            while client.is_connected:
                await asyncio.sleep(1)
            try:
                await client.stop_notify(self.CYCLING_POWER_MEASUREMENT_UUID)
            except Exception:
                pass

        self.is_connected = False


# ============================================================
# BLE HR BEMENŐ ADATKEZELÉS
# ============================================================

class BLEHRInputHandler:
    """BLE Heart Rate Service (UUID: 0x180D) fogadó.

    Asyncio korrutin alapú implementáció. Standard BLE Heart Rate
    Measurement (UUID: 0x2A37) notificationöket fogad, és a szívfrekvencia
    értékeket az asyncio queue-ba teszi.

    Parse: flags byte bit 0 → 0 = 8-bites HR, 1 = 16-bites HR.

    Attribútumok:
        device_name: A keresett BLE HR eszköz neve.
        is_connected: True, ha a BLE kapcsolat aktív.
    """

    HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
    RETRY_RESET_SECONDS = 30

    def __init__(self, settings: Dict[str, Any], hr_queue: asyncio.Queue) -> None:
        ds = settings["data_source"]
        self.device_name: Optional[str] = ds.get("ble_hr_device_name")
        self.scan_timeout: int = ds.get("ble_hr_scan_timeout", 10)
        self.reconnect_interval: int = ds.get("ble_hr_reconnect_interval", 5)
        self.max_retries: int = ds.get("ble_hr_max_retries", 10)
        self.hr_queue = hr_queue
        self.is_connected = False
        self._retry_count = 0

    async def run(self) -> None:
        """A BLE HR fogadó fő korrutinja – újracsatlakozási logikával."""
        if not _BLEAK_AVAILABLE:
            logger.error("BLE HR: bleak könyvtár nem elérhető!")
            return
        if not self.device_name:
            logger.warning("BLE HR: nincs 'ble_hr_device_name' megadva, leállítva.")
            return

        loop = asyncio.get_event_loop()
        logger.info(f"BLE HR fogadó elindítva: {self.device_name}")

        while True:
            try:
                await self._scan_and_subscribe(loop)
                self._retry_count = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._retry_count += 1
                self.is_connected = False
                logger.warning(
                    f"BLE HR kapcsolat hiba ({self._retry_count}/{self.max_retries}): {exc}"
                )
                if self._retry_count >= self.max_retries:
                    logger.warning(
                        f"BLE HR: max újracsatlakozás elérve, "
                        f"{self.RETRY_RESET_SECONDS}s várakozás..."
                    )
                    await asyncio.sleep(self.RETRY_RESET_SECONDS)
                    self._retry_count = 0
                else:
                    await asyncio.sleep(self.reconnect_interval)

    async def _scan_and_subscribe(self, loop: asyncio.AbstractEventLoop) -> None:
        if not _BLEAK_AVAILABLE:
            return
        """BLE HR eszköz keresése, csatlakozás, notification feliratkozás.

        Args:
            loop: A fő asyncio event loop (thread-safe put számára).
        """
        logger.info(f"BLE HR keresés: {self.device_name}...")
        devices = await BleakScanner.discover(timeout=self.scan_timeout)
        addr = None
        for d in devices:
            if d.name == self.device_name:
                addr = d.address
                logger.info(f"BLE HR eszköz megtalálva: {d.name} ({d.address})")
                break

        if not addr:
            raise Exception(f"BLE HR eszköz nem található: {self.device_name}")

        async with BleakClient(addr) as client:
            self.is_connected = True
            self._retry_count = 0
            logger.info(f"BLE HR csatlakozva: {addr}")

            def _handler(sender: Any, data: bytes) -> None:
                try:
                    if len(data) < 2:
                        return
                    flags = data[0]
                    # bit 0: 0 = 8-bites HR, 1 = 16-bites HR
                    if flags & 0x01:
                        if len(data) < 3:
                            return
                        hr = int.from_bytes(data[1:3], byteorder="little")
                    else:
                        hr = data[1]
                    future = asyncio.run_coroutine_threadsafe(
                        self.hr_queue.put(hr), loop
                    )
                    future.add_done_callback(
                        lambda f: logger.debug(f"BLE HR queue put hiba: {f.exception()}")
                        if not f.cancelled() and f.exception() else None
                    )
                except Exception as exc:
                    logger.warning(f"BLE HR notification feldolgozási hiba: {exc}")

            await client.start_notify(self.HEART_RATE_MEASUREMENT_UUID, _handler)
            while client.is_connected:
                await asyncio.sleep(1)
            try:
                await client.stop_notify(self.HEART_RATE_MEASUREMENT_UUID)
            except Exception:
                pass

        self.is_connected = False


# ============================================================
# ZWIFT UDP BEMENŐ ADATKEZELÉS
# ============================================================

class ZwiftUDPInputHandler:
    """Zwift UDP adatforrás fogadó – asyncio DatagramProtocol alapú.

    A zwift-udp-monitor programból érkező JSON csomagokat fogadja UDP-n.
    Asyncio DatagramProtocol alapú implementáció, teljesen non-blocking.
    Érvényes power és HR értékeket az asyncio queue-kba teszi.

    JSON formátum:
        {"power": int, "heartrate": int}

    Attribútumok:
        process_power: True, ha a power adatokat kell feldolgozni.
        process_hr: True, ha a HR adatokat kell feldolgozni.
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        power_queue: asyncio.Queue,
        hr_queue: asyncio.Queue,
    ) -> None:
        ds = settings["data_source"]
        self.settings = settings
        self.host: str = ds.get("zwift_udp_host", "127.0.0.1")
        self.port: int = ds.get("zwift_udp_port", 7878)
        self.power_queue = power_queue
        self.hr_queue = hr_queue
        self.process_power: bool = ds.get("power_source") == "zwift_udp"
        hr_enabled = settings.get("heart_rate_zones", {}).get("enabled", False)
        self.process_hr: bool = ds.get("hr_source") == "zwift_udp" and hr_enabled
        self._transport: Any = None

    async def run(self) -> None:
        """A Zwift UDP fogadó fő korrutinja – asyncio DatagramProtocol-t indít."""
        loop = asyncio.get_event_loop()
        logger.info(f"Zwift UDP fogadó elindítva: {self.host}:{self.port}")

        handler = self

        class _Protocol(asyncio.DatagramProtocol):
            def connection_made(self, transport: Any) -> None:
                logger.info(f"Zwift UDP socket kötve: {handler.host}:{handler.port}")
                handler._transport = transport

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                handler._process_packet(data)

            def error_received(self, exc: Exception) -> None:
                logger.warning(f"Zwift UDP hiba: {exc}")

            def connection_lost(self, exc: Optional[Exception]) -> None:
                logger.info("Zwift UDP kapcsolat lezárva")

        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Protocol,
                local_addr=(self.host, self.port),
            )
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                transport.close()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.error(f"Zwift UDP bind hiba: {exc}")

    def _process_packet(self, raw: bytes) -> None:
        """JSON csomag feldolgozása – validáció és queue-ba helyezés.

        Érvénytelen JSON vagy tartományon kívüli értékek figyelmen kívül maradnak.
        A datagram_received callback az asyncio event loop-ból hívódik, ezért
        az asyncio queue-ba helyezés biztonságos put_nowait-tel.

        Args:
            raw: A nyers UDP csomag bájtjai.
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            return

        if self.process_power and "power" in data:
            p = data["power"]
            if isinstance(p, (int, float)) and not isinstance(p, bool) and 0 <= p <= 2500:
                try:
                    self.power_queue.put_nowait(int(p))
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: power queue teli, adat elvetve")
            else:
                logger.debug(f"Zwift UDP: érvénytelen power: {p}")

        if self.process_hr and "heartrate" in data:
            hrz = self.settings.get("heart_rate_zones", {})
            valid_min_hr: int = hrz.get("valid_min_hr", 30)
            valid_max_hr: int = hrz.get("valid_max_hr", 220)

            h = data["heartrate"]
            if is_valid_hr(h, valid_min_hr, valid_max_hr):
                try:
                    self.hr_queue.put_nowait(int(h))
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: hr queue teli, adat elvetve")
            else:
                logger.debug(f"Zwift UDP: érvénytelen heartrate: {h}")

# ============================================================
# POWER FELDOLGOZÓ KORRUTIN
# ============================================================

async def power_processor_task(
    raw_power_queue: asyncio.Queue,
    state: ControllerState,
    zone_event: asyncio.Event,
    power_averager: PowerAverager,
    printer: ConsolePrinter,
    settings: Dict[str, Any],
    power_zones: Dict[int, Tuple[int, int]],
) -> None:
    """Teljesítmény adatok feldolgozása – validálás, átlagolás, állapot frissítés.

    Olvassa a raw_power_queue-t, validálja a beérkező watt értékeket,
    gördülő átlagot számít, meghatározza a zónát, frissíti a megosztott
    állapotot, majd jelzi a zone_event-tel, hogy zóna újraszámítás szükséges.

    Args:
        raw_power_queue: Nyers power adatok asyncio.Queue-ja.
        state: A megosztott vezérlő állapot.
        zone_event: asyncio.Event – beállítja, ha új átlag áll rendelkezésre.
        power_averager: PowerAverager példány.
        printer: ThrottledPrinter a konzol kiíráshoz.
        settings: Betöltött beállítások dict-je.
        power_zones: Kiszámított power zóna határok.
    """
    min_watt = settings["min_watt"]
    max_watt = settings["max_watt"]
    hr_enabled = settings.get("heart_rate_zones", {}).get("enabled", False)
    zone_mode = (
        settings["heart_rate_zones"].get("zone_mode", "power_only")
        if hr_enabled else "power_only"
    )

    logger.info("Power processor korrutin elindítva")

    while True:
        power = await raw_power_queue.get()

        if not is_valid_power(power, min_watt, max_watt):
            printer.print("invalid_power", "⚠ FIGYELMEZTETÉS: Érvénytelen power adat!")
            continue

        power = int(power)
        now = time.monotonic()

        if zone_mode != "higher_wins":
            printer.print("power_raw", f"⚡ Teljesítmény: {power} watt")

        avg_power = power_averager.add_sample(power)
        if avg_power is None:
            continue  # Még nincs elég minta

        avg_power = round(avg_power)
        new_power_zone = zone_for_power(avg_power, power_zones)

        if zone_mode == "higher_wins":
            printer.print(
                "power_avg_hw",
                f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone} | Higher Wins!",
            )
        else:
            printer.print(
                "power_avg",
                f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone}",
            )

        async with state.lock:
            state.last_power_time = now
            state.current_power_zone = new_power_zone
            state.current_avg_power = avg_power

        zone_event.set()  # Zone controller újraszámítást igényel


# ============================================================
# HR FELDOLGOZÓ KORRUTIN
# ============================================================

async def hr_processor_task(
    raw_hr_queue: asyncio.Queue,
    state: ControllerState,
    zone_event: asyncio.Event,
    hr_averager: HRAverager,
    printer: ConsolePrinter,
    settings: Dict[str, Any],
    hr_zones: Dict[str, int],
) -> None:
    
    """HR adatok feldolgozása – validálás, átlagolás, állapot frissítés.

    Olvassa a raw_hr_queue-t, validálja a bpm értékeket, gördülő átlagot
    számít, meghatározza a HR zónát, frissíti a megosztott állapotot, majd
    jelzi a zone_event-tel, hogy zóna újraszámítás szükséges.

    Frissíti a state.last_hr_time mezőt, amelyet a dropout checker
    hr_only és higher_wins módban figyelembe vesz.

    Args:
        raw_hr_queue: Nyers HR adatok asyncio.Queue-ja.
        state: A megosztott vezérlő állapot.
        zone_event: asyncio.Event – beállítja, ha új átlag áll rendelkezésre.
        hr_averager: HRAverager példány.
        printer: ConsolePrinter a konzol kiíráshoz.
        settings: Betöltött beállítások dict-je.
        hr_zones: Kiszámított HR zóna határok.
    """
    hrz = settings.get("heart_rate_zones", {})
    hr_enabled = settings.get("heart_rate_zones", {}).get("enabled", False)
    zone_mode = (
        settings["heart_rate_zones"].get("zone_mode", "power_only")
        if hr_enabled else "power_only"
    )
    valid_min_hr: int = hrz.get("valid_min_hr", 30)
    valid_max_hr: int = hrz.get("valid_max_hr", 220)

    logger.info("HR processor korrutin elindítva")

    while True:
        hr = await raw_hr_queue.get()

        try:
            hr = int(hr)
        except (TypeError, ValueError):
            continue
        if not is_valid_hr(hr, valid_min_hr, valid_max_hr):
            continue

        now = time.monotonic()

        if not hr_enabled:
            printer.print("hr_disabled", f"❤ Szívfrekvencia: {hr} bpm")
            async with state.lock:
                state.last_hr_time = now
            continue

        if zone_mode in ("hr_only", "power_only"):
            printer.print("hr_raw", f"❤ HR: {hr} bpm")

        avg_hr = hr_averager.add_sample(hr)
        if avg_hr is None:
            continue  # Még nincs elég minta

        avg_hr = round(avg_hr)
        new_hr_zone = zone_for_hr(avg_hr, hr_zones)

        if zone_mode == "hr_only":
            printer.print(
                "hr_avg",
                f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone}",
            )
        elif zone_mode == "higher_wins":
            printer.print(
                "hr_avg_hw",
                f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone} | Higher Wins!",
            )

        async with state.lock:
            state.last_hr_time = now
            state.current_hr_zone = new_hr_zone
            state.current_avg_hr = float(avg_hr)

        zone_event.set()  # Zone controller újraszámítást igényel


# ============================================================
# ZÓNA VEZÉRLŐ KORRUTIN
# ============================================================

async def zone_controller_task(
    state: ControllerState,
    zone_queue: asyncio.Queue,
    cooldown_ctrl: CooldownController,
    settings: Dict[str, Any],
    zone_event: asyncio.Event,
) -> None:
    """Zóna vezérlő – kombinálja a power és HR zónákat, alkalmazza a cooldownt.

    Megvárja a zone_event jelzést (amelyet a power és HR processorok állítanak be),
    majd a legfrissebb állapot alapján:
    1. Meghatározza a final zónát (apply_zone_mode / higher_wins)
    2. Alkalmazza a cooldown logikát (CooldownController)
    3. Ha szükséges, elküldi a zóna parancsot a BLE fan queue-ba

    Args:
        state: A megosztott vezérlő állapot.
        zone_queue: BLE fan output asyncio.Queue-ja.
        cooldown_ctrl: CooldownController példány.
        settings: Betöltött beállítások dict-je.
        zone_event: asyncio.Event – jelzi, hogy új adat érkezett.
    """
    hr_enabled = settings.get("heart_rate_zones", {}).get("enabled", False)
    zone_mode = (
        settings["heart_rate_zones"].get("zone_mode", "power_only")
        if hr_enabled else "power_only"
    )
    zero_immediate = settings.get("zero_power_immediate", False)
    dropout_timeout = settings["dropout_timeout"]

    logger.info("Zóna vezérlő korrutin elindítva")

    while True:
        await zone_event.wait()
        zone_event.clear()

        # Állapot pillanatfelvétel (lock alatt)
        async with state.lock:
            power_zone = state.current_power_zone
            hr_zone = state.current_hr_zone
            current_zone = state.current_zone
            now = time.monotonic()
            last_power = state.last_power_time
            last_hr = state.last_hr_time

        # Frissesség ellenőrzése (dropout figyelembe vételéhez)
        power_fresh = (now - last_power) < dropout_timeout
        hr_fresh = last_hr is not None and (now - last_hr) < dropout_timeout

        # Zóna kombinálás a zone_mode alapján
        if zone_mode == "power_only":
            final_zone = power_zone if power_fresh else None
        elif zone_mode == "hr_only":
            final_zone = hr_zone if hr_fresh else None
        else:  # higher_wins
            p = power_zone if power_fresh else None
            h = hr_zone if hr_fresh else None
            final_zone = apply_zone_mode(p, h, zone_mode)

        if final_zone is None:
            continue  # Nincs elég friss adat a döntéshez

        # Cooldown logika alkalmazása
        zone_to_send = cooldown_ctrl.process(current_zone, final_zone, zero_immediate)

        if zone_to_send is not None:
            async with state.lock:
                state.current_zone = zone_to_send
            await send_zone(zone_to_send, zone_queue)
            print(f"→ Zóna elküldve: LEVEL:{zone_to_send}")


# ============================================================
# DROPOUT ELLENŐRZŐ KORRUTIN
# ============================================================

async def dropout_checker_task(
    state: ControllerState,
    zone_queue: asyncio.Queue,
    settings: Dict[str, Any],
    power_averager: PowerAverager,   # ← új
    hr_averager: HRAverager,         # ← új
) -> None:
    """Adatforrás kiesés detektálása, Z0 küldése és pufferek ürítése.

    Dropout esetén:
    - Z0-t küld a ventilátor felé
    - Törli az érintett PowerAverager / HRAverager puffereket
    - Reseteli a kapcsolódó state mezőket (avg_power, avg_hr, power_zone, hr_zone)

    Így dropout után az első új átlag csak friss mintákból épül fel.
    """
    dropout_timeout = settings["dropout_timeout"]
    hr_enabled = settings.get("heart_rate_zones", {}).get("enabled", False)
    zone_mode = (
        settings["heart_rate_zones"].get("zone_mode", "power_only")
        if hr_enabled else "power_only"
    )
    logger.info("Dropout checker korrutin elindítva")

    while True:
        await asyncio.sleep(1)
        now = time.monotonic()
        send_dropout = False

        async with state.lock:
            if state.current_zone is None or state.current_zone == 0:
                continue

            power_fresh = (now - state.last_power_time) < dropout_timeout
            hr_fresh = (
                state.last_hr_time is not None
                and (now - state.last_hr_time) < dropout_timeout
            )

            if zone_mode == "power_only":
                stale = not power_fresh
                elapsed = now - state.last_power_time
                label = "power"
            elif zone_mode == "hr_only":
                stale = not hr_fresh
                elapsed = (
                    (now - state.last_hr_time)
                    if state.last_hr_time is not None
                    else float("inf")
                )
                label = "HR"
            else:  # higher_wins
                stale = not power_fresh and not hr_fresh
                elapsed = min(
                    now - state.last_power_time,
                    (now - state.last_hr_time)
                    if state.last_hr_time is not None
                    else float("inf"),
                )
                label = "power+HR"

            if stale:
                print(f"⚠ Adatforrás kiesett ({label}, {elapsed:.1f}s) → LEVEL:0")

                # Pufferek ürítése – régi minták ne keveredjenek az újba
                if zone_mode in ("power_only", "higher_wins"):
                    power_averager.clear()
                    state.current_avg_power = None
                    state.current_power_zone = None

                if zone_mode in ("hr_only", "higher_wins"):
                    hr_averager.clear()
                    state.current_avg_hr = None
                    state.current_hr_zone = None

                state.current_zone = 0
                send_dropout = True

        if send_dropout:
            await send_zone(0, zone_queue)

# ============================================================
# FAN CONTROLLER – FŐ ÖSSZEHANGOLÁS
# ============================================================

class FanController:
    """A Smart Fan Controller fő orchestrátora.

    Összefogja az összes komponenst, elindítja az asyncio task-okat
    és a szálakat, és gondoskodik a tiszta leállításról.

    Indítási sorrend:
        1. Beállítások betöltése
        2. Zóna határok kiszámítása
        3. Átlagolók, cooldown, printer létrehozása
        4. BLE fan output asyncio task indítása
        5. BLE power/HR input asyncio task-ok indítása (ha szükséges)
        6. Zwift UDP input asyncio task indítása (ha szükséges)
        7. ANT+ szál indítása (ha szükséges)
        8. Power/HR processor asyncio task-ok indítása
        9. Zone controller asyncio task indítása
        10. Dropout checker asyncio task indítása
        11. Főciklus: Ctrl+C / SIGTERM megvárása
        12. Leállítás: minden task és szál leállítása
    """

    def __init__(self, settings_file: str = "settings.json") -> None:
        self.settings = load_settings(settings_file)
        self._antplus_handler: Optional[ANTPlusInputHandler] = None
        self._antplus_thread: Optional[threading.Thread] = None
        self._tasks: list = []
        self._running = True

    def print_startup_info(self) -> None:
        """Kiírja az indítási konfigurációs összefoglalót."""
        s = self.settings
        ds = s["data_source"]
        hrz = s.get("heart_rate_zones", {})
        print("=" * 60)
        print(f"  Smart Fan Controller (Új) v{__version__} – Power/HR → BLE Fan")
        print("=" * 60)
        print(f"FTP: {s['ftp']}W | Érvényes tartomány: 0–{s['max_watt']}W")
        power_zones = calculate_power_zones(
            s["ftp"], s["min_watt"], s["max_watt"],
            s["zone_thresholds"]["z1_max_percent"],
            s["zone_thresholds"]["z2_max_percent"],
        )
        print(f"Zóna határok: {power_zones}")
        print(
            f"Buffer: {s['buffer_seconds']}s | "
            f"Min. minták: {s['minimum_samples']} | "
            f"Dropout: {s['dropout_timeout']}s | "
            f"Cooldown: {s['cooldown_seconds']}s"
        )
        print(f"0W azonnali: {'Igen' if s['zero_power_immediate'] else 'Nem'}")
        print(f"BLE Fan eszköz: {s['ble']['device_name']}")
        if s["ble"].get("pin_code"):
            print(f"BLE PIN: {'*' * len(str(s['ble']['pin_code']))}")
        print(f"📡 Power forrás: {ds['power_source'].upper()} | HR forrás: {ds['hr_source'].upper()}")
        if hrz.get("enabled"):
            hr_zones = calculate_hr_zones(
                hrz["max_hr"], hrz["resting_hr"],
                hrz["z1_max_percent"], hrz["z2_max_percent"],
            )
            print(
                f"HR zóna mód: {hrz.get('zone_mode', 'power_only')} | "
                f"Határok: Z0<{hrz['resting_hr']}bpm, Z1<{hr_zones['z1_max']}bpm, Z2<{hr_zones['z2_max']}bpm"
            )
        print("-" * 60)

    async def run(self) -> None:
        """A vezérlő fő asyncio korrutinja – elindít mindent és vár."""
        s = self.settings
        ds = s["data_source"]
        hr_enabled = s.get("heart_rate_zones", {}).get("enabled", False)
        zone_mode = s["heart_rate_zones"].get("zone_mode", "power_only") if hr_enabled else "power_only"

        # --- Zóna határok kiszámítása ---
        power_zones = calculate_power_zones(
            s["ftp"], s["min_watt"], s["max_watt"],
            s["zone_thresholds"]["z1_max_percent"],
            s["zone_thresholds"]["z2_max_percent"],
        )
        hr_zones = calculate_hr_zones(
            s["heart_rate_zones"]["max_hr"],
            s["heart_rate_zones"]["resting_hr"],
            s["heart_rate_zones"]["z1_max_percent"],
            s["heart_rate_zones"]["z2_max_percent"],
        ) if hr_enabled else {"resting": 60, "z1_max": 130, "z2_max": 148}

        # --- Komponensek létrehozása ---
        raw_power_queue: asyncio.Queue = asyncio.Queue()
        raw_hr_queue: asyncio.Queue = asyncio.Queue()
        zone_cmd_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        zone_event = asyncio.Event()

        state = ControllerState()
        buffer_rate_hz = s.get("buffer_rate_hz", 4)  # ← ÚJ
        power_averager = PowerAverager(s["buffer_seconds"], s["minimum_samples"], buffer_rate_hz)  # ← MÓDOSÍTOTT
        hr_averager = HRAverager(s["buffer_seconds"], s["minimum_samples"], buffer_rate_hz)        # ← MÓDOSÍTOTT
        cooldown_ctrl = CooldownController(s["cooldown_seconds"])
        printer = ConsolePrinter()

        loop = asyncio.get_event_loop()

        # --- BLE Fan Output ---
        ble_fan = BLEFanOutputController(s)
        self._tasks.append(asyncio.create_task(
            ble_fan.run(zone_cmd_queue), name="BLEFanOutput"
        ))

        # --- Bemeneti adatforrások ---
        power_source = ds.get("power_source", "antplus")
        hr_source = ds.get("hr_source", "antplus")

        if power_source == "ble":
            ble_power = BLEPowerInputHandler(s, raw_power_queue)
            self._tasks.append(asyncio.create_task(
                ble_power.run(), name="BLEPowerInput"
            ))

        if hr_source == "ble" and hr_enabled:
            ble_hr = BLEHRInputHandler(s, raw_hr_queue)
            self._tasks.append(asyncio.create_task(
                ble_hr.run(), name="BLEHRInput"
            ))

        needs_zwift = (power_source == "zwift_udp") or (hr_source == "zwift_udp" and hr_enabled)
        if needs_zwift:
            zwift_udp = ZwiftUDPInputHandler(s, raw_power_queue, raw_hr_queue)
            self._tasks.append(asyncio.create_task(
                zwift_udp.run(), name="ZwiftUDPInput"
            ))

        needs_antplus = (power_source == "antplus") or (hr_source == "antplus" and hr_enabled)
        if needs_antplus:
            if _ANTPLUS_AVAILABLE:
                self._antplus_handler = ANTPlusInputHandler(
                    s, raw_power_queue, raw_hr_queue, loop
                )
                self._antplus_thread = self._antplus_handler.start()
            else:
                logger.warning("ANT+ forrás kérve, de az openant könyvtár nem elérhető!")

        # --- Feldolgozó és vezérlő korrutinok ---
        self._tasks.append(asyncio.create_task(
            power_processor_task(
                raw_power_queue, state, zone_event,
                power_averager, printer, s, power_zones,
            ), name="PowerProcessor"
        ))
        self._tasks.append(asyncio.create_task(
            hr_processor_task(
                raw_hr_queue, state, zone_event,
                hr_averager, printer, s, hr_zones,
            ), name="HRProcessor"
        ))
        self._tasks.append(asyncio.create_task(
            zone_controller_task(
                state, zone_cmd_queue, cooldown_ctrl, s, zone_event,
            ), name="ZoneController"
        ))
        self._tasks.append(asyncio.create_task(
            dropout_checker_task(state, zone_cmd_queue, s, power_averager, hr_averager),
            name="DropoutChecker"
        ))

        print()
        print("🚴 Figyelés elindítva... (Ctrl+C a leállításhoz)")
        print()

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await ble_fan.disconnect()
            # ANT leállítása is itt, ha még fut:
            if self._antplus_handler:
                self._antplus_handler.stop()
            if self._antplus_thread and self._antplus_thread.is_alive():
                self._antplus_thread.join(timeout=5.0)


    def stop(self) -> None:
        """Leállítja az összes asyncio task-ot és az ANT+ szálat."""
        for task in self._tasks:
            task.cancel()
        if self._antplus_handler:
            self._antplus_handler.stop()
        if self._antplus_thread and self._antplus_thread.is_alive():
            self._antplus_thread.join(timeout=5.0)   # ← ÚJ: max 5s-t vár
            if self._antplus_thread.is_alive():
                logger.warning("ANT+ szál nem állt le 5s alatt!")



# ============================================================
# MAIN
# ============================================================

def main() -> None:
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    logging.getLogger("openant").setLevel(logging.CRITICAL)

    controller = FanController("settings.json")
    controller.print_startup_info()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cleaned_up = False

    def cleanup() -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        controller.stop()
        try:
            if not loop.is_closed() and not loop.is_running():
                pending = asyncio.all_tasks(loop)
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
        except Exception:
            pass
        print("\nProgram leállítva.")

    def signal_handler(signum: int, frame: Any) -> None:   # ← 1. ELŐSZÖR definiálva
        print(f"\nSignal {signum} fogadva, leállítás...")
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, signal_handler)           # ← 2. UTÁNA használva

    atexit.register(cleanup)

    try:
        loop.run_until_complete(controller.run())
    except KeyboardInterrupt:
        print("\nLeállítás (Ctrl+C)...")
    finally:
        cleanup()
        loop.close()



if __name__ == "__main__":
    main()
