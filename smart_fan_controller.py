import sys
import os
import logging
import json
import time
import asyncio
import threading
import queue
import socket
import copy
from collections import deque
from openant.easy.node import Node
from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.power_meter import PowerMeter, PowerData
from openant.devices.heart_rate import HeartRate, HeartRateData
from bleak import BleakClient, BleakScanner

# psutil opcionális import
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("⚠ psutil nem elérhető, Zwift folyamat figyelés kikapcsolva")

# bless BLE szerver - opcionális
try:
    from bless import (
        BlessServer,
        GATTCharacteristicProperties,
        GATTAttributePermissions,
    )
    BLESS_AVAILABLE = True
except ImportError:
    BLESS_AVAILABLE = False

# Zwift protobuf - csak ha elérhető
try:
    from zwift_pb2 import PlayerState
    PROTOBUF_AVAILABLE = True
except ImportError:
    PROTOBUF_AVAILABLE = False

# ============================================================
# Alapértelmezett beállítások
# ============================================================
DEFAULT_SETTINGS = {
    "ftp": 180,
    "min_watt": 0,
    "max_watt": 1000,
    "cooldown_seconds": 120,
    "buffer_seconds": 3,
    "minimum_samples": 8,
    "dropout_timeout": 5,
    "zero_power_immediate": False,
    "zone_thresholds": {
        "z1_max_percent": 60,
        "z2_max_percent": 89
    },
    "ble": {
        "skip_connection": False,
        "device_name": "FanController",
        "scan_timeout": 10,
        "connection_timeout": 15,
        "reconnect_interval": 5,
        "max_retries": 10,
        "command_timeout": 3,
        "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
        "characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
        "pin_code": None
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
    },
    "antplus_bridge": {
        "enabled": False,
        "heart_rate": {
            "enabled": True,
            "device_id": 0
        },
        "ble_broadcast": {
            "enabled": True,
            "power_service": True,
            "heart_rate_service": True,
            "device_name": "SmartFanBridge"
        }
    },
    "heart_rate_zones": {
        "enabled": False,
        "max_hr": 185,
        "resting_hr": 60,
        "zone_mode": "power_only",
        "z1_max_percent": 70,
        "z2_max_percent": 80
    }
}


# ============================================================
# BLEController
# ============================================================
class BLEController:
    def __init__(self, settings):
        self.skip_connection = settings['ble'].get('skip_connection', False)
        
        self.device_name = settings['ble']['device_name']
        self.scan_timeout = settings['ble']['scan_timeout']
        self.connection_timeout = settings['ble']['connection_timeout']
        self.reconnect_interval = settings['ble']['reconnect_interval']
        self.max_retries = settings['ble']['max_retries']
        self.command_timeout = settings['ble']['command_timeout']
        self.service_uuid = settings['ble']['service_uuid']
        self.characteristic_uuid = settings['ble']['characteristic_uuid']
        self.pin_code = settings['ble'].get('pin_code', None)

        self.client = None
        self.device_address = None
        self.is_connected = False
        self.retry_count = 0
        self.retry_reset_time = None
        self.last_sent_command = None

        self.command_queue = queue.Queue(maxsize=1)
        self.running = False
        self.thread = None
        self.loop = None
        self.ready_event = threading.Event()

    def start(self):
        if self.running:
            print("⚠ BLE thread már fut!")
            return
        
        if self.skip_connection:
            print("⚠ BLE TEST MODE - parancsok csak logolva (skip_connection=true)")
        
        self.running = True
        self.thread = threading.Thread(target=self._ble_loop, daemon=True, name="BLE-Thread")
        self.thread.start()
        print("✓ BLE thread elindítva")

    def _ble_loop(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            
            if not self.skip_connection:
                print("🔍 BLE inicializálás...")
                self.loop.run_until_complete(self._initial_connect())
            else:
                print("🔍 BLE inicializálás kihagyva (TEST MODE)")
            
            self.ready_event.set()

            while self.running:
                try:
                    try:
                        level = self.command_queue.get(timeout=0.5)
                        
                        if self.skip_connection:
                            self._log_command(level)
                        else:
                            self.loop.run_until_complete(self._send_command_async(level))
                    except queue.Empty:
                        continue
                except Exception as e:
                    print(f"✗ BLE loop hiba: {e}")
                    time.sleep(1)

            print("🔌 BLE kapcsolat lezárása...")
            if not self.skip_connection:
                self.loop.run_until_complete(self._disconnect_async())

        except Exception as e:
            print(f"✗ BLE thread kritikus hiba: {e}")
        finally:
            self.ready_event.set()
            if self.loop:
                self.loop.close()
            print("✓ BLE thread leállt")

    def _log_command(self, level):
        """TEST MODE: csak kiírja a parancsot, nem küldi el"""
        if self.last_sent_command != level:
            message = f"LEVEL:{level}"
            print(f"🧪 TEST MODE - Parancs: {message}")
            self.last_sent_command = level

    async def _initial_connect(self):
        success = await self._scan_and_connect_async()
        if not success:
            print(f"⚠ Nem sikerült csatlakozni a BLE eszközhöz, de folytatjuk...")
            print(f"  A program automatikusan újrapróbálkozik parancs küldéskor.")

    async def _scan_and_connect_async(self):
        print(f"🔍 BLE eszköz keresése: {self.device_name}...")
        try:
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for device in devices:
                if device.name == self.device_name:
                    print(f"✓ Eszköz megtalálva: {device.name} ({device.address})")
                    self.device_address = device.address
                    return await self._connect_async()
            print(f"✗ Nem található: {self.device_name}")
            return False
        except Exception as e:
            print(f"✗ Keresési hiba: {e}")
            return False

    async def _connect_async(self):
        if not self.device_address:
            return False
        try:
            if self.client and await self._is_connected_async():
                return True
            self.client = BleakClient(self.device_address, timeout=self.connection_timeout)
            await self.client.connect()
            if self.pin_code is not None:
                print(f"🔗 BLE párosítás folyamatban: {self.device_address}")
                try:
                    await self.client.pair()
                    print(f"✓ BLE párosítás sikeres: {self.device_address}")
                except Exception as pair_err:
                    print(f"⚠ BLE párosítás hiba (folytatás): {pair_err}")
            self.is_connected = True
            self.retry_count = 0
            self.retry_reset_time = None
            print(f"✓ Csatlakozva: {self.device_address}")
            return True
        except Exception as e:
            print(f"✗ Csatlakozási hiba: {e}")
            self.is_connected = False
            self.client = None
            return False

    async def _is_connected_async(self):
        try:
            if self.client:
                return self.client.is_connected
        except Exception:
            pass
        return False

    async def _disconnect_async(self):
        if self.client:
            try:
                await self.client.disconnect()
                print("✓ BLE kapcsolat lezárva")
            except Exception:
                pass
            finally:
                self.is_connected = False
                self.client = None

    async def _send_command_async(self, level):
        if self.last_sent_command == level and await self._is_connected_async():
            return True

        if not await self._is_connected_async():
            if self.retry_reset_time is not None:
                elapsed = time.time() - self.retry_reset_time
                if elapsed >= 30:
                    print(f"🔄 Retry count reset ({elapsed:.0f}s telt el), újrapróbálkozás...")
                    self.retry_count = 0
                    self.retry_reset_time = None
                else:
                    remaining = 30 - elapsed
                    print(f"⏳ Újrapróbálkozás {remaining:.0f}s múlva...")
                    await asyncio.sleep(min(remaining, self.reconnect_interval))
                    return False

            if self.retry_count < self.max_retries:
                self.retry_count += 1
                print(f"🔄 Újracsatlakozás... ({self.retry_count}/{self.max_retries})")
                if self.device_address:
                    if await self._connect_async():
                        return await self._send_immediate(level)
                else:
                    if await self._scan_and_connect_async():
                        return await self._send_immediate(level)
                await asyncio.sleep(self.reconnect_interval)
                return False
            else:
                if self.retry_reset_time is None:
                    self.retry_reset_time = time.time()
                    print(f"⚠ Max újracsatlakozási kísérletek elérve ({self.max_retries})!")
                    print(f"  30s múlva újrapróbálkozik...")
                return False

        return await self._send_immediate(level)

    async def _send_immediate(self, level):
        if not await self._is_connected_async():
            self.is_connected = False
            return False
        try:
            message = f"LEVEL:{level}"
            await asyncio.wait_for(
                self.client.write_gatt_char(
                    self.characteristic_uuid,
                    message.encode('utf-8')
                ),
                timeout=self.command_timeout
            )
            self.last_sent_command = level
            print(f"✓ Parancs elküldve: {message}")
            return True
        except asyncio.TimeoutError:
            print(f"✗ Parancs küldés timeout ({self.command_timeout}s)")
            self.is_connected = False
            return False
        except Exception as e:
            print(f"✗ Küldési hiba: {e}")
            self.is_connected = False
            return False

    def send_command_sync(self, level):
        if isinstance(level, bool) or not isinstance(level, int) or level < 0 or level > 3:
            print(f"⚠ Érvénytelen parancs szint: {level} (egész számnak kell lennie, 0-3 között)")
            return
        if not self.running:
            print("⚠ BLE thread nem fut, parancs elvetve")
            return
        try:
            self.command_queue.put_nowait(level)
        except queue.Full:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.command_queue.put_nowait(level)
            except queue.Full:
                print(f"⚠ Queue hiba, parancs elvetve: LEVEL:{level}")

    def stop(self):
        if not self.running:
            return
        print("🛑 BLE thread leállítása...")
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                print("⚠ BLE thread nem állt le időben")
            else:
                print("✓ BLE thread leállítva")


# ============================================================
# PowerZoneController
# ============================================================
class PowerZoneController:
    def __init__(self, settings_file="settings.json"):
        self.settings = self.load_and_validate_settings(settings_file)

        self.ftp = self.settings['ftp']
        self.min_watt = self.settings['min_watt']
        self.max_watt = self.settings['max_watt']
        self.cooldown_seconds = self.settings['cooldown_seconds']
        self.buffer_seconds = self.settings['buffer_seconds']
        self.minimum_samples = self.settings['minimum_samples']
        self.dropout_timeout = self.settings['dropout_timeout']
        self.zero_power_immediate = self.settings['zero_power_immediate']
        self.zone_thresholds = self.settings['zone_thresholds']
        self.hr_zone_settings = self.settings.get('heart_rate_zones', copy.deepcopy(DEFAULT_SETTINGS['heart_rate_zones']))

        self.zones = self.calculate_zones()

        self.current_zone = None
        self.last_zone_change = time.time()
        self.cooldown_active = False
        self.cooldown_start_time = 0
        self.pending_zone = None

        self.last_data_time = time.time()

        buffer_size = int(self.buffer_seconds * 4)
        self.power_buffer = deque(maxlen=buffer_size)

        self.state_lock = threading.Lock()
        self.last_cooldown_print = 0

        self.current_heart_rate = None
        self.current_hr_zone = None
        self.current_power_zone = None
        hr_buffer_size = int(self.buffer_seconds * 4)
        self.hr_buffer = deque(maxlen=hr_buffer_size)

        self.ble = BLEController(self.settings)

        self.running = False
        self.dropout_thread = None

        print(f"FTP: {self.ftp}W")
        print(f"Érvényes watt tartomány: 0W - {self.max_watt}W")
        print(f"Zóna határok: {self.zones}")
        print(f"Buffer méret: {buffer_size} adat ({self.buffer_seconds}s)")
        print(f"Minimum minták: {self.minimum_samples}")
        print(f"Dropout timeout: {self.dropout_timeout}s")
        print(f"Cooldown: {self.cooldown_seconds}s")
        print(f"0W azonnali: {'Igen' if self.zero_power_immediate else 'Nem'}")
        print(f"BLE eszköz: {self.settings['ble']['device_name']}")
        if self.settings['ble'].get('skip_connection', False):
            print(f"BLE mód: TEST MODE (skip_connection=true)")
        pin_code = self.settings['ble'].get('pin_code', None)
        if pin_code is not None:
            print(f"BLE PIN: {pin_code}")
        hr_source = self.settings['data_source'].get('heart_rate_source', 'antplus')
        print(f"HR forrás: {hr_source}")
        if self.hr_zone_settings.get('enabled', False):
            hr_z = self.hr_zones
            print(f"HR zóna mód: {self.hr_zone_settings.get('zone_mode', 'power_only')}")
            print(f"HR zóna határok: Z0 < {self.hr_zone_settings['resting_hr']} bpm, Z1 < {hr_z['z1_max']} bpm, Z2 < {hr_z['z2_max']} bpm")

    def start_dropout_checker(self):
        self.running = True
        self.dropout_thread = threading.Thread(
            target=self._dropout_check_loop,
            daemon=True,
            name="Dropout-Thread"
        )
        self.dropout_thread.start()
        print("✓ Dropout ellenőrző thread elindítva")

    def _dropout_check_loop(self):
        while self.running:
            self.check_dropout()
            time.sleep(1)

    def stop_dropout_checker(self):
        self.running = False
        if self.dropout_thread and self.dropout_thread.is_alive():
            self.dropout_thread.join(timeout=3)
            print("✓ Dropout ellenőrző thread leállítva")

    def load_and_validate_settings(self, settings_file):
        settings = copy.deepcopy(DEFAULT_SETTINGS)

        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
        except FileNotFoundError:
            print(f"⚠ FIGYELMEZTETÉS: '{settings_file}' nem található! Alapértelmezett beállítások használata.")
            self.save_default_settings(settings_file)
            return settings
        except json.JSONDecodeError as e:
            print(f"⚠ FIGYELMEZTETÉS: '{settings_file}' hibás JSON formátum! ({e})")
            return settings
        except Exception as e:
            print(f"⚠ FIGYELMEZTETÉS: Hiba a beállítások betöltésekor! ({e})")
            return settings

        validation_failed = False

        if 'ftp' in loaded_settings:
            if isinstance(loaded_settings['ftp'], (int, float)) and 100 <= loaded_settings['ftp'] <= 500:
                settings['ftp'] = int(loaded_settings['ftp'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'ftp' érték: {loaded_settings['ftp']} (100-500 között kell lennie)")
                validation_failed = True

        if 'min_watt' in loaded_settings:
            if isinstance(loaded_settings['min_watt'], (int, float)) and loaded_settings['min_watt'] >= 0:
                settings['min_watt'] = int(loaded_settings['min_watt'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'min_watt' érték: {loaded_settings['min_watt']} (0 vagy nagyobb kell legyen)")
                validation_failed = True

        if 'max_watt' in loaded_settings:
            if isinstance(loaded_settings['max_watt'], (int, float)) and loaded_settings['max_watt'] > 0:
                settings['max_watt'] = int(loaded_settings['max_watt'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'max_watt' érték: {loaded_settings['max_watt']} (0-nál nagyobb kell legyen)")
                validation_failed = True

        if 'cooldown_seconds' in loaded_settings:
            if isinstance(loaded_settings['cooldown_seconds'], (int, float)) and 0 <= loaded_settings['cooldown_seconds'] <= 300:
                settings['cooldown_seconds'] = int(loaded_settings['cooldown_seconds'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'cooldown_seconds' érték: {loaded_settings['cooldown_seconds']} (0-300 között kell lennie)")
                validation_failed = True

        if 'buffer_seconds' in loaded_settings:
            if isinstance(loaded_settings['buffer_seconds'], (int, float)) and 1 <= loaded_settings['buffer_seconds'] <= 10:
                settings['buffer_seconds'] = int(loaded_settings['buffer_seconds'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'buffer_seconds' érték: {loaded_settings['buffer_seconds']} (1-10 között kell lennie)")
                validation_failed = True

        if 'minimum_samples' in loaded_settings:
            if isinstance(loaded_settings['minimum_samples'], (int, float)) and loaded_settings['minimum_samples'] > 0:
                settings['minimum_samples'] = int(loaded_settings['minimum_samples'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'minimum_samples' érték: {loaded_settings['minimum_samples']} (0-nál nagyobb kell legyen)")
                validation_failed = True

        if 'dropout_timeout' in loaded_settings:
            if isinstance(loaded_settings['dropout_timeout'], (int, float)) and loaded_settings['dropout_timeout'] > 0:
                settings['dropout_timeout'] = int(loaded_settings['dropout_timeout'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'dropout_timeout' érték: {loaded_settings['dropout_timeout']} (0-nál nagyobb kell legyen)")
                validation_failed = True

        if 'zero_power_immediate' in loaded_settings:
            if isinstance(loaded_settings['zero_power_immediate'], bool):
                settings['zero_power_immediate'] = loaded_settings['zero_power_immediate']
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zero_power_immediate' érték: {loaded_settings['zero_power_immediate']} (true vagy false kell legyen)")
                validation_failed = True

        if 'zone_thresholds' in loaded_settings:
            if isinstance(loaded_settings['zone_thresholds'], dict):
                z_thresholds = loaded_settings['zone_thresholds']
                if 'z1_max_percent' in z_thresholds:
                    if isinstance(z_thresholds['z1_max_percent'], (int, float)) and 1 <= z_thresholds['z1_max_percent'] <= 100:
                        settings['zone_thresholds']['z1_max_percent'] = int(z_thresholds['z1_max_percent'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'z1_max_percent' érték: {z_thresholds['z1_max_percent']} (1-100 között kell lennie)")
                        validation_failed = True
                if 'z2_max_percent' in z_thresholds:
                    if isinstance(z_thresholds['z2_max_percent'], (int, float)) and 1 <= z_thresholds['z2_max_percent'] <= 100:
                        settings['zone_thresholds']['z2_max_percent'] = int(z_thresholds['z2_max_percent'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'z2_max_percent' érték: {z_thresholds['z2_max_percent']} (1-100 között kell lennie)")
                        validation_failed = True
                if settings['zone_thresholds']['z1_max_percent'] >= settings['zone_thresholds']['z2_max_percent']:
                    print(f"⚠ FIGYELMEZTETÉS: z1_max_percent >= z2_max_percent! Alapértelmezett zóna határok használata.")
                    settings['zone_thresholds'] = copy.deepcopy(DEFAULT_SETTINGS['zone_thresholds'])
                    validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zone_thresholds' formátum")
                validation_failed = True

        if 'ble' in loaded_settings:
            if isinstance(loaded_settings['ble'], dict):
                ble_settings = loaded_settings['ble']
                
                if 'skip_connection' in ble_settings:
                    if isinstance(ble_settings['skip_connection'], bool):
                        settings['ble']['skip_connection'] = ble_settings['skip_connection']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'skip_connection' érték: {ble_settings['skip_connection']} (true vagy false kell legyen)")
                        validation_failed = True
                
                if 'device_name' in ble_settings:
                    if isinstance(ble_settings['device_name'], str) and len(ble_settings['device_name']) > 0:
                        settings['ble']['device_name'] = ble_settings['device_name']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'device_name' érték")
                        validation_failed = True
                if 'scan_timeout' in ble_settings:
                    if isinstance(ble_settings['scan_timeout'], (int, float)) and 1 <= ble_settings['scan_timeout'] <= 60:
                        settings['ble']['scan_timeout'] = int(ble_settings['scan_timeout'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'scan_timeout' érték: {ble_settings['scan_timeout']} (1-60 között kell lennie)")
                        validation_failed = True
                if 'connection_timeout' in ble_settings:
                    if isinstance(ble_settings['connection_timeout'], (int, float)) and 1 <= ble_settings['connection_timeout'] <= 60:
                        settings['ble']['connection_timeout'] = int(ble_settings['connection_timeout'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'connection_timeout' érték: {ble_settings['connection_timeout']} (1-60 között kell lennie)")
                        validation_failed = True
                if 'reconnect_interval' in ble_settings:
                    if isinstance(ble_settings['reconnect_interval'], (int, float)) and 1 <= ble_settings['reconnect_interval'] <= 60:
                        settings['ble']['reconnect_interval'] = int(ble_settings['reconnect_interval'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'reconnect_interval' érték: {ble_settings['reconnect_interval']} (1-60 között kell lennie)")
                        validation_failed = True
                if 'max_retries' in ble_settings:
                    if isinstance(ble_settings['max_retries'], (int, float)) and 1 <= ble_settings['max_retries'] <= 100:
                        settings['ble']['max_retries'] = int(ble_settings['max_retries'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'max_retries' érték: {ble_settings['max_retries']} (1-100 között kell lennie)")
                        validation_failed = True
                if 'command_timeout' in ble_settings:
                    if isinstance(ble_settings['command_timeout'], (int, float)) and 1 <= ble_settings['command_timeout'] <= 30:
                        settings['ble']['command_timeout'] = int(ble_settings['command_timeout'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'command_timeout' érték: {ble_settings['command_timeout']} (1-30 között kell lennie)")
                        validation_failed = True
                if 'service_uuid' in ble_settings:
                    if isinstance(ble_settings['service_uuid'], str) and len(ble_settings['service_uuid']) > 0:
                        settings['ble']['service_uuid'] = ble_settings['service_uuid']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'service_uuid' érték")
                        validation_failed = True
                if 'characteristic_uuid' in ble_settings:
                    if isinstance(ble_settings['characteristic_uuid'], str) and len(ble_settings['characteristic_uuid']) > 0:
                        settings['ble']['characteristic_uuid'] = ble_settings['characteristic_uuid']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'characteristic_uuid' érték")
                        validation_failed = True
                if 'pin_code' in ble_settings:
                    if ble_settings['pin_code'] is None:
                        settings['ble']['pin_code'] = None
                    elif isinstance(ble_settings['pin_code'], int) and not isinstance(ble_settings['pin_code'], bool) and 0 <= ble_settings['pin_code'] <= 999999:
                        settings['ble']['pin_code'] = ble_settings['pin_code']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'pin_code' érték: {ble_settings['pin_code']} (0-999999 közötti egész szám vagy null kell legyen)")
                        validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'ble' formátum")
                validation_failed = True

        if 'data_source' in loaded_settings:
            if isinstance(loaded_settings['data_source'], dict):
                ds = loaded_settings['data_source']

                if 'primary' in ds:
                    if ds['primary'] in ('antplus', 'zwift'):
                        settings['data_source']['primary'] = ds['primary']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'primary' érték: {ds['primary']} ('antplus' vagy 'zwift' kell legyen)")
                        validation_failed = True

                if 'fallback' in ds:
                    if ds['fallback'] in ('zwift', 'none'):
                        settings['data_source']['fallback'] = ds['fallback']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'fallback' érték: {ds['fallback']} ('zwift' vagy 'none' kell legyen)")
                        validation_failed = True

                if settings['data_source']['primary'] == settings['data_source']['fallback']:
                    print(f"⚠ FIGYELMEZTETÉS: 'primary' és 'fallback' azonos ('{settings['data_source']['primary']}')! Fallback 'none'-ra állítva.")
                    settings['data_source']['fallback'] = 'none'
                    validation_failed = True

                if 'heart_rate_source' in ds:
                    if ds['heart_rate_source'] in ('antplus', 'zwift', 'both'):
                        settings['data_source']['heart_rate_source'] = ds['heart_rate_source']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_source' érték: {ds['heart_rate_source']} ('antplus', 'zwift' vagy 'both' kell legyen)")
                        validation_failed = True

                if 'zwift' in ds:
                    if isinstance(ds['zwift'], dict):
                        z = ds['zwift']
                        if 'port' in z:
                            if isinstance(z['port'], int) and 1 <= z['port'] <= 65535:
                                settings['data_source']['zwift']['port'] = z['port']
                            else:
                                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'port' érték: {z['port']} (1-65535 között kell lennie)")
                                validation_failed = True
                        if 'host' in z:
                            if isinstance(z['host'], str) and len(z['host']) > 0:
                                settings['data_source']['zwift']['host'] = z['host']
                            else:
                                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'host' érték")
                                validation_failed = True
                        if 'process_name' in z:
                            if isinstance(z['process_name'], str) and len(z['process_name']) > 0:
                                settings['data_source']['zwift']['process_name'] = z['process_name']
                            else:
                                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'process_name' érték")
                                validation_failed = True
                        if 'check_interval' in z:
                            if isinstance(z['check_interval'], (int, float)) and 1 <= z['check_interval'] <= 60:
                                settings['data_source']['zwift']['check_interval'] = int(z['check_interval'])
                            else:
                                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'check_interval' érték: {z['check_interval']} (1-60 között kell lennie)")
                                validation_failed = True

                        known_zwift_keys = {'port', 'host', 'process_name', 'check_interval'}
                        unknown_zwift = set(z.keys()) - known_zwift_keys
                        if unknown_zwift:
                            print(f"⚠ FIGYELMEZTETÉS: Ismeretlen zwift mező(k): {', '.join(unknown_zwift)}")
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zwift' formátum")
                        validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'data_source' formátum")
                validation_failed = True

        if 'antplus_bridge' in loaded_settings:
            if isinstance(loaded_settings['antplus_bridge'], dict):
                ab = loaded_settings['antplus_bridge']
                if 'enabled' in ab:
                    if isinstance(ab['enabled'], bool):
                        settings['antplus_bridge']['enabled'] = ab['enabled']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'antplus_bridge.enabled' érték (true vagy false kell legyen)")
                        validation_failed = True
                if 'heart_rate' in ab:
                    if isinstance(ab['heart_rate'], dict):
                        hr = ab['heart_rate']
                        if 'enabled' in hr:
                            if isinstance(hr['enabled'], bool):
                                settings['antplus_bridge']['heart_rate']['enabled'] = hr['enabled']
                            else:
                                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate.enabled' érték (true vagy false kell legyen)")
                                validation_failed = True
                        if 'device_id' in hr:
                            if isinstance(hr['device_id'], int) and 0 <= hr['device_id'] <= 65535:
                                settings['antplus_bridge']['heart_rate']['device_id'] = hr['device_id']
                            else:
                                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate.device_id' érték (0-65535 kell legyen)")
                                validation_failed = True
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'antplus_bridge.heart_rate' formátum")
                        validation_failed = True
                if 'ble_broadcast' in ab:
                    if isinstance(ab['ble_broadcast'], dict):
                        bb = ab['ble_broadcast']
                        for flag in ('enabled', 'power_service', 'heart_rate_service'):
                            if flag in bb:
                                if isinstance(bb[flag], bool):
                                    settings['antplus_bridge']['ble_broadcast'][flag] = bb[flag]
                                else:
                                    print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'ble_broadcast.{flag}' érték (true vagy false kell legyen)")
                                    validation_failed = True
                        if 'device_name' in bb:
                            if isinstance(bb['device_name'], str) and len(bb['device_name']) > 0:
                                settings['antplus_bridge']['ble_broadcast']['device_name'] = bb['device_name']
                            else:
                                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'ble_broadcast.device_name' érték")
                                validation_failed = True
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'antplus_bridge.ble_broadcast' formátum")
                        validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'antplus_bridge' formátum")
                validation_failed = True

        if 'heart_rate_zones' in loaded_settings:
            if isinstance(loaded_settings['heart_rate_zones'], dict):
                hrz = loaded_settings['heart_rate_zones']
                if 'enabled' in hrz:
                    if isinstance(hrz['enabled'], bool):
                        settings['heart_rate_zones']['enabled'] = hrz['enabled']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_zones.enabled' érték (true vagy false kell legyen)")
                        validation_failed = True
                if 'max_hr' in hrz:
                    if isinstance(hrz['max_hr'], int) and not isinstance(hrz['max_hr'], bool) and 100 <= hrz['max_hr'] <= 220:
                        settings['heart_rate_zones']['max_hr'] = hrz['max_hr']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'max_hr' érték: {hrz['max_hr']} (100-220 közötti egész szám kell legyen)")
                        validation_failed = True
                if 'resting_hr' in hrz:
                    if isinstance(hrz['resting_hr'], int) and not isinstance(hrz['resting_hr'], bool) and 30 <= hrz['resting_hr'] <= 100:
                        settings['heart_rate_zones']['resting_hr'] = hrz['resting_hr']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'resting_hr' érték: {hrz['resting_hr']} (30-100 közötti egész szám kell legyen)")
                        validation_failed = True
                if 'zone_mode' in hrz:
                    if hrz['zone_mode'] in ('hr_only', 'higher_wins', 'power_only'):
                        settings['heart_rate_zones']['zone_mode'] = hrz['zone_mode']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zone_mode' érték: {hrz['zone_mode']} ('hr_only', 'higher_wins' vagy 'power_only' kell legyen)")
                        validation_failed = True
                if 'z1_max_percent' in hrz:
                    if isinstance(hrz['z1_max_percent'], int) and not isinstance(hrz['z1_max_percent'], bool) and 1 <= hrz['z1_max_percent'] <= 100:
                        settings['heart_rate_zones']['z1_max_percent'] = hrz['z1_max_percent']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_zones.z1_max_percent' érték: {hrz['z1_max_percent']} (1-100 között kell lennie)")
                        validation_failed = True
                if 'z2_max_percent' in hrz:
                    if isinstance(hrz['z2_max_percent'], int) and not isinstance(hrz['z2_max_percent'], bool) and 1 <= hrz['z2_max_percent'] <= 100:
                        settings['heart_rate_zones']['z2_max_percent'] = hrz['z2_max_percent']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_zones.z2_max_percent' érték: {hrz['z2_max_percent']} (1-100 között kell lennie)")
                        validation_failed = True
                if settings['heart_rate_zones']['z1_max_percent'] >= settings['heart_rate_zones']['z2_max_percent']:
                    print(f"⚠ FIGYELMEZTETÉS: HR z1_max_percent >= z2_max_percent! Alapértelmezett HR zóna határok használata.")
                    settings['heart_rate_zones']['z1_max_percent'] = DEFAULT_SETTINGS['heart_rate_zones']['z1_max_percent']
                    settings['heart_rate_zones']['z2_max_percent'] = DEFAULT_SETTINGS['heart_rate_zones']['z2_max_percent']
                    validation_failed = True
                max_hr = settings['heart_rate_zones']['max_hr']
                resting_hr = settings['heart_rate_zones']['resting_hr']
                z1_max = max_hr * settings['heart_rate_zones']['z1_max_percent'] / 100
                if resting_hr >= z1_max:
                    print(f"⚠ FIGYELMEZTETÉS: 'resting_hr' ({resting_hr}) >= z1_max ({z1_max:.0f})! Alapértelmezett HR zóna határok használata.")
                    settings['heart_rate_zones']['resting_hr'] = DEFAULT_SETTINGS['heart_rate_zones']['resting_hr']
                    settings['heart_rate_zones']['z1_max_percent'] = DEFAULT_SETTINGS['heart_rate_zones']['z1_max_percent']
                    settings['heart_rate_zones']['z2_max_percent'] = DEFAULT_SETTINGS['heart_rate_zones']['z2_max_percent']
                    validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_zones' formátum")
                validation_failed = True

        if settings['min_watt'] >= settings['max_watt']:
            print(f"⚠ FIGYELMEZTETÉS: 'min_watt' >= 'max_watt'! Alapértelmezett értékek használata.")
            settings['min_watt'] = DEFAULT_SETTINGS['min_watt']
            settings['max_watt'] = DEFAULT_SETTINGS['max_watt']
            validation_failed = True

        buffer_size = settings['buffer_seconds'] * 4
        if settings['minimum_samples'] > buffer_size:
            print(f"⚠ FIGYELMEZTETÉS: 'minimum_samples' ({settings['minimum_samples']}) nagyobb mint buffer méret ({buffer_size})!")
            settings['minimum_samples'] = buffer_size
            validation_failed = True

        known_keys = {'ftp', 'min_watt', 'max_watt', 'cooldown_seconds', 'buffer_seconds',
                      'minimum_samples', 'dropout_timeout', 'zero_power_immediate',
                      'zone_thresholds', 'ble', 'data_source', 'antplus_bridge',
                      'heart_rate_zones'}
        unknown_keys = set(loaded_settings.keys()) - known_keys
        if unknown_keys:
            print(f"⚠ FIGYELMEZTETÉS: Ismeretlen mező(k): {', '.join(unknown_keys)}")

        if validation_failed:
            print("\n⚠ HIBÁS BEÁLLÍTÁSOK! Érvényes értékek használata.")

        return settings

    def save_default_settings(self, settings_file):
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_SETTINGS, f, indent=2, ensure_ascii=False)
            print(f"✓ Alapértelmezett '{settings_file}' létrehozva.")
        except Exception as e:
            print(f"✗ Nem sikerült létrehozni a '{settings_file}' fájlt: {e}")

    def calculate_zones(self):
        z1_max = int(self.ftp * self.zone_thresholds['z1_max_percent'] / 100)
        z2_max = int(self.ftp * self.zone_thresholds['z2_max_percent'] / 100)

        z2_max = min(z2_max, self.max_watt)
        z1_max = min(z1_max, z2_max - 1)

        z1_max_orig = int(self.ftp * self.zone_thresholds['z1_max_percent'] / 100)
        z2_max_orig = int(self.ftp * self.zone_thresholds['z2_max_percent'] / 100)
        if z2_max_orig > self.max_watt:
            print(f"⚠ FIGYELMEZTETÉS: z2_max ({z2_max_orig}W) > max_watt ({self.max_watt}W), határolva!")
        if z1_max_orig > z2_max - 1:
            print(f"⚠ FIGYELMEZTETÉS: z1_max ({z1_max_orig}W) határolva z2_max-hoz ({z2_max}W)!")

        return {
            0: (0, 0),
            1: (1, z1_max),
            2: (z1_max + 1, z2_max),
            3: (z2_max + 1, self.max_watt)
        }

    @property
    def hr_zones(self):
        max_hr = self.hr_zone_settings['max_hr']
        z1_max = int(max_hr * self.hr_zone_settings['z1_max_percent'] / 100)
        z2_max = int(max_hr * self.hr_zone_settings['z2_max_percent'] / 100)
        return {
            'resting_hr': self.hr_zone_settings['resting_hr'],
            'z1_max': z1_max,
            'z2_max': z2_max,
        }

    def get_hr_zone(self, hr):
        if hr == 0 or hr < self.hr_zone_settings['resting_hr']:
            return 0
        max_hr = self.hr_zone_settings['max_hr']
        z1_boundary = max_hr * self.hr_zone_settings['z1_max_percent'] / 100
        z2_boundary = max_hr * self.hr_zone_settings['z2_max_percent'] / 100
        if hr < z1_boundary:
            return 1
        if hr < z2_boundary:
            return 2
        return 3

    def is_valid_power(self, power):
        try:
            if not isinstance(power, (int, float)):
                return False
            if power < 0:
                return False
            if power > self.max_watt:
                return False
            return True
        except Exception:
            return False

    def get_zone_for_power(self, power):
        if power == 0:
            return 0
        for zone, (min_p, max_p) in self.zones.items():
            if min_p <= power <= max_p:
                return zone
        return 3

    def check_dropout(self):
        current_time = time.time()
        time_since_last_data = current_time - self.last_data_time

        if time_since_last_data >= self.dropout_timeout:
            with self.state_lock:
                if self.current_zone != 0:
                    print(f"⚠ Adatforrás kiesett ({time_since_last_data:.1f}s) → LEVEL:0")
                    self.current_zone = 0
                    self.cooldown_active = False
                    self.pending_zone = None
                    self.power_buffer.clear()
                    send_needed = True
                else:
                    send_needed = False

            if send_needed:
                self.ble.send_command_sync(0)

    def check_cooldown_and_apply(self, new_zone):
        current_time = time.time()
        time_elapsed = current_time - self.cooldown_start_time
        send_zone = None

        if time_elapsed >= self.cooldown_seconds:
            self.cooldown_active = False
            target_zone = new_zone

            if target_zone != self.current_zone:
                print(f"✓ Cooldown lejárt! Zóna váltás: {self.current_zone} → {target_zone}")
                self.current_zone = target_zone
                self.last_zone_change = current_time
                send_zone = target_zone
            else:
                print(f"✓ Cooldown lejárt, de nincs zóna váltás (már a célzónában vagyunk)")

            self.pending_zone = None
        else:
            remaining = self.cooldown_seconds - time_elapsed
            should_print = (current_time - self.last_cooldown_print) >= 10

            if new_zone != self.pending_zone and new_zone < self.current_zone:
                self.pending_zone = new_zone
                print(f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna frissítve: {new_zone})")
                self.last_cooldown_print = current_time
            elif should_print and new_zone < self.current_zone:
                print(f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {self.pending_zone})")
                self.last_cooldown_print = current_time

        return send_zone

    def should_change_zone(self, new_zone):
        current_time = time.time()

        # --- 0W (leállás) kezelés explicit ---
        if new_zone == 0:
            if self.zero_power_immediate:
                # Azonnali leállás (cooldown nélkül)
                if self.current_zone != 0:
                    print(f"✓ 0W detektálva: azonnali leállás (cooldown nélkül)")
                    self.cooldown_active = False
                    self.pending_zone = None
                    return True
                return False
            else:
                # Normál leállás (cooldown szükséges)
                if self.current_zone != 0:
                    self.cooldown_active = True
                    self.cooldown_start_time = current_time
                    self.pending_zone = 0
                    print(f"🕐 0W detektálva: cooldown indítva {self.cooldown_seconds}s (cél: 0)")
                    return False
                else:
                    # Már 0-ban vagyunk, nincs teendő
                    return False

        if self.cooldown_active:
            if new_zone >= self.current_zone:
                print(f"✓ Teljesítmény emelkedés: cooldown törölve (új zóna: {new_zone} >= jelenlegi: {self.current_zone})")
                self.cooldown_active = False
                self.pending_zone = None
                if new_zone > self.current_zone:
                    return True
                else:
                    return False
            return False

        if new_zone == self.current_zone:
            return False

        if new_zone > self.current_zone:
            return True

        if new_zone < self.current_zone:
            self.cooldown_active = True
            self.cooldown_start_time = current_time
            self.pending_zone = new_zone
            print(f"🕐 Cooldown indítva: {self.cooldown_seconds}s várakozás (cél: {new_zone})")
            return False

        return False

    def process_power_data(self, power):
        with self.state_lock:
            self.last_data_time = time.time()

            if not self.is_valid_power(power):
                print("⚠ FIGYELMEZTETÉS: Érvénytelen adat!")
                return

            power = int(power)
            self.power_buffer.append(power)

            if len(self.power_buffer) < self.minimum_samples:
                print(f"📊 Adatok gyűjtése: {len(self.power_buffer)}/{self.minimum_samples}")
                return

            avg_power = sum(self.power_buffer) // len(self.power_buffer)
            new_power_zone = self.get_zone_for_power(avg_power)
            self.current_power_zone = new_power_zone

            print(f"Átlag teljesítmény: {avg_power}W | Jelenlegi zóna: {self.current_zone} | Új zóna: {new_power_zone}")

            zone_mode = self.hr_zone_settings.get('zone_mode', 'power_only') if self.hr_zone_settings.get('enabled', False) else 'power_only'

            if zone_mode == 'hr_only':
                # Power only tracked for dropout detection; HR drives the fan
                return

            if zone_mode == 'higher_wins' and self.current_hr_zone is not None:
                new_zone = max(new_power_zone, self.current_hr_zone)
            else:
                new_zone = new_power_zone

            cooldown_send_zone = None
            if self.cooldown_active:
                cooldown_send_zone = self.check_cooldown_and_apply(new_zone)

            zone_change_send = None
            if self.current_zone is None or self.should_change_zone(new_zone):
                self.current_zone = new_zone
                self.last_zone_change = time.time()
                zone_change_send = new_zone

        send_zone = cooldown_send_zone if cooldown_send_zone is not None else zone_change_send
        if send_zone is not None:
            self.ble.send_command_sync(send_zone)

    def process_heart_rate_data(self, hr):
        try:
            hr = int(hr)
        except (TypeError, ValueError):
            return
        if hr <= 0 or hr > 250:
            return
        self.current_heart_rate = hr

        if not self.hr_zone_settings.get('enabled', False):
            print(f"❤ Szívfrekvencia: {hr} bpm")
            return

        self.hr_buffer.append(hr)
        avg_hr = sum(self.hr_buffer) // len(self.hr_buffer)
        new_hr_zone = self.get_hr_zone(avg_hr)
        self.current_hr_zone = new_hr_zone

        zone_mode = self.hr_zone_settings.get('zone_mode', 'power_only')
        print(f"❤ HR: {avg_hr} bpm | HR zóna: {new_hr_zone}")

        if zone_mode == 'power_only':
            return

        with self.state_lock:
            if zone_mode == 'hr_only':
                target_zone = new_hr_zone
            else:  # higher_wins
                target_zone = max(self.current_power_zone or 0, new_hr_zone)

            cooldown_send_zone = None
            if self.cooldown_active:
                cooldown_send_zone = self.check_cooldown_and_apply(target_zone)

            zone_change_send = None
            if self.current_zone is None or self.should_change_zone(target_zone):
                self.current_zone = target_zone
                self.last_zone_change = time.time()
                zone_change_send = target_zone

        send_zone = cooldown_send_zone if cooldown_send_zone is not None else zone_change_send
        if send_zone is not None:
            self.ble.send_command_sync(send_zone)


# ============================================================
# ZwiftSource - Zwift UDP adatforrás
# ============================================================
class ZwiftSource:
    def __init__(self, settings, callback, hr_callback=None):
        self.host = settings['host']
        self.port = settings['port']
        self.process_name = settings['process_name']
        self.check_interval = settings['check_interval']
        self.callback = callback
        self.hr_callback = hr_callback

        self.running = False
        self.thread = None
        self.sock = None
        self.zwift_running = False

        self._active_lock = threading.Lock()
        self._active = False

    @property
    def active(self):
        with self._active_lock:
            return self._active

    def set_active(self, active):
        with self._active_lock:
            changed = active != self._active
            self._active = active
        if changed:
            state = "aktív" if active else "passzív"
            print(f"{'✓' if active else '⚠'} Zwift forrás {state}")

    def is_zwift_running(self):
        if not PSUTIL_AVAILABLE:
            return True
        try:
            for proc in psutil.process_iter(['name']):
                if proc.info['name'] and \
                   self.process_name.lower() in proc.info['name'].lower():
                    return True
        except Exception:
            pass
        return False

    def _read_varint(self, data, offset):
        value = 0
        shift = 0
        byte_count = 0
        while offset < len(data) and byte_count < 10:
            b = data[offset]
            offset += 1
            byte_count += 1
            value |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                return value, offset
        return None, offset

    def _parse_power(self, data):
        if not data:
            return None

        if PROTOBUF_AVAILABLE:
            try:
                state = PlayerState()
                state.ParseFromString(data)
                power = state.power
                if isinstance(power, (int, float)) and 0 <= power <= 10000:
                    return int(power)
            except Exception:
                pass

        try:
            if len(data) < 6:
                return None

            offset = 4

            while offset < len(data) - 1:
                tag_byte = data[offset]
                field_number = tag_byte >> 3
                wire_type = tag_byte & 0x07
                offset += 1

                if wire_type == 0:
                    value, offset = self._read_varint(data, offset)
                    if value is None:
                        break
                    if field_number == 4:
                        if 0 <= value <= 10000:
                            return int(value)
                        else:
                            return None

                elif wire_type == 2:
                    length, offset = self._read_varint(data, offset)
                    if length is None:
                        break
                    offset += length

                elif wire_type == 5:
                    offset += 4

                elif wire_type == 1:
                    offset += 8

                else:
                    break

        except Exception:
            pass

        return None

    def _parse_heart_rate(self, data):
        """Parse heart rate (field 6) from Zwift UDP packet."""
        if not data:
            return None

        if PROTOBUF_AVAILABLE:
            try:
                state = PlayerState()
                state.ParseFromString(data)
                hr = state.heart_rate
                if isinstance(hr, (int, float)) and 1 <= hr <= 300:
                    return int(hr)
            except Exception:
                pass

        try:
            if len(data) < 6:
                return None

            offset = 4

            while offset < len(data) - 1:
                tag_byte = data[offset]
                field_number = tag_byte >> 3
                wire_type = tag_byte & 0x07
                offset += 1

                if wire_type == 0:
                    value, offset = self._read_varint(data, offset)
                    if value is None:
                        break
                    if field_number == 6:
                        if 1 <= value <= 300:
                            return int(value)

                elif wire_type == 2:
                    length, offset = self._read_varint(data, offset)
                    if length is None:
                        break
                    offset += length

                elif wire_type == 5:
                    offset += 4

                elif wire_type == 1:
                    offset += 8

                else:
                    break

        except Exception:
            pass

        return None

    def _open_socket(self):
        try:
            self._close_socket()
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.host, self.port))
            self.sock.settimeout(0.5)
            print(f"✓ Zwift UDP socket megnyitva: {self.host}:{self.port}")
        except Exception as e:
            print(f"✗ Zwift UDP socket hiba: {e}")
            self.sock = None

    def _close_socket(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _listen_loop(self):
        last_zwift_check = 0

        while self.running:
            current_time = time.time()

            if current_time - last_zwift_check >= self.check_interval:
                was_running = self.zwift_running
                self.zwift_running = self.is_zwift_running()
                last_zwift_check = current_time

                if self.zwift_running and not was_running:
                    print(f"✓ Zwift elindult, UDP figyelés: {self.host}:{self.port}")
                    self._open_socket()
                elif not self.zwift_running and was_running:
                    print(f"⚠ Zwift leállt, UDP figyelés szünetel")
                    self._close_socket()

            if not self.zwift_running:
                time.sleep(1)
                continue

            if self.sock is None:
                self._open_socket()
                if self.sock is None:
                    time.sleep(1)
                    continue

            try:
                data, addr = self.sock.recvfrom(4096)
                power = self._parse_power(data)

                if power is not None and self.active:
                    self.callback(power)

                if self.hr_callback is not None and self.active:
                    hr = self._parse_heart_rate(data)
                    if hr is not None:
                        self.hr_callback(hr)

            except socket.timeout:
                continue
            except OSError:
                self._close_socket()
                time.sleep(1)
            except Exception as e:
                print(f"⚠ Zwift UDP hiba: {e}")
                time.sleep(1)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(
            target=self._listen_loop,
            daemon=True,
            name="Zwift-Thread"
        )
        self.thread.start()
        print("✓ Zwift UDP listener elindítva")

    def stop(self):
        self.running = False
        self._close_socket()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
            print("✓ Zwift UDP listener leállítva")


# ============================================================
# BLEBridgeServer - ANT+ → BLE broadcast
# ============================================================
class BLEBridgeServer:
    """Re-broadcasts ANT+ power and heart rate data as BLE GATT services."""

    CYCLING_POWER_SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"
    CYCLING_POWER_MEASUREMENT_UUID = "00002a63-0000-1000-8000-00805f9b34fb"
    HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
    HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

    def __init__(self, settings):
        bridge = settings.get('antplus_bridge', {})
        self.enabled = bridge.get('enabled', False)
        broadcast = bridge.get('ble_broadcast', {})
        self.broadcast_enabled = broadcast.get('enabled', True)
        self.power_service_enabled = broadcast.get('power_service', True)
        self.hr_service_enabled = broadcast.get('heart_rate_service', True)
        self.device_name = broadcast.get('device_name', 'SmartFanBridge')

        self._server = None
        self._loop = None
        self._thread = None
        self._running = False

    def is_active(self):
        return self.enabled and self.broadcast_enabled

    def start(self):
        if not self.is_active():
            return
        if not BLESS_AVAILABLE:
            print("⚠ bless library nem elérhető, BLE bridge kikapcsolva")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="BLEBridge-Thread"
        )
        self._thread.start()
        print("✓ BLE Bridge thread elindítva")

    def _run_loop(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._async_run())
        except Exception as e:
            print(f"✗ BLE Bridge kritikus hiba: {e}")
        finally:
            if self._loop:
                self._loop.close()
            print("✓ BLE Bridge thread leállt")

    async def _async_run(self):
        try:
            self._server = BlessServer(self.device_name, loop=self._loop)

            if self.power_service_enabled:
                await self._server.add_new_service(self.CYCLING_POWER_SERVICE_UUID)
                await self._server.add_new_characteristic(
                    self.CYCLING_POWER_SERVICE_UUID,
                    self.CYCLING_POWER_MEASUREMENT_UUID,
                    GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
                    bytearray([0x00, 0x00, 0x00, 0x00]),
                    GATTAttributePermissions.readable,
                )

            if self.hr_service_enabled:
                await self._server.add_new_service(self.HEART_RATE_SERVICE_UUID)
                await self._server.add_new_characteristic(
                    self.HEART_RATE_SERVICE_UUID,
                    self.HEART_RATE_MEASUREMENT_UUID,
                    GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
                    bytearray([0x00, 0x00]),
                    GATTAttributePermissions.readable,
                )

            await self._server.start()
            print(f"✓ BLE Bridge aktív: {self.device_name}")

            while self._running:
                await asyncio.sleep(0.1)

            await self._server.stop()

        except Exception as e:
            print(f"✗ BLE Bridge hiba: {e}")

    def _do_update_power(self, value):
        try:
            char = self._server.get_characteristic(self.CYCLING_POWER_MEASUREMENT_UUID)
            if char:
                char.value = value
                self._server.update_value(
                    self.CYCLING_POWER_SERVICE_UUID,
                    self.CYCLING_POWER_MEASUREMENT_UUID,
                )
        except Exception:
            pass

    def _do_update_heart_rate(self, value):
        try:
            char = self._server.get_characteristic(self.HEART_RATE_MEASUREMENT_UUID)
            if char:
                char.value = value
                self._server.update_value(
                    self.HEART_RATE_SERVICE_UUID,
                    self.HEART_RATE_MEASUREMENT_UUID,
                )
        except Exception:
            pass

    def update_power(self, power_watts):
        if not self._running or not self._server or not self.power_service_enabled:
            return
        try:
            power = max(-32768, min(32767, int(power_watts)))
            value = bytearray(4)
            value[0] = 0x00
            value[1] = 0x00
            value[2] = power & 0xFF
            value[3] = (power >> 8) & 0xFF
            if self._loop:
                self._loop.call_soon_threadsafe(self._do_update_power, value)
        except Exception:
            pass

    def update_heart_rate(self, hr_bpm):
        if not self._running or not self._server or not self.hr_service_enabled:
            return
        try:
            hr = max(0, min(255, int(hr_bpm)))
            value = bytearray([0x00, hr])
            if self._loop:
                self._loop.call_soon_threadsafe(self._do_update_heart_rate, value)
        except Exception:
            pass

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)


# ============================================================
# DataSourceManager - ANT+ / Zwift kezelő
# ============================================================
class DataSourceManager:

    ANTPLUS_STARTUP_GRACE = 30
    ANTPLUS_RECONNECT_DELAY = 5
    ANTPLUS_MAX_RETRIES = 10

    def __init__(self, settings, controller):
        self.settings = settings
        self.controller = controller
        self.ds_settings = settings['data_source']

        self.primary = self.ds_settings['primary']
        self.fallback = self.ds_settings['fallback']
        self.heart_rate_source = self.ds_settings.get('heart_rate_source', 'antplus')

        self.antplus_node = None
        self.antplus_devices = []
        self.antplus_last_data = 0
        self.antplus_startup_grace_end = 0
        self.antplus_last_hr = 0

        self.grace_printed = False
        self.grace_expired_printed = False

        self.zwift_source = None
        self.running = False
        self.monitor_thread = None

        if self.primary == 'zwift' or self.fallback == 'zwift':
            hr_cb = None
            if self.heart_rate_source in ('zwift', 'both'):
                hr_cb = self._on_zwift_hr
            self.zwift_source = ZwiftSource(
                self.ds_settings['zwift'],
                self.controller.process_power_data,
                hr_callback=hr_cb
            )

        self.bridge = BLEBridgeServer(settings)

    def _on_antplus_found(self, device):
        print(f"✓ ANT+ eszköz csatlakoztatva: {device}")
        self.antplus_last_data = time.time()

    def _on_zwift_hr(self, hr):
        """HR callback from Zwift - for 'both' mode only forwards if ANT+ HR is stale."""
        if self.heart_rate_source == 'both':
            dropout_timeout = self.settings.get('dropout_timeout', 5)
            if time.time() - self.antplus_last_hr < dropout_timeout:
                return  # ANT+ HR is still active, skip Zwift HR
        self.controller.process_heart_rate_data(hr)

    def _on_antplus_data(self, page, page_name, data):
        if isinstance(data, PowerData):
            self.antplus_last_data = time.time()
            power = data.instantaneous_power
            self.controller.process_power_data(power)
            self.bridge.update_power(power)
        elif isinstance(data, HeartRateData):
            hr = data.heart_rate
            self.bridge.update_heart_rate(hr)
            if self.heart_rate_source != 'zwift':
                self.antplus_last_hr = time.time()
                self.controller.process_heart_rate_data(hr)

    def _register_antplus_device(self, device):
        self.antplus_devices.append(device)
        device.on_found = lambda: self._on_antplus_found(device)
        device.on_device_data = self._on_antplus_data

    def _init_antplus_node(self):
        self.antplus_node = Node()
        self.antplus_node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)

        self.antplus_devices = []
        meter = PowerMeter(self.antplus_node)
        self._register_antplus_device(meter)

        bridge_settings = self.settings.get('antplus_bridge', {})
        if bridge_settings.get('enabled', False):
            hr_settings = bridge_settings.get('heart_rate', {})
            if hr_settings.get('enabled', True):
                device_id = hr_settings.get('device_id', 0)
                hr_monitor = HeartRate(self.antplus_node, device_id=device_id)
                self._register_antplus_device(hr_monitor)

    def _start_antplus(self):
        try:
            self._init_antplus_node()

            ant_thread = threading.Thread(
                target=self._antplus_loop,
                daemon=True,
                name="ANT+-Thread"
            )
            ant_thread.start()
            print("✓ ANT+ figyelés elindítva")
            return True

        except Exception as e:
            print(f"✗ ANT+ indítási hiba: {e}")
            self.antplus_node = None
            return False

    def _antplus_loop(self):
        retry_count = 0

        while self.running:
            try:
                self.antplus_node.start()
                retry_count = 0
                break

            except Exception as e:
                if not self.running:
                    break

                retry_count += 1
                print(f"⚠ ANT+ kapcsolat megszakadt ({retry_count}/{self.ANTPLUS_MAX_RETRIES}): {e}")
                self.antplus_last_data = 0

                if retry_count >= self.ANTPLUS_MAX_RETRIES:
                    print(f"✗ ANT+ max újracsatlakozási kísérletek elérve ({self.ANTPLUS_MAX_RETRIES})!")
                    print(f"  ANT+ leállítva, csak Zwift fallback marad aktív.")
                    self.antplus_last_data = 0
                    break

                print(f"🔄 ANT+ újracsatlakozás {self.ANTPLUS_RECONNECT_DELAY}s múlva...")
                time.sleep(self.ANTPLUS_RECONNECT_DELAY)

                if not self.running:
                    break

                try:
                    self._stop_antplus_node()
                    self._init_antplus_node()
                    print("✓ ANT+ node újrainicializálva, újrapróbálkozás...")
                except Exception as re:
                    print(f"✗ ANT+ újrainicializálás hiba: {re}")
                    time.sleep(self.ANTPLUS_RECONNECT_DELAY)

    def _stop_antplus_node(self):
        try:
            if self.antplus_devices:
                for d in self.antplus_devices:
                    try:
                        d.close_channel()
                    except Exception:
                        pass
            if self.antplus_node:
                self.antplus_node.stop()
                self.antplus_node = None
            self.antplus_devices = []
        except Exception:
            pass

    def _stop_antplus(self):
        try:
            self._stop_antplus_node()
            self.antplus_last_data = 0
            print("✓ ANT+ leállítva")
        except Exception as e:
            print(f"⚠ ANT+ leállítási hiba: {e}")

    def _monitor_loop(self):
        check_interval = self.ds_settings['zwift']['check_interval']
        dropout_timeout = self.settings['dropout_timeout']
        last_source_print = 0
        last_antplus_ok = None

        while self.running:
            time.sleep(check_interval)

            if not self.running:
                break

            current_time = time.time()

            antplus_has_data = (
                self.antplus_last_data > 0 and
                (current_time - self.antplus_last_data) < dropout_timeout
            )
            zwift_ok = self.zwift_source and self.zwift_source.zwift_running

            if self.primary == 'antplus' and self.fallback == 'zwift' and self.zwift_source:
                in_grace = current_time < self.antplus_startup_grace_end

                if in_grace:
                    if not self.grace_printed:
                        remaining_grace = self.antplus_startup_grace_end - current_time
                        print(f"⏳ ANT+ türelmi idő: {remaining_grace:.0f}s (Zwift fallback passzív)")
                        self.grace_printed = True

                    self.zwift_source.set_active(False)
                    last_antplus_ok = False
                else:
                    if not self.grace_expired_printed:
                        print(f"✓ ANT+ türelmi idő lejárt, normál fallback üzemmód")
                        self.grace_expired_printed = True

                    if antplus_has_data:
                        self.zwift_source.set_active(False)
                        if last_antplus_ok is False:
                            print("✓ ANT+ visszaállt, Zwift fallback passzív")
                    else:
                        self.zwift_source.set_active(True)
                        if last_antplus_ok is True:
                            print("⚠ ANT+ kiesett, Zwift fallback aktív")

                    last_antplus_ok = antplus_has_data

            if current_time - last_source_print >= 30:
                print(f"📡 Adatforrás státusz | "
                      f"ANT+: {'✓' if antplus_has_data else '✗'} | "
                      f"Zwift: {'✓' if zwift_ok else '✗'}")
                last_source_print = current_time

    def start(self):
        self.running = True

        print(f"📡 Elsődleges adatforrás: {self.primary.upper()}")
        if self.fallback != 'none':
            print(f"📡 Másodlagos adatforrás: {self.fallback.upper()}")

        if self.primary == 'antplus' or self.fallback == 'antplus':
            if self.primary == 'antplus':
                self.antplus_startup_grace_end = time.time() + self.ANTPLUS_STARTUP_GRACE
            self._start_antplus()

        if self.zwift_source:
            self.zwift_source.start()

            if self.primary == 'zwift':
                self.zwift_source.set_active(True)
                print("✓ Zwift elsődleges forrásként aktív")
            else:
                self.zwift_source.set_active(False)

        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="DataSource-Monitor"
        )
        self.monitor_thread.start()
        print("✓ Adatforrás monitor elindítva")

        self.bridge.start()

    def stop(self):
        self.running = False

        try:
            self._stop_antplus()
        except Exception as e:
            print(f"ANT+ leállítási hiba: {e}")

        try:
            if self.zwift_source:
                self.zwift_source.stop()
        except Exception as e:
            print(f"Zwift leállítási hiba: {e}")

        try:
            self.bridge.stop()
        except Exception as e:
            print(f"BLE Bridge leállítási hiba: {e}")


# ============================================================
# main()
# ============================================================
def main():
    logging.disable(logging.CRITICAL)

    devnull = open(os.devnull, 'w')
    sys.stderr = devnull

    try:
        print("=" * 60)
        print("  Smart Fan Controller - ANT+ Power Meter → BLE Fan Control")
        print("=" * 60)
        print()

        controller = PowerZoneController("settings.json")

        print()
        print("-" * 60)

        controller.ble.start()
        controller.start_dropout_checker()

        ble_timeout = (controller.settings['ble']['scan_timeout'] +
                       controller.settings['ble']['connection_timeout'])

        if not controller.settings['ble'].get('skip_connection', False):
            print(f"⏳ BLE inicializálás folyamatban (max {ble_timeout}s)...")

        controller.ble.ready_event.wait(timeout=ble_timeout)
        print("✓ BLE inicializálás kész")

        print("-" * 60)
        print()

        data_manager = DataSourceManager(controller.settings, controller)
        data_manager.start()

        print()
        print("🚴 Figyelés elindítva... (Ctrl+C a leállításhoz)")
        print()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n🛑 Leállítás...")
        finally:
            try:
                data_manager.stop()
            except Exception as e:
                print(f"DataSource leállítási hiba: {e}")

            try:
                controller.stop_dropout_checker()
            except Exception as e:
                print(f"Dropout thread leállítási hiba: {e}")

            try:
                controller.ble.stop()
            except Exception as e:
                print(f"BLE leállítási hiba: {e}")

            print()
            print("✓ Program leállítva")
            print()
    finally:
        sys.stderr = sys.__stderr__
        devnull.close()


if __name__ == "__main__":
    main()
