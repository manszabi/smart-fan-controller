import json
import os
import time
import copy
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
from collections import deque

# Mock external dependencies before importing the module
import sys

# Mock openant
mock_node_module = MagicMock()
mock_devices_module = MagicMock()
mock_power_meter_module = MagicMock()
mock_heart_rate_module = MagicMock()
sys.modules['openant'] = MagicMock()
sys.modules['openant.easy'] = MagicMock()
sys.modules['openant.easy.node'] = mock_node_module
sys.modules['openant.devices'] = mock_devices_module
sys.modules['openant.devices.power_meter'] = mock_power_meter_module
sys.modules['openant.devices.heart_rate'] = mock_heart_rate_module
mock_devices_module.ANTPLUS_NETWORK_KEY = b'\x00' * 8
mock_power_meter_module.PowerMeter = MagicMock
mock_power_meter_module.PowerData = type('PowerData', (), {'instantaneous_power': 0})
mock_heart_rate_module.HeartRate = MagicMock
mock_heart_rate_module.HeartRateData = type('HeartRateData', (), {'heart_rate': 0})

# Mock bleak
sys.modules['bleak'] = MagicMock()

# Mock bless
sys.modules['bless'] = MagicMock()

# Now import the module under test
import smart_fan_controller
from smart_fan_controller import (
    PowerZoneController,
    BLEController,
    BLEBridgeServer,
    ZwiftSource,
    DEFAULT_SETTINGS,
)


class TestPowerZoneControllerInit(unittest.TestCase):
    """Test PowerZoneController initialization and settings loading."""

    def _create_settings_file(self, settings_dict):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings_dict, f, indent=2)
        f.close()
        return f.name

    def tearDown(self):
        if hasattr(self, '_settings_file') and os.path.exists(self._settings_file):
            os.unlink(self._settings_file)

    def test_default_settings_when_file_missing(self):
        """Test that default settings are used when file doesn't exist."""
        tmp_file = os.path.join(tempfile.gettempdir(), 'nonexistent_settings_12345.json')
        if os.path.exists(tmp_file):
            os.unlink(tmp_file)
        controller = PowerZoneController(tmp_file)
        self.assertEqual(controller.ftp, 180)
        self.assertEqual(controller.cooldown_seconds, 120)
        # Clean up generated default file
        if os.path.exists(tmp_file):
            os.unlink(tmp_file)

    def test_valid_settings_loaded(self):
        """Test loading valid settings from file."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ftp'] = 200
        settings['cooldown_seconds'] = 60
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.ftp, 200)
        self.assertEqual(controller.cooldown_seconds, 60)

    def test_invalid_ftp_uses_default(self):
        """Test that invalid FTP value falls back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ftp'] = 9999  # out of 100-500 range
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.ftp, DEFAULT_SETTINGS['ftp'])

    def test_invalid_json_uses_defaults(self):
        """Test that malformed JSON falls back to defaults."""
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        f.write("{invalid json")
        f.close()
        self._settings_file = f.name
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.ftp, DEFAULT_SETTINGS['ftp'])

    def test_zone_thresholds_z1_gte_z2(self):
        """Test that z1 >= z2 threshold reverts to defaults."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['zone_thresholds'] = {'z1_max_percent': 90, 'z2_max_percent': 60}
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(
            controller.zone_thresholds['z1_max_percent'],
            DEFAULT_SETTINGS['zone_thresholds']['z1_max_percent']
        )

    def test_min_watt_gte_max_watt_uses_default(self):
        """Test that min_watt >= max_watt reverts both to defaults."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['min_watt'] = 500
        settings['max_watt'] = 100
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.min_watt, DEFAULT_SETTINGS['min_watt'])
        self.assertEqual(controller.max_watt, DEFAULT_SETTINGS['max_watt'])


class TestZoneCalculation(unittest.TestCase):
    """Test zone boundary calculation."""

    def _make_controller(self, ftp=180, z1_pct=60, z2_pct=89, max_watt=1000):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ftp'] = ftp
        settings['zone_thresholds']['z1_max_percent'] = z1_pct
        settings['zone_thresholds']['z2_max_percent'] = z2_pct
        settings['max_watt'] = max_watt
        settings['ble']['skip_connection'] = True
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        return PowerZoneController(f.name)

    def tearDown(self):
        if hasattr(self, '_tmp') and os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_default_zones(self):
        """Test zone calculation with default FTP=180."""
        controller = self._make_controller(ftp=180)
        zones = controller.zones
        # Zone 0: (0, 0)
        self.assertEqual(zones[0], (0, 0))
        # Zone 1: (1, 108) — 180 * 0.60 = 108
        self.assertEqual(zones[1], (1, 108))
        # Zone 2: (109, 160) — 180 * 0.89 = 160.2 → 160
        self.assertEqual(zones[2], (109, 160))
        # Zone 3: (161, 1000)
        self.assertEqual(zones[3], (161, 1000))

    def test_zones_with_different_ftp(self):
        """Test zone calculation with FTP=250."""
        controller = self._make_controller(ftp=250)
        zones = controller.zones
        # Zone 1: (1, 150) — 250 * 0.60 = 150
        self.assertEqual(zones[1][1], 150)
        # Zone 2: (151, 222) — 250 * 0.89 = 222.5 → 222
        self.assertEqual(zones[2][0], 151)
        self.assertEqual(zones[2][1], 222)


class TestGetZoneForPower(unittest.TestCase):
    """Test power-to-zone mapping."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_zero_power(self):
        self.assertEqual(self.controller.get_zone_for_power(0), 0)

    def test_zone_1(self):
        self.assertEqual(self.controller.get_zone_for_power(50), 1)
        self.assertEqual(self.controller.get_zone_for_power(108), 1)

    def test_zone_2(self):
        self.assertEqual(self.controller.get_zone_for_power(109), 2)
        self.assertEqual(self.controller.get_zone_for_power(160), 2)

    def test_zone_3(self):
        self.assertEqual(self.controller.get_zone_for_power(161), 3)
        self.assertEqual(self.controller.get_zone_for_power(300), 3)

    def test_zone_boundary(self):
        """Test exact boundary values."""
        self.assertEqual(self.controller.get_zone_for_power(1), 1)

    def test_very_high_power(self):
        """Power above max_watt still returns zone 3."""
        self.assertEqual(self.controller.get_zone_for_power(2000), 3)


class TestIsValidPower(unittest.TestCase):
    """Test power validation."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_valid_zero(self):
        self.assertTrue(self.controller.is_valid_power(0))

    def test_valid_normal(self):
        self.assertTrue(self.controller.is_valid_power(200))

    def test_valid_max(self):
        self.assertTrue(self.controller.is_valid_power(1000))

    def test_negative_invalid(self):
        self.assertFalse(self.controller.is_valid_power(-1))

    def test_above_max_invalid(self):
        self.assertFalse(self.controller.is_valid_power(1001))

    def test_string_invalid(self):
        self.assertFalse(self.controller.is_valid_power("abc"))

    def test_none_invalid(self):
        self.assertFalse(self.controller.is_valid_power(None))

    def test_float_valid(self):
        self.assertTrue(self.controller.is_valid_power(150.5))


class TestCooldownLogic(unittest.TestCase):
    """Test cooldown behavior during zone transitions."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['cooldown_seconds'] = 10
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)
        self.controller.ble.running = True  # Prevent "BLE thread not running" warning
        self.controller.current_zone = 3

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_zone_decrease_starts_cooldown(self):
        """Decreasing zone should start cooldown, not change zone."""
        result = self.controller.should_change_zone(1)
        self.assertFalse(result)
        self.assertTrue(self.controller.cooldown_active)
        self.assertEqual(self.controller.pending_zone, 1)
        self.assertEqual(self.controller.current_zone, 3)

    def test_zone_increase_during_cooldown_cancels_cooldown(self):
        """Zone increase during active cooldown should cancel cooldown and change zone.
        
        This is the critical bug fix: previously, should_change_zone was guarded
        by 'not self.cooldown_active', so it was never called during cooldown,
        meaning zone increases were ignored.
        """
        # Start cooldown by decreasing zone
        self.controller.should_change_zone(1)
        self.assertTrue(self.controller.cooldown_active)

        # Now zone increases beyond current — should cancel cooldown
        result = self.controller.should_change_zone(3)
        self.assertFalse(self.controller.cooldown_active)
        self.assertIsNone(self.controller.pending_zone)
        # new_zone == current_zone, so no change needed
        self.assertFalse(result)

    def test_zone_increase_above_current_during_cooldown(self):
        """Zone increase above current during cooldown should change zone."""
        self.controller.current_zone = 2
        self.controller.should_change_zone(1)  # start cooldown
        self.assertTrue(self.controller.cooldown_active)

        result = self.controller.should_change_zone(3)
        self.assertFalse(self.controller.cooldown_active)
        self.assertTrue(result)  # zone should change to 3

    def test_zone_increase_no_cooldown(self):
        """Zone increase without cooldown should change immediately."""
        self.controller.current_zone = 1
        result = self.controller.should_change_zone(3)
        self.assertTrue(result)
        self.assertFalse(self.controller.cooldown_active)

    def test_same_zone_no_change(self):
        """Same zone should not trigger change."""
        result = self.controller.should_change_zone(3)
        self.assertFalse(result)

    def test_zero_power_immediate(self):
        """Zero power immediate mode should skip cooldown."""
        self.controller.zero_power_immediate = True
        self.controller.current_zone = 2
        result = self.controller.should_change_zone(0)
        self.assertTrue(result)
        self.assertFalse(self.controller.cooldown_active)

    def test_zero_power_immediate_already_at_zero(self):
        """Zero power immediate when already at zone 0 returns False."""
        self.controller.zero_power_immediate = True
        self.controller.current_zone = 0
        result = self.controller.should_change_zone(0)
        self.assertFalse(result)


class TestProcessPowerData(unittest.TestCase):
    """Test the main power data processing pipeline."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['cooldown_seconds'] = 10
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)
        self.controller.ble.running = True
        self.sent_commands = []
        self.controller.ble.send_command_sync = lambda level: self.sent_commands.append(level)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_initial_zone_set(self):
        """First power data should set the initial zone."""
        self.controller.process_power_data(200)  # zone 3
        self.assertEqual(self.controller.current_zone, 3)
        self.assertIn(3, self.sent_commands)

    def test_invalid_power_ignored(self):
        """Invalid power data should be ignored."""
        self.controller.current_zone = 1
        self.controller.process_power_data(-5)
        self.assertEqual(self.controller.current_zone, 1)
        self.assertEqual(len(self.sent_commands), 0)

    def test_zone_increase_sends_command(self):
        """Zone increase should send BLE command immediately."""
        self.controller.process_power_data(50)  # zone 1
        self.sent_commands.clear()
        self.controller.power_buffer.clear()
        self.controller.process_power_data(200)  # zone 3
        self.assertIn(3, self.sent_commands)

    def test_zone_decrease_starts_cooldown_no_command(self):
        """Zone decrease should start cooldown, not send lower command."""
        self.controller.process_power_data(200)  # zone 3
        self.sent_commands.clear()
        self.controller.process_power_data(50)  # zone 1
        # No command should be sent (cooldown started)
        self.assertEqual(len(self.sent_commands), 0)
        self.assertTrue(self.controller.cooldown_active)

    def test_zone_increase_during_cooldown_sends_command(self):
        """Zone increase during cooldown should cancel cooldown and send command.
        
        This tests the critical bug fix.
        """
        # Set initial zone to 2
        self.controller.process_power_data(120)  # zone 2
        self.assertEqual(self.controller.current_zone, 2)
        self.sent_commands.clear()

        # Decrease to zone 0 → starts cooldown
        self.controller.power_buffer.clear()
        self.controller.process_power_data(0)  # zone 0
        self.assertTrue(self.controller.cooldown_active)
        self.assertEqual(len(self.sent_commands), 0)

        # Now increase to zone 3 during cooldown → should cancel cooldown
        self.controller.power_buffer.clear()
        self.controller.process_power_data(200)  # zone 3
        self.assertFalse(self.controller.cooldown_active)
        self.assertEqual(self.controller.current_zone, 3)
        self.assertIn(3, self.sent_commands)

    def test_updates_last_data_time(self):
        """Processing power data should update last_data_time."""
        before = time.time()
        self.controller.process_power_data(100)
        after = time.time()
        self.assertGreaterEqual(self.controller.last_data_time, before)
        self.assertLessEqual(self.controller.last_data_time, after)


class TestCheckCooldownAndApply(unittest.TestCase):
    """Test cooldown timer expiration logic."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['cooldown_seconds'] = 5
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)
        self.controller.current_zone = 3
        self.controller.cooldown_active = True
        self.controller.pending_zone = 1

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_cooldown_expired_changes_zone(self):
        """After cooldown expires, zone should change to new value."""
        self.controller.cooldown_start_time = time.time() - 10  # expired
        result = self.controller.check_cooldown_and_apply(1)
        self.assertEqual(result, 1)
        self.assertFalse(self.controller.cooldown_active)
        self.assertEqual(self.controller.current_zone, 1)

    def test_cooldown_expired_same_zone_no_send(self):
        """After cooldown expires, if zone hasn't changed, return None."""
        self.controller.cooldown_start_time = time.time() - 10
        result = self.controller.check_cooldown_and_apply(3)  # same as current
        self.assertIsNone(result)
        self.assertFalse(self.controller.cooldown_active)

    def test_cooldown_not_expired(self):
        """During active cooldown, no zone change."""
        self.controller.cooldown_start_time = time.time()  # just started
        result = self.controller.check_cooldown_and_apply(1)
        self.assertIsNone(result)
        self.assertTrue(self.controller.cooldown_active)


class TestDropout(unittest.TestCase):
    """Test data source dropout detection."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['dropout_timeout'] = 2
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)
        self.controller.ble.running = True
        self.sent_commands = []
        self.controller.ble.send_command_sync = lambda level: self.sent_commands.append(level)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_dropout_resets_to_zone_0(self):
        """Dropout should reset fan to zone 0."""
        self.controller.process_power_data(200)  # set zone 3
        self.assertEqual(self.controller.current_zone, 3)
        self.sent_commands.clear()

        # Simulate timeout
        self.controller.last_data_time = time.time() - 5
        self.controller.check_dropout()
        self.assertEqual(self.controller.current_zone, 0)
        self.assertIn(0, self.sent_commands)

    def test_no_dropout_within_timeout(self):
        """No dropout if data was received recently."""
        self.controller.process_power_data(200)
        self.sent_commands.clear()

        self.controller.check_dropout()
        self.assertEqual(self.controller.current_zone, 3)
        self.assertEqual(len(self.sent_commands), 0)

    def test_dropout_at_zone_0_no_duplicate(self):
        """Dropout when already at zone 0 should not send duplicate."""
        self.controller.current_zone = 0
        self.controller.last_data_time = time.time() - 5
        self.controller.check_dropout()
        self.assertEqual(len(self.sent_commands), 0)


class TestZwiftSimulatorVarint(unittest.TestCase):
    """Test varint encoding in zwift_simulator."""

    def test_encode_varint_zero(self):
        from zwift_simulator import encode_varint
        self.assertEqual(encode_varint(0), b'\x00')

    def test_encode_varint_small(self):
        from zwift_simulator import encode_varint
        self.assertEqual(encode_varint(1), b'\x01')
        self.assertEqual(encode_varint(127), b'\x7f')

    def test_encode_varint_multi_byte(self):
        from zwift_simulator import encode_varint
        # 128 = 0x80 → varint: 0x80 0x01
        result = encode_varint(128)
        self.assertEqual(result, bytes([0x80, 0x01]))

    def test_encode_varint_large(self):
        from zwift_simulator import encode_varint
        # 300 = 0x012C → varint: 0xAC 0x02
        result = encode_varint(300)
        self.assertEqual(result, bytes([0xAC, 0x02]))


class TestZwiftSourceParsePower(unittest.TestCase):
    """Test Zwift UDP packet parsing."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.source = smart_fan_controller.ZwiftSource(
            settings['data_source']['zwift'],
            callback=lambda x: None
        )

    def test_parse_power_from_simulator_packet(self):
        """Test parsing power from a packet created by the simulator."""
        from zwift_simulator import create_zwift_udp_packet
        packet = create_zwift_udp_packet(200)
        # Skip the 4-byte header for raw parsing
        power = self.source._parse_power(packet)
        self.assertEqual(power, 200)

    def test_parse_power_zero(self):
        from zwift_simulator import create_zwift_udp_packet
        packet = create_zwift_udp_packet(0)
        power = self.source._parse_power(packet)
        self.assertEqual(power, 0)

    def test_parse_power_high(self):
        from zwift_simulator import create_zwift_udp_packet
        packet = create_zwift_udp_packet(400)
        power = self.source._parse_power(packet)
        self.assertEqual(power, 400)

    def test_parse_power_too_short(self):
        """Packets that are too short should return None."""
        power = self.source._parse_power(b'\x00\x01')
        self.assertIsNone(power)

    def test_parse_power_empty(self):
        power = self.source._parse_power(b'')
        self.assertIsNone(power)


class TestBLEControllerTestMode(unittest.TestCase):
    """Test BLE controller in test/skip mode."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        self.ble = BLEController(settings)

    def test_skip_connection_flag(self):
        self.assertTrue(self.ble.skip_connection)

    def test_log_command_dedup(self):
        """Duplicate commands should not be logged."""
        self.ble._log_command(1)
        self.assertEqual(self.ble.last_sent_command, 1)
        # Same command again should not update
        self.ble._log_command(1)
        self.assertEqual(self.ble.last_sent_command, 1)

    def test_log_command_different(self):
        """Different commands should be logged."""
        self.ble._log_command(1)
        self.assertEqual(self.ble.last_sent_command, 1)
        self.ble._log_command(2)
        self.assertEqual(self.ble.last_sent_command, 2)

    def test_send_command_sync_not_running(self):
        """Commands should be dropped when BLE thread is not running."""
        self.ble.running = False
        self.ble.send_command_sync(1)
        self.assertTrue(self.ble.command_queue.empty())

    def test_send_command_sync_running(self):
        """Commands should be queued when BLE thread is running."""
        self.ble.running = True
        self.ble.send_command_sync(2)
        self.assertFalse(self.ble.command_queue.empty())
        self.assertEqual(self.ble.command_queue.get_nowait(), 2)

    def test_send_command_sync_invalid_level_negative(self):
        """Negative level should be rejected."""
        self.ble.running = True
        self.ble.send_command_sync(-1)
        self.assertTrue(self.ble.command_queue.empty())

    def test_send_command_sync_invalid_level_too_high(self):
        """Level above 3 should be rejected."""
        self.ble.running = True
        self.ble.send_command_sync(4)
        self.assertTrue(self.ble.command_queue.empty())

    def test_send_command_sync_invalid_level_string(self):
        """Non-integer level should be rejected."""
        self.ble.running = True
        self.ble.send_command_sync("high")
        self.assertTrue(self.ble.command_queue.empty())

    def test_send_command_sync_invalid_level_float(self):
        """Float level should be rejected."""
        self.ble.running = True
        self.ble.send_command_sync(1.5)
        self.assertTrue(self.ble.command_queue.empty())

    def test_send_command_sync_valid_boundaries(self):
        """All valid zone levels (0-3) should be accepted."""
        self.ble.running = True
        for level in range(4):
            self.ble.send_command_sync(level)
            self.assertEqual(self.ble.command_queue.get_nowait(), level)

    def test_send_command_sync_invalid_level_bool(self):
        """Boolean values should be rejected despite being int subtypes."""
        self.ble.running = True
        self.ble.send_command_sync(True)
        self.assertTrue(self.ble.command_queue.empty())
        self.ble.send_command_sync(False)
        self.assertTrue(self.ble.command_queue.empty())


class TestSettingsValidationDataSource(unittest.TestCase):
    """Test data source settings validation."""

    def _create_settings_file(self, settings_dict):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings_dict, f, indent=2)
        f.close()
        return f.name

    def tearDown(self):
        if hasattr(self, '_settings_file') and os.path.exists(self._settings_file):
            os.unlink(self._settings_file)

    def test_primary_equals_fallback_resets_fallback(self):
        """When primary == fallback, fallback should be set to 'none'."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['primary'] = 'zwift'
        settings['data_source']['fallback'] = 'zwift'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['fallback'], 'none')

    def test_invalid_primary_uses_default(self):
        """Invalid primary value should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['primary'] = 'invalid'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['primary'],
                         DEFAULT_SETTINGS['data_source']['primary'])

    def test_invalid_fallback_uses_default(self):
        """Invalid fallback value should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['fallback'] = 'invalid'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['fallback'],
                         DEFAULT_SETTINGS['data_source']['fallback'])

    def test_invalid_port_uses_default(self):
        """Invalid port should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['zwift']['port'] = 99999
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['zwift']['port'],
                         DEFAULT_SETTINGS['data_source']['zwift']['port'])

    def test_valid_zwift_primary(self):
        """Zwift as primary source should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['primary'] = 'zwift'
        settings['data_source']['fallback'] = 'none'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['primary'], 'zwift')
        self.assertEqual(controller.settings['data_source']['fallback'], 'none')


class TestParseEmptyData(unittest.TestCase):
    """Test that _parse_power handles empty/invalid data correctly."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.source = smart_fan_controller.ZwiftSource(
            settings['data_source']['zwift'],
            callback=lambda x: None
        )

    def test_parse_power_none_data(self):
        """None-like empty data should return None."""
        power = self.source._parse_power(b'')
        self.assertIsNone(power)

    def test_parse_power_single_byte(self):
        """Single byte should return None."""
        power = self.source._parse_power(b'\x00')
        self.assertIsNone(power)


class TestSettingsValidationBLE(unittest.TestCase):
    """Test BLE settings validation edge cases."""

    def _create_settings_file(self, settings_dict):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings_dict, f, indent=2)
        f.close()
        return f.name

    def tearDown(self):
        if hasattr(self, '_settings_file') and os.path.exists(self._settings_file):
            os.unlink(self._settings_file)

    def test_invalid_scan_timeout_uses_default(self):
        """Invalid scan_timeout should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['scan_timeout'] = 100
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['ble']['scan_timeout'],
                         DEFAULT_SETTINGS['ble']['scan_timeout'])

    def test_invalid_device_name_empty(self):
        """Empty device_name should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['device_name'] = ''
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['ble']['device_name'],
                         DEFAULT_SETTINGS['ble']['device_name'])

    def test_minimum_samples_exceeds_buffer(self):
        """minimum_samples larger than buffer should be capped."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['buffer_seconds'] = 1  # buffer_size = 4
        settings['minimum_samples'] = 100
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        buffer_size = controller.settings['buffer_seconds'] * 4
        self.assertLessEqual(controller.settings['minimum_samples'], buffer_size)


class TestHeartRateData(unittest.TestCase):
    """Test heart rate data handling in PowerZoneController."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_initial_heart_rate_is_none(self):
        """Heart rate should be None initially."""
        self.assertIsNone(self.controller.current_heart_rate)

    def test_process_valid_heart_rate(self):
        """Valid heart rate should be stored."""
        self.controller.process_heart_rate_data(150)
        self.assertEqual(self.controller.current_heart_rate, 150)

    def test_process_heart_rate_updates(self):
        """Heart rate should update on successive calls."""
        self.controller.process_heart_rate_data(120)
        self.controller.process_heart_rate_data(145)
        self.assertEqual(self.controller.current_heart_rate, 145)

    def test_invalid_heart_rate_zero(self):
        """Zero heart rate should be ignored."""
        self.controller.process_heart_rate_data(100)
        self.controller.process_heart_rate_data(0)
        self.assertEqual(self.controller.current_heart_rate, 100)

    def test_invalid_heart_rate_too_high(self):
        """Heart rate above 250 should be ignored."""
        self.controller.process_heart_rate_data(100)
        self.controller.process_heart_rate_data(300)
        self.assertEqual(self.controller.current_heart_rate, 100)

    def test_invalid_heart_rate_negative(self):
        """Negative heart rate should be ignored."""
        self.controller.process_heart_rate_data(100)
        self.controller.process_heart_rate_data(-5)
        self.assertEqual(self.controller.current_heart_rate, 100)

    def test_invalid_heart_rate_non_numeric(self):
        """Non-numeric heart rate should be ignored."""
        self.controller.process_heart_rate_data(100)
        self.controller.process_heart_rate_data("abc")
        self.assertEqual(self.controller.current_heart_rate, 100)

    def test_heart_rate_boundary_1(self):
        """HR of 1 should be valid."""
        self.controller.process_heart_rate_data(1)
        self.assertEqual(self.controller.current_heart_rate, 1)

    def test_heart_rate_boundary_250(self):
        """HR of 250 should be invalid (above max 220)."""
        self.controller.process_heart_rate_data(100)
        self.controller.process_heart_rate_data(250)
        self.assertEqual(self.controller.current_heart_rate, 100)


class TestBLEBridgeServer(unittest.TestCase):
    """Test BLEBridgeServer initialization and configuration."""

    def _make_settings(self, enabled=False, broadcast_enabled=True,
                       power_service=True, hr_service=True,
                       device_name='SmartFanBridge'):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge'] = {
            'enabled': enabled,
            'heart_rate': {'enabled': True, 'device_id': 0},
            'ble_broadcast': {
                'enabled': broadcast_enabled,
                'power_service': power_service,
                'heart_rate_service': hr_service,
                'device_name': device_name,
            }
        }
        return settings

    def test_disabled_by_default(self):
        """Bridge should be disabled by default."""
        bridge = BLEBridgeServer(DEFAULT_SETTINGS)
        self.assertFalse(bridge.enabled)

    def test_enabled_when_configured(self):
        """Bridge should be enabled when settings say so."""
        settings = self._make_settings(enabled=True)
        bridge = BLEBridgeServer(settings)
        self.assertTrue(bridge.enabled)

    def test_is_active_requires_both_flags(self):
        """is_active() requires both enabled and broadcast_enabled."""
        settings = self._make_settings(enabled=True, broadcast_enabled=True)
        bridge = BLEBridgeServer(settings)
        self.assertTrue(bridge.is_active())

        settings2 = self._make_settings(enabled=True, broadcast_enabled=False)
        bridge2 = BLEBridgeServer(settings2)
        self.assertFalse(bridge2.is_active())

        settings3 = self._make_settings(enabled=False, broadcast_enabled=True)
        bridge3 = BLEBridgeServer(settings3)
        self.assertFalse(bridge3.is_active())

    def test_device_name(self):
        """Device name should be read from settings."""
        settings = self._make_settings(enabled=True, device_name='TestBridge')
        bridge = BLEBridgeServer(settings)
        self.assertEqual(bridge.device_name, 'TestBridge')

    def test_start_does_nothing_when_disabled(self):
        """start() should not create a thread when bridge is disabled."""
        bridge = BLEBridgeServer(DEFAULT_SETTINGS)
        bridge.start()
        self.assertIsNone(bridge._thread)

    def test_update_power_safe_when_not_running(self):
        """update_power() should not raise when bridge is not running."""
        bridge = BLEBridgeServer(DEFAULT_SETTINGS)
        bridge.update_power(200)  # should not raise

    def test_update_heart_rate_safe_when_not_running(self):
        """update_heart_rate() should not raise when bridge is not running."""
        bridge = BLEBridgeServer(DEFAULT_SETTINGS)
        bridge.update_heart_rate(150)  # should not raise

    def test_stop_safe_when_not_started(self):
        """stop() should not raise when bridge was never started."""
        bridge = BLEBridgeServer(DEFAULT_SETTINGS)
        bridge.stop()  # should not raise


class TestAntplusBridgeSettingsValidation(unittest.TestCase):
    """Test antplus_bridge settings validation."""

    def _create_settings_file(self, settings_dict):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings_dict, f, indent=2)
        f.close()
        return f.name

    def tearDown(self):
        if hasattr(self, '_settings_file') and os.path.exists(self._settings_file):
            os.unlink(self._settings_file)

    def test_antplus_bridge_enabled(self):
        """antplus_bridge.enabled=true should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge']['enabled'] = True
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertTrue(controller.settings['antplus_bridge']['enabled'])

    def test_antplus_bridge_disabled(self):
        """antplus_bridge.enabled=false should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge']['enabled'] = False
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertFalse(controller.settings['antplus_bridge']['enabled'])

    def test_invalid_enabled_uses_default(self):
        """Non-boolean enabled should revert to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge']['enabled'] = 'yes'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(
            controller.settings['antplus_bridge']['enabled'],
            DEFAULT_SETTINGS['antplus_bridge']['enabled']
        )

    def test_valid_device_id(self):
        """Valid device_id should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge']['heart_rate']['device_id'] = 12345
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(
            controller.settings['antplus_bridge']['heart_rate']['device_id'], 12345
        )

    def test_invalid_device_id_uses_default(self):
        """Out-of-range device_id should revert to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge']['heart_rate']['device_id'] = 99999
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(
            controller.settings['antplus_bridge']['heart_rate']['device_id'],
            DEFAULT_SETTINGS['antplus_bridge']['heart_rate']['device_id']
        )

    def test_valid_device_name(self):
        """Valid ble_broadcast device_name should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge']['ble_broadcast']['device_name'] = 'MyBridge'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(
            controller.settings['antplus_bridge']['ble_broadcast']['device_name'],
            'MyBridge'
        )

    def test_invalid_antplus_bridge_format(self):
        """Non-dict antplus_bridge should revert to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge'] = 'invalid'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(
            controller.settings['antplus_bridge'],
            DEFAULT_SETTINGS['antplus_bridge']
        )


class TestIsValidPowerExtended(unittest.TestCase):
    """Test extended power validation: bool, NaN, Inf."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_nan_invalid(self):
        """NaN should be rejected."""
        self.assertFalse(self.controller.is_valid_power(float('nan')))

    def test_inf_invalid(self):
        """Positive infinity should be rejected."""
        self.assertFalse(self.controller.is_valid_power(float('inf')))

    def test_negative_inf_invalid(self):
        """Negative infinity should be rejected."""
        self.assertFalse(self.controller.is_valid_power(float('-inf')))

    def test_bool_true_invalid(self):
        """True (bool) should be rejected despite being an int subtype."""
        self.assertFalse(self.controller.is_valid_power(True))

    def test_bool_false_invalid(self):
        """False (bool) should be rejected despite being an int subtype."""
        self.assertFalse(self.controller.is_valid_power(False))

    def test_valid_zero(self):
        """0 (int) should be valid."""
        self.assertTrue(self.controller.is_valid_power(0))

    def test_valid_positive(self):
        """A normal positive watt value should be valid."""
        self.assertTrue(self.controller.is_valid_power(200))


class TestInvalidPowerDoesNotUpdateLastDataTime(unittest.TestCase):
    """Test that invalid power data does not update last_data_time."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_invalid_power_does_not_update_last_data_time(self):
        """Invalid power should NOT update last_data_time."""
        old_time = self.controller.last_data_time
        time.sleep(0.01)
        self.controller.process_power_data(-999)
        self.assertEqual(self.controller.last_data_time, old_time)

    def test_nan_power_does_not_update_last_data_time(self):
        """NaN power should NOT update last_data_time."""
        old_time = self.controller.last_data_time
        time.sleep(0.01)
        self.controller.process_power_data(float('nan'))
        self.assertEqual(self.controller.last_data_time, old_time)

    def test_valid_power_updates_last_data_time(self):
        """Valid power SHOULD update last_data_time."""
        old_time = self.controller.last_data_time
        time.sleep(0.01)
        self.controller.process_power_data(100)
        self.assertGreater(self.controller.last_data_time, old_time)


class TestZwiftSourceSetActive(unittest.TestCase):
    """Test ZwiftSource.set_active() method."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.source = ZwiftSource(
            settings['data_source']['zwift'],
            callback=lambda x: None
        )

    def test_set_active_true(self):
        """set_active(True) should make source active."""
        self.source.set_active(True)
        self.assertTrue(self.source.active)

    def test_set_active_false(self):
        """set_active(False) should make source inactive."""
        self.source.set_active(True)
        self.source.set_active(False)
        self.assertFalse(self.source.active)

    def test_set_active_toggle(self):
        """Toggling active state should work correctly."""
        self.source.set_active(True)
        self.assertTrue(self.source.active)
        self.source.set_active(False)
        self.assertFalse(self.source.active)
        self.source.set_active(True)
        self.assertTrue(self.source.active)

    def test_initial_active_is_false(self):
        """Source should start inactive."""
        self.assertFalse(self.source.active)

    def test_set_active_method_exists(self):
        """set_active should be a method, not a property."""
        self.assertTrue(callable(self.source.set_active))


class TestDataSourceManagerStopJoinsMonitorThread(unittest.TestCase):
    """Test that DataSourceManager.stop() joins the monitor thread."""

    def _make_settings(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['data_source']['primary'] = 'zwift'
        settings['data_source']['fallback'] = 'none'
        return settings

    def test_stop_joins_monitor_thread(self):
        """stop() should join the monitor_thread."""
        from smart_fan_controller import DataSourceManager
        settings = self._make_settings()
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        try:
            controller = PowerZoneController(f.name)
            manager = DataSourceManager(settings, controller)
            # Manually create a monitor_thread mock to track join calls
            join_called = []
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = True
            mock_thread.join.side_effect = lambda timeout=None: join_called.append(timeout)
            manager.monitor_thread = mock_thread
            manager.running = True
            manager.stop()
            self.assertTrue(len(join_called) > 0, "monitor_thread.join() was not called")
        finally:
            if os.path.exists(f.name):
                os.unlink(f.name)

    def test_stop_does_not_raise_when_no_monitor_thread(self):
        """stop() should not raise if monitor_thread is None."""
        from smart_fan_controller import DataSourceManager
        settings = self._make_settings()
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        try:
            controller = PowerZoneController(f.name)
            manager = DataSourceManager(settings, controller)
            manager.monitor_thread = None
            manager.stop()  # should not raise
        finally:
            if os.path.exists(f.name):
                os.unlink(f.name)


class TestBLEBridgeServerStopTimeout(unittest.TestCase):
    """Test BLEBridgeServer.stop() timeout warning."""

    def test_stop_prints_warning_when_thread_does_not_stop(self):
        """stop() should print a warning if the thread is still alive after join."""
        bridge = BLEBridgeServer(DEFAULT_SETTINGS)
        # Simulate a thread that never stops
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True  # always alive
        bridge._thread = mock_thread
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            bridge.stop()
        output = out.getvalue()
        self.assertIn("BLE Bridge thread nem állt le időben", output)

    def test_stop_no_warning_when_thread_stops_in_time(self):
        """stop() should not print a warning if the thread stops."""
        bridge = BLEBridgeServer(DEFAULT_SETTINGS)
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False  # stopped
        bridge._thread = mock_thread
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            bridge.stop()
        output = out.getvalue()
        self.assertNotIn("BLE Bridge thread nem állt le időben", output)


if __name__ == '__main__':
    unittest.main()


class TestBLEPINSettings(unittest.TestCase):
    """Test BLE PIN code settings validation."""

    def _create_settings_file(self, settings_dict):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings_dict, f, indent=2)
        f.close()
        return f.name

    def tearDown(self):
        if hasattr(self, '_settings_file') and os.path.exists(self._settings_file):
            os.unlink(self._settings_file)

    def test_default_pin_code_is_none(self):
        """Default pin_code should be None."""
        self.assertIsNone(DEFAULT_SETTINGS['ble'].get('pin_code'))

    def test_valid_pin_code(self):
        """Valid pin_code in 0-999999 range should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = 123456
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['ble']['pin_code'], 123456)

    def test_pin_code_zero(self):
        """pin_code of 0 should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = 0
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['ble']['pin_code'], 0)

    def test_pin_code_max(self):
        """pin_code of 999999 should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = 999999
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['ble']['pin_code'], 999999)

    def test_pin_code_null(self):
        """pin_code of null (None) should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = None
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertIsNone(controller.settings['ble']['pin_code'])

    def test_invalid_pin_code_too_large(self):
        """pin_code above 999999 should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = 1000000
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertIsNone(controller.settings['ble']['pin_code'])

    def test_invalid_pin_code_negative(self):
        """Negative pin_code should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = -1
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertIsNone(controller.settings['ble']['pin_code'])

    def test_invalid_pin_code_bool(self):
        """Boolean pin_code should be rejected."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = True
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertIsNone(controller.settings['ble']['pin_code'])

    def test_ble_controller_stores_pin_code(self):
        """BLEController should store pin_code from settings."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = 123456
        ble = BLEController(settings)
        self.assertEqual(ble.pin_code, 123456)

    def test_ble_controller_none_pin_code(self):
        """BLEController should store None pin_code."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['pin_code'] = None
        ble = BLEController(settings)
        self.assertIsNone(ble.pin_code)


class TestHeartRateSource(unittest.TestCase):
    """Test heart_rate_source settings validation."""

    def _create_settings_file(self, settings_dict):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings_dict, f, indent=2)
        f.close()
        return f.name

    def tearDown(self):
        if hasattr(self, '_settings_file') and os.path.exists(self._settings_file):
            os.unlink(self._settings_file)

    def test_default_heart_rate_source(self):
        """Default heart_rate_source should be 'antplus'."""
        self.assertEqual(DEFAULT_SETTINGS['data_source']['heart_rate_source'], 'antplus')

    def test_valid_heart_rate_source_antplus(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['heart_rate_source'] = 'antplus'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['heart_rate_source'], 'antplus')

    def test_valid_heart_rate_source_zwift(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['heart_rate_source'] = 'zwift'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['heart_rate_source'], 'zwift')

    def test_valid_heart_rate_source_both(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['heart_rate_source'] = 'both'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['heart_rate_source'], 'both')

    def test_invalid_heart_rate_source_uses_default(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['data_source']['heart_rate_source'] = 'bluetooth'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['data_source']['heart_rate_source'],
                         DEFAULT_SETTINGS['data_source']['heart_rate_source'])


class TestHRZoneSettings(unittest.TestCase):
    """Test heart_rate_zones settings validation."""

    def _create_settings_file(self, settings_dict):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings_dict, f, indent=2)
        f.close()
        return f.name

    def tearDown(self):
        if hasattr(self, '_settings_file') and os.path.exists(self._settings_file):
            os.unlink(self._settings_file)

    def test_default_hr_zones_disabled(self):
        """HR zones should be disabled by default."""
        self.assertFalse(DEFAULT_SETTINGS['heart_rate_zones']['enabled'])

    def test_default_zone_mode_power_only(self):
        """Default zone_mode should be 'power_only'."""
        self.assertEqual(DEFAULT_SETTINGS['heart_rate_zones']['zone_mode'], 'power_only')

    def test_valid_hr_zone_settings(self):
        """Valid HR zone settings should be accepted."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['heart_rate_zones'] = {
            'enabled': True,
            'max_hr': 185,
            'resting_hr': 60,
            'zone_mode': 'hr_only',
            'z1_max_percent': 70,
            'z2_max_percent': 80
        }
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertTrue(controller.settings['heart_rate_zones']['enabled'])
        self.assertEqual(controller.settings['heart_rate_zones']['zone_mode'], 'hr_only')

    def test_invalid_max_hr_too_low(self):
        """max_hr below 100 should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['heart_rate_zones']['max_hr'] = 50
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['heart_rate_zones']['max_hr'],
                         DEFAULT_SETTINGS['heart_rate_zones']['max_hr'])

    def test_invalid_max_hr_too_high(self):
        """max_hr above 220 should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['heart_rate_zones']['max_hr'] = 250
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['heart_rate_zones']['max_hr'],
                         DEFAULT_SETTINGS['heart_rate_zones']['max_hr'])

    def test_invalid_resting_hr(self):
        """resting_hr outside 30-100 should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['heart_rate_zones']['resting_hr'] = 20
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['heart_rate_zones']['resting_hr'],
                         DEFAULT_SETTINGS['heart_rate_zones']['resting_hr'])

    def test_invalid_zone_mode(self):
        """Invalid zone_mode should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['heart_rate_zones']['zone_mode'] = 'invalid'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['heart_rate_zones']['zone_mode'],
                         DEFAULT_SETTINGS['heart_rate_zones']['zone_mode'])

    def test_valid_zone_modes(self):
        """All valid zone modes should be accepted."""
        for mode in ('hr_only', 'higher_wins', 'power_only'):
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            settings['heart_rate_zones']['zone_mode'] = mode
            tmp_file = self._create_settings_file(settings)
            controller = PowerZoneController(tmp_file)
            self.assertEqual(controller.settings['heart_rate_zones']['zone_mode'], mode)
            os.unlink(tmp_file)

    def test_z1_gte_z2_reverts_to_defaults(self):
        """z1_max_percent >= z2_max_percent should revert to defaults."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['heart_rate_zones']['z1_max_percent'] = 80
        settings['heart_rate_zones']['z2_max_percent'] = 70
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['heart_rate_zones']['z1_max_percent'],
                         DEFAULT_SETTINGS['heart_rate_zones']['z1_max_percent'])
        self.assertEqual(controller.settings['heart_rate_zones']['z2_max_percent'],
                         DEFAULT_SETTINGS['heart_rate_zones']['z2_max_percent'])

    def test_invalid_enabled_bool(self):
        """Non-bool enabled should fall back to default."""
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['heart_rate_zones']['enabled'] = 'yes'
        self._settings_file = self._create_settings_file(settings)
        controller = PowerZoneController(self._settings_file)
        self.assertEqual(controller.settings['heart_rate_zones']['enabled'],
                         DEFAULT_SETTINGS['heart_rate_zones']['enabled'])


class TestGetHRZone(unittest.TestCase):
    """Test HR zone calculation."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['heart_rate_zones'] = {
            'enabled': True,
            'max_hr': 185,
            'resting_hr': 60,
            'zone_mode': 'hr_only',
            'z1_max_percent': 70,
            'z2_max_percent': 80
        }
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        self.controller = PowerZoneController(f.name)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_zero_hr_is_zone_0(self):
        """HR == 0 should return zone 0."""
        # HR of 0 is rejected by process_heart_rate_data but get_hr_zone handles it
        self.assertEqual(self.controller.get_hr_zone(0), 0)

    def test_below_resting_is_zone_0(self):
        """HR below resting_hr (60) should return zone 0."""
        self.assertEqual(self.controller.get_hr_zone(55), 0)

    def test_resting_hr_is_zone_1(self):
        """HR at resting_hr should return zone 1."""
        self.assertEqual(self.controller.get_hr_zone(60), 1)

    def test_zone_1(self):
        """HR in zone 1 range should return 1."""
        # 185 * 70 / 100 = 129.5 (float boundary), so 129 < 129.5 → zone 1
        self.assertEqual(self.controller.get_hr_zone(100), 1)
        self.assertEqual(self.controller.get_hr_zone(129), 1)

    def test_zone_2(self):
        """HR in zone 2 range should return 2."""
        # z1_boundary = 129.5, z2_boundary = 148.0
        # 130 >= 129.5 → zone 2; 147 < 148.0 → zone 2
        self.assertEqual(self.controller.get_hr_zone(130), 2)
        self.assertEqual(self.controller.get_hr_zone(140), 2)

    def test_zone_3(self):
        """HR at or above z2_boundary should return zone 3."""
        # 185 * 80 / 100 = 148.0 (exact), so 148 >= 148.0 → zone 3
        self.assertEqual(self.controller.get_hr_zone(148), 3)
        self.assertEqual(self.controller.get_hr_zone(175), 3)

    def test_hr_zones_property(self):
        """hr_zones property should return correct boundaries."""
        zones = self.controller.hr_zones
        self.assertIn('resting_hr', zones)
        self.assertIn('z1_max', zones)
        self.assertIn('z2_max', zones)
        self.assertEqual(zones['resting_hr'], 60)
        self.assertEqual(zones['z1_max'], 129)  # int(185 * 70 / 100) = int(129.5) = 129
        self.assertEqual(zones['z2_max'], 148)  # int(185 * 80 / 100) = int(148.0) = 148


class TestHRZoneControl(unittest.TestCase):
    """Test HR zone-based fan control."""

    def _make_controller(self, zone_mode='power_only', hr_enabled=True):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        settings['heart_rate_zones'] = {
            'enabled': hr_enabled,
            'max_hr': 185,
            'resting_hr': 60,
            'zone_mode': zone_mode,
            'z1_max_percent': 70,
            'z2_max_percent': 80
        }
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        controller = PowerZoneController(f.name)
        controller.ble.running = True
        self.sent_commands = []
        controller.ble.send_command_sync = lambda level: self.sent_commands.append(level)
        return controller

    def tearDown(self):
        if hasattr(self, '_tmp') and os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_power_only_mode_hr_ignored_for_fan(self):
        """In power_only mode, HR data should not send BLE commands."""
        controller = self._make_controller(zone_mode='power_only', hr_enabled=True)
        # First set a power zone
        controller.process_power_data(200)  # zone 3
        self.sent_commands.clear()
        # Now send high HR - should not affect fan in power_only mode
        controller.process_heart_rate_data(175)  # zone 3 HR
        self.assertEqual(len(self.sent_commands), 0)

    def test_hr_only_mode_hr_drives_fan(self):
        """In hr_only mode, HR data should drive the fan."""
        controller = self._make_controller(zone_mode='hr_only')
        # HR above z2_max → zone 3
        controller.process_heart_rate_data(175)
        self.assertIn(3, self.sent_commands)

    def test_hr_only_mode_power_does_not_drive_fan(self):
        """In hr_only mode, power data should not drive the fan."""
        controller = self._make_controller(zone_mode='hr_only')
        # High power but no HR yet - should not send command
        controller.process_power_data(200)  # zone 3
        self.assertEqual(len(self.sent_commands), 0)

    def test_higher_wins_power_higher(self):
        """In higher_wins mode, higher of power/HR zone wins."""
        controller = self._make_controller(zone_mode='higher_wins')
        # Set HR zone to 1 (low HR)
        controller.current_hr_zone = 1
        controller.process_power_data(200)  # power zone 3
        self.assertIn(3, self.sent_commands)

    def test_higher_wins_hr_higher(self):
        """In higher_wins mode, HR zone wins when higher than power zone."""
        controller = self._make_controller(zone_mode='higher_wins')
        # Set power zone to 1 by processing low power
        controller.process_power_data(50)  # zone 1
        self.sent_commands.clear()
        controller.current_power_zone = 1
        # Now set HR to zone 3 (high HR)
        controller.process_heart_rate_data(175)  # zone 3
        self.assertIn(3, self.sent_commands)

    def test_hr_disabled_hr_only_logged(self):
        """When HR zones disabled, HR data should only be logged (no fan control)."""
        controller = self._make_controller(hr_enabled=False)
        controller.process_heart_rate_data(175)
        # current_heart_rate should be updated
        self.assertEqual(controller.current_heart_rate, 175)
        # But no BLE command sent
        self.assertEqual(len(self.sent_commands), 0)

    def test_hr_buffer_smoothing(self):
        """HR buffer should smooth out spikes."""
        controller = self._make_controller(zone_mode='hr_only')
        # Feed multiple HR values; average should determine zone
        for hr in [170, 172, 174, 175]:
            controller.process_heart_rate_data(hr)
        # All values > z2_max (148), so should be in zone 3
        self.assertEqual(controller.current_hr_zone, 3)


class TestZwiftSourceHRParsing(unittest.TestCase):
    """Test Zwift UDP packet heart rate parsing."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.source = ZwiftSource(
            settings['data_source']['zwift'],
            callback=lambda x: None
        )

    def test_parse_heart_rate_empty(self):
        """Empty data should return None."""
        self.assertIsNone(self.source._parse_heart_rate(b''))

    def test_parse_heart_rate_too_short(self):
        """Too-short data should return None."""
        self.assertIsNone(self.source._parse_heart_rate(b'\x00\x01'))

    def test_hr_callback_called_when_set(self):
        """hr_callback should be called with HR from packet when set."""
        from zwift_simulator import create_zwift_udp_packet
        received = []
        source = ZwiftSource(
            DEFAULT_SETTINGS['data_source']['zwift'],
            callback=lambda x: None,
            hr_callback=lambda hr: received.append(hr)
        )
        source._active = True
        # Create packet with default heart_rate=140 (field 6 is included by simulator)
        data = create_zwift_udp_packet(200)
        # Manually invoke what _listen_loop would do
        power = source._parse_power(data)
        if source.hr_callback is not None and source.active:
            hr = source._parse_heart_rate(data)
            if hr is not None:
                source.hr_callback(hr)
        # Simulator encodes field 6 (heart_rate=140 by default)
        self.assertEqual(received, [140])

    def test_hr_callback_none_by_default(self):
        """hr_callback should default to None."""
        source = ZwiftSource(
            DEFAULT_SETTINGS['data_source']['zwift'],
            callback=lambda x: None
        )
        self.assertIsNone(source.hr_callback)


class TestOnAntplusDataBridgeHeartRate(unittest.TestCase):
    """Test that _on_antplus_data only forwards HR to bridge when heart_rate_source != 'zwift'."""

    def _make_manager(self, heart_rate_source):
        from smart_fan_controller import DataSourceManager
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['data_source']['primary'] = 'zwift'
        settings['data_source']['fallback'] = 'none'
        settings['data_source']['heart_rate_source'] = heart_rate_source
class TestHROnlyUpdatesLastDataTime(unittest.TestCase):
    """BUG #26: process_heart_rate_data should update last_data_time in hr_only mode."""

    def _make_controller(self, zone_mode='hr_only'):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        settings['heart_rate_zones'] = {
            'enabled': True,
            'max_hr': 185,
            'resting_hr': 60,
            'zone_mode': zone_mode,
            'z1_max_percent': 70,
            'z2_max_percent': 80
        }
class TestCheckDropoutLocking(unittest.TestCase):
    """Test that check_dropout reads last_data_time inside state_lock."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['dropout_timeout'] = 2
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        controller = PowerZoneController(f.name)
        controller.ble.running = True
        controller.ble.send_command_sync = lambda level: None
        return controller

    def tearDown(self):
        if hasattr(self, '_tmp') and os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_hr_only_updates_last_data_time(self):
        """In hr_only mode, process_heart_rate_data must update last_data_time."""
        controller = self._make_controller(zone_mode='hr_only')
        before = time.time()
        controller.process_heart_rate_data(150)
        self.assertGreaterEqual(controller.last_data_time, before)

    def test_power_only_does_not_update_last_data_time_via_hr(self):
        """In power_only mode, process_heart_rate_data must NOT update last_data_time."""
        controller = self._make_controller(zone_mode='power_only')
        old_time = controller.last_data_time
        controller.process_heart_rate_data(150)
        self.assertEqual(controller.last_data_time, old_time)


class TestOnZwiftHrUsesControllerDropoutTimeout(unittest.TestCase):
    """BUG #27: _on_zwift_hr should use controller.dropout_timeout, not settings.get()."""

    def test_uses_controller_dropout_timeout(self):
        """_on_zwift_hr must read dropout_timeout from controller attribute."""
        from smart_fan_controller import DataSourceManager
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['data_source']['heart_rate_source'] = 'both'
        settings['dropout_timeout'] = 7  # non-default value

        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        tmp = f.name
        try:
            controller = PowerZoneController(tmp)
            received = []
            controller.process_heart_rate_data = lambda hr: received.append(hr)

            manager = DataSourceManager(settings, controller)
            manager.heart_rate_source = 'both'
            # antplus_last_hr was never set (0 by default); time.time() - 0 >> 7
            manager.antplus_last_hr = 0

            # Should NOT be blocked because ANT+ HR is stale (> dropout_timeout ago)
            manager._on_zwift_hr(130)
            self.assertIn(130, received)
        finally:
            os.unlink(tmp)


class TestProcessHeartRateDataThreadSafety(unittest.TestCase):
    """BUG #29: process_heart_rate_data must write current_heart_rate under state_lock."""

    def _make_controller(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        settings['heart_rate_zones']['enabled'] = False
        self.controller = PowerZoneController(f.name)
        self.controller.ble.running = True
        self.sent_commands = []
        self.controller.ble.send_command_sync = lambda level: self.sent_commands.append(level)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_check_dropout_reads_last_data_time_under_lock(self):
        """check_dropout should read last_data_time under state_lock."""
        self.controller.process_power_data(200)
        self.controller.last_data_time = time.time() - 5

        lock_acquired_during_read = []
        real_lock = self.controller.state_lock

        class TrackingLock:
            def __enter__(self_inner):
                lock_acquired_during_read.append(True)
                return real_lock.__enter__()
            def __exit__(self_inner, *args):
                return real_lock.__exit__(*args)

        self.controller.state_lock = TrackingLock()
        self.controller.check_dropout()
        self.assertTrue(len(lock_acquired_during_read) > 0,
                        "state_lock should be acquired in check_dropout")

    def test_dropout_zone_reset_under_lock(self):
        """Dropout should reset zone to 0 and send command."""
        self.controller.process_power_data(200)
        self.assertEqual(self.controller.current_zone, 3)
        self.sent_commands.clear()

        self.controller.last_data_time = time.time() - 5
        self.controller.check_dropout()
        self.assertEqual(self.controller.current_zone, 0)
        self.assertIn(0, self.sent_commands)


class TestCooldownElif(unittest.TestCase):
    """Test that cooldown active and should_change_zone don't run simultaneously (elif)."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        settings['cooldown_seconds'] = 30
        settings['minimum_samples'] = 1
        settings['buffer_seconds'] = 1
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        controller = PowerZoneController(f.name)
        manager = DataSourceManager(settings, controller)
        manager.bridge = MagicMock()
        manager.controller = MagicMock()
        return manager
        controller.ble.running = True
        controller.ble.send_command_sync = lambda level: None
        return controller

    def tearDown(self):
        if hasattr(self, '_tmp') and os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_current_heart_rate_updated_and_lock_released(self):
        """After process_heart_rate_data, current_heart_rate is set and lock released."""
        controller = self._make_controller()
        controller.process_heart_rate_data(120)
        self.assertEqual(controller.current_heart_rate, 120)
        self.assertFalse(controller.state_lock.locked())

    def test_concurrent_hr_writes_do_not_corrupt(self):
        """Multiple threads writing HR should not corrupt the value."""
        controller = self._make_controller()
        errors = []

        def write_hr(val):
            try:
                controller.process_heart_rate_data(val)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_hr, args=(100 + i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertIsNotNone(controller.current_heart_rate)


class TestBLEBridgeServerStopInFinally(unittest.TestCase):
    """BUG #31: _async_run must call _server.stop() even when an exception occurs."""

    def test_stop_called_on_exception(self):
        """_server.stop() must be called in finally block when exception is raised."""
        import asyncio
        from smart_fan_controller import BLEBridgeServer

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge']['ble_broadcast']['enabled'] = True
        server = BLEBridgeServer(settings['antplus_bridge']['ble_broadcast'])
        server._running = False
        server.power_service_enabled = False
        server.hr_service_enabled = False

        stop_called = []

        async def mock_start():
            raise RuntimeError("test error")

        async def mock_stop():
            stop_called.append(True)

        mock_server = MagicMock()
        mock_server.start = mock_start
        mock_server.stop = mock_stop

        with patch('smart_fan_controller.BLESS_AVAILABLE', True), \
             patch('smart_fan_controller.BlessServer', return_value=mock_server):
            server._loop = asyncio.new_event_loop()
            server._loop.run_until_complete(server._async_run())
            server._loop.close()

        self.assertEqual(stop_called, [True])

    def test_stop_called_on_normal_exit(self):
        """_server.stop() must be called in finally block on normal exit."""
        import asyncio
        from smart_fan_controller import BLEBridgeServer

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['antplus_bridge']['ble_broadcast']['enabled'] = True
        server = BLEBridgeServer(settings['antplus_bridge']['ble_broadcast'])
        server._running = False
        server.power_service_enabled = False
        server.hr_service_enabled = False

        stop_called = []

        async def mock_start():
            pass

        async def mock_stop():
            stop_called.append(True)

        mock_server = MagicMock()
        mock_server.start = mock_start
        mock_server.stop = mock_stop

        with patch('smart_fan_controller.BLESS_AVAILABLE', True), \
             patch('smart_fan_controller.BlessServer', return_value=mock_server):
            server._loop = asyncio.new_event_loop()
            server._loop.run_until_complete(server._async_run())
            server._loop.close()

        self.assertEqual(stop_called, [True])


class TestOpenSocketReusePort(unittest.TestCase):
    """BUG #33: _open_socket should attempt to set SO_REUSEPORT when available."""

    def test_so_reuseport_set_when_available(self):
        """If SO_REUSEPORT is defined, it should be passed to setsockopt."""
        import socket as sock_module
        MOCK_SO_REUSEPORT = 15
        source = ZwiftSource(
            DEFAULT_SETTINGS['data_source']['zwift'],
            callback=lambda x: None
        )
        calls = []
        mock_sock = MagicMock()
        mock_sock.setsockopt = lambda *a: calls.append(a)
        mock_sock.bind = MagicMock()
        mock_sock.settimeout = MagicMock()

        with patch('socket.socket', return_value=mock_sock), \
             patch.object(source, '_close_socket'):
            # Ensure SO_REUSEPORT is available
            had_attr = hasattr(sock_module, 'SO_REUSEPORT')
            if not had_attr:
                sock_module.SO_REUSEPORT = MOCK_SO_REUSEPORT
            try:
                source._open_socket()
            finally:
                if not had_attr:
                    delattr(sock_module, 'SO_REUSEPORT')

        opt_names = [c[1] for c in calls]
        self.assertIn(sock_module.SO_REUSEPORT if had_attr else MOCK_SO_REUSEPORT, opt_names)

    def test_so_reuseport_skipped_when_unavailable(self):
        """If SO_REUSEPORT is not defined, _open_socket should not fail."""
        import socket as sock_module
        source = ZwiftSource(
            DEFAULT_SETTINGS['data_source']['zwift'],
            callback=lambda x: None
        )
        mock_sock = MagicMock()
        mock_sock.setsockopt = MagicMock()
        mock_sock.bind = MagicMock()
        mock_sock.settimeout = MagicMock()

        with patch('socket.socket', return_value=mock_sock), \
             patch.object(source, '_close_socket'):
            had_attr = hasattr(sock_module, 'SO_REUSEPORT')
            original = getattr(sock_module, 'SO_REUSEPORT', None)
            if had_attr:
                delattr(sock_module, 'SO_REUSEPORT')
            try:
                source._open_socket()  # Should not raise
            finally:
                if had_attr:
                    setattr(sock_module, 'SO_REUSEPORT', original)

        # SO_REUSEADDR should still be set
        self.assertTrue(mock_sock.setsockopt.call_count >= 1)


class TestVersionExists(unittest.TestCase):
    """BUG #36: __version__ attribute must exist in smart_fan_controller module."""

    def test_version_attribute_exists(self):
        """smart_fan_controller must have a __version__ attribute."""
        self.assertTrue(hasattr(smart_fan_controller, '__version__'))

    def test_version_is_string(self):
        """__version__ must be a non-empty string."""
        self.assertIsInstance(smart_fan_controller.__version__, str)
        self.assertGreater(len(smart_fan_controller.__version__), 0)


class TestSaveDefaultSettingsShowsPath(unittest.TestCase):
    """BUG #32: save_default_settings must print the absolute path."""

    def _make_controller(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        return PowerZoneController(f.name)

    def tearDown(self):
        if hasattr(self, '_tmp') and os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_bridge_not_updated_when_heart_rate_source_is_zwift(self):
        """bridge.update_heart_rate should NOT be called when heart_rate_source='zwift'."""
        manager = self._make_manager('zwift')
        HeartRateData = smart_fan_controller.HeartRateData
        hr_data = HeartRateData()
        hr_data.heart_rate = 155
        manager._on_antplus_data(0, '', hr_data)
        manager.bridge.update_heart_rate.assert_not_called()
        manager.controller.process_heart_rate_data.assert_not_called()

    def test_bridge_updated_when_heart_rate_source_is_antplus(self):
        """bridge.update_heart_rate should be called when heart_rate_source='antplus'."""
        manager = self._make_manager('antplus')
        HeartRateData = smart_fan_controller.HeartRateData
        hr_data = HeartRateData()
        hr_data.heart_rate = 155
        manager._on_antplus_data(0, '', hr_data)
        manager.bridge.update_heart_rate.assert_called_once_with(155)
        manager.controller.process_heart_rate_data.assert_called_once_with(155)
    def test_success_prints_absolute_path(self):
        """On success, save_default_settings should print the absolute path."""
        controller = self._make_controller()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, 'test_settings.json')
            with patch('builtins.print') as mock_print:
                controller.save_default_settings(target)
            printed = ' '.join(str(c) for c in mock_print.call_args_list)
            self.assertIn(os.path.abspath(target), printed)

    def test_permission_error_prints_path(self):
        """On PermissionError, save_default_settings should print the absolute path."""
        controller = self._make_controller()
        with patch('builtins.open', side_effect=PermissionError("no write")), \
             patch('builtins.print') as mock_print:
            controller.save_default_settings('/some/path/settings.json')
        printed = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn(os.path.abspath('/some/path/settings.json'), printed)
        self.controller = PowerZoneController(f.name)
        self.controller.ble.running = True
        self.sent_commands = []
        self.controller.ble.send_command_sync = lambda level: self.sent_commands.append(level)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_should_change_zone_not_called_during_cooldown(self):
        """When cooldown is active, should_change_zone should not be called."""
        self.controller.process_power_data(200)  # zone 3
        self.sent_commands.clear()
        self.controller.power_buffer.clear()
        self.controller.process_power_data(50)  # zone 1 → starts cooldown
        self.assertTrue(self.controller.cooldown_active)

        call_count = []
        original = self.controller.should_change_zone

        def counting_should_change_zone(z):
            call_count.append(z)
            return original(z)

        self.controller.should_change_zone = counting_should_change_zone
        self.controller.power_buffer.clear()
        self.controller.process_power_data(50)  # still zone 1, cooldown active
        self.assertEqual(len(call_count), 0,
                         "should_change_zone must NOT be called when cooldown_active=True")

    def test_cooldown_expired_handled_by_check_cooldown_not_should_change_zone(self):
        """When cooldown is active (even expired), check_cooldown_and_apply handles it, not should_change_zone."""
        self.controller.process_power_data(200)  # zone 3
        self.sent_commands.clear()
        self.controller.power_buffer.clear()
        self.controller.process_power_data(50)  # zone 1 → starts cooldown
        self.assertTrue(self.controller.cooldown_active)
        self.controller.cooldown_start_time = time.time() - 60  # expire cooldown

        call_count = []
        original = self.controller.should_change_zone

        def counting_should_change_zone(z):
            call_count.append(z)
            return original(z)

        self.controller.should_change_zone = counting_should_change_zone
        self.controller.power_buffer.clear()
        self.controller.process_power_data(50)  # zone 1, cooldown expired → handled by check_cooldown_and_apply
        self.assertEqual(len(call_count), 0,
                         "should_change_zone must NOT be called when cooldown_active=True (handled by check_cooldown_and_apply)")


class TestParseHeartRateInvalidReturnsNone(unittest.TestCase):
    """Test that _parse_heart_rate returns None for out-of-range HR values."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.source = smart_fan_controller.ZwiftSource(
            settings['data_source']['zwift'],
            callback=lambda x: None
        )

    def _build_packet_with_field(self, field_number, value):
        """Build a minimal fake protobuf packet with a single varint field."""
        from zwift_simulator import encode_varint
        tag = (field_number << 3) | 0  # wire type 0 = varint
        header = b'\x00' * 4  # 4-byte header
        return header + encode_varint(tag) + encode_varint(value)

    def test_hr_out_of_range_high_returns_none(self):
        """HR value > 220 should return None."""
        data = self._build_packet_with_field(6, 221)
        result = self.source._parse_heart_rate(data)
        self.assertIsNone(result)

    def test_hr_zero_returns_none(self):
        """HR value 0 (below range 1-220) should return None."""
        data = self._build_packet_with_field(6, 0)
        result = self.source._parse_heart_rate(data)
        self.assertIsNone(result)

    def test_hr_boundary_valid_1(self):
        """HR value 1 (minimum valid) should return 1."""
        data = self._build_packet_with_field(6, 1)
        result = self.source._parse_heart_rate(data)
        self.assertEqual(result, 1)

    def test_hr_boundary_valid_220(self):
        """HR value 220 (maximum valid) should return 220."""
        data = self._build_packet_with_field(6, 220)
        result = self.source._parse_heart_rate(data)
        self.assertEqual(result, 220)


class TestParsePacketCombined(unittest.TestCase):
    """Test the _parse_packet method returning power and HR together."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.source = smart_fan_controller.ZwiftSource(
            settings['data_source']['zwift'],
            callback=lambda x: None
        )

    def test_parse_packet_returns_both_values(self):
        """_parse_packet should return (power, hr) tuple."""
        from zwift_simulator import create_zwift_udp_packet
        data = create_zwift_udp_packet(200)
        power, hr = self.source._parse_packet(data)
        self.assertEqual(power, 200)
        self.assertEqual(hr, 140)  # default from simulator

    def test_parse_packet_empty_returns_none_none(self):
        """Empty data should return (None, None)."""
        power, hr = self.source._parse_packet(b'')
        self.assertIsNone(power)
        self.assertIsNone(hr)

    def test_parse_packet_consistent_with_parse_power(self):
        """_parse_power should return same value as first element of _parse_packet."""
        from zwift_simulator import create_zwift_udp_packet
        data = create_zwift_udp_packet(350)
        power_direct = self.source._parse_power(data)
        power_packet, _ = self.source._parse_packet(data)
        self.assertEqual(power_direct, power_packet)

    def test_parse_packet_consistent_with_parse_heart_rate(self):
        """_parse_heart_rate should return same value as second element of _parse_packet."""
        from zwift_simulator import create_zwift_udp_packet
        data = create_zwift_udp_packet(200)
        hr_direct = self.source._parse_heart_rate(data)
        _, hr_packet = self.source._parse_packet(data)
        self.assertEqual(hr_direct, hr_packet)


class TestVarintTagParsing(unittest.TestCase):
    """Test that _parse_packet handles multi-byte (varint) tags correctly."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.source = smart_fan_controller.ZwiftSource(
            settings['data_source']['zwift'],
            callback=lambda x: None
        )

    def _encode_varint(self, value):
        from zwift_simulator import encode_varint
        return encode_varint(value)

    def test_high_field_number_tag_parsed_correctly(self):
        """Field number > 15 requires multi-byte varint tag encoding."""
        # field 20, wire type 0 → tag = (20 << 3) | 0 = 160 → multi-byte varint
        field_number = 20
        tag = (field_number << 3) | 0
        # tag = 160, encoded as varint = 0xA0 0x01
        header = b'\x00' * 4
        # Build: [high_field varint_value][power_field varint_value]
        power_field = 4
        power_tag = (power_field << 3) | 0  # = 32, single byte
        data = header + self._encode_varint(tag) + self._encode_varint(999) + \
               self._encode_varint(power_tag) + self._encode_varint(250)
        power, _ = self.source._parse_packet(data)
        self.assertEqual(power, 250)

    def test_single_byte_tag_still_works(self):
        """Single-byte tags (field < 16) should still work correctly."""
        from zwift_simulator import create_zwift_udp_packet
        data = create_zwift_udp_packet(180)
        power, _ = self.source._parse_packet(data)
        self.assertEqual(power, 180)


class TestBLEDisconnectTimeout(unittest.TestCase):
    """Test that _disconnect_async uses asyncio.wait_for with timeout."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        self.ble = BLEController(settings)

    def test_disconnect_timeout_on_hang(self):
        """_disconnect_async should handle TimeoutError without hanging."""
        import asyncio

        mock_client = MagicMock()

        async def slow_disconnect():
            await asyncio.sleep(10)

        mock_client.disconnect = slow_disconnect

        self.ble.client = mock_client
        self.ble.is_connected = True

        async def run():
            await asyncio.wait_for(self.ble._disconnect_async(), timeout=6.0)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

        self.assertFalse(self.ble.is_connected)

    def test_disconnect_clears_client_on_timeout(self):
        """After timeout disconnect, client should be set to None."""
        import asyncio

        mock_client = MagicMock()

        async def slow_disconnect():
            await asyncio.sleep(10)

        mock_client.disconnect = slow_disconnect
        self.ble.client = mock_client
        self.ble.is_connected = True

        async def run():
            await asyncio.wait_for(self.ble._disconnect_async(), timeout=6.0)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

        self.assertIsNone(self.ble.client)


class TestPsutilNoSuchProcess(unittest.TestCase):
    """Test that is_zwift_running handles NoSuchProcess per-process."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.source = smart_fan_controller.ZwiftSource(
            settings['data_source']['zwift'],
            callback=lambda x: None
        )

    def test_no_such_process_skipped(self):
        """NoSuchProcess on one process should be skipped, not propagate."""
        # Define mock exception classes that don't require psutil to be installed
        class MockNoSuchProcess(Exception):
            def __init__(self, pid=0):
                self.pid = pid

        class MockAccessDenied(Exception):
            pass

        mock_psutil = MagicMock()
        mock_psutil.NoSuchProcess = MockNoSuchProcess
        mock_psutil.AccessDenied = MockAccessDenied

        dying_proc = MagicMock()
        zwift_proc = MagicMock()
        zwift_proc.info.get = MagicMock(return_value='ZwiftApp.exe')

        def proc_iter(attrs):
            yield dying_proc
            yield zwift_proc

        mock_psutil.process_iter = proc_iter
        dying_proc.info.get = MagicMock(side_effect=MockNoSuchProcess(pid=9999))

        orig_available = smart_fan_controller.PSUTIL_AVAILABLE
        orig_psutil = getattr(smart_fan_controller, 'psutil', None)
        try:
            smart_fan_controller.PSUTIL_AVAILABLE = True
            smart_fan_controller.psutil = mock_psutil
            result = self.source.is_zwift_running()
            self.assertTrue(result)
        finally:
            smart_fan_controller.PSUTIL_AVAILABLE = orig_available
            if orig_psutil is None:
                if hasattr(smart_fan_controller, 'psutil'):
                    del smart_fan_controller.psutil
            else:
                smart_fan_controller.psutil = orig_psutil

    def test_all_processes_gone_returns_false(self):
        """If all processes raise NoSuchProcess, return False."""
        class MockNoSuchProcess(Exception):
            def __init__(self, pid=0):
                self.pid = pid

        class MockAccessDenied(Exception):
            pass

        mock_psutil = MagicMock()
        mock_psutil.NoSuchProcess = MockNoSuchProcess
        mock_psutil.AccessDenied = MockAccessDenied

        dying_proc = MagicMock()

        def proc_iter(attrs):
            yield dying_proc

        mock_psutil.process_iter = proc_iter
        dying_proc.info.get = MagicMock(side_effect=MockNoSuchProcess(pid=1234))

        orig_available = smart_fan_controller.PSUTIL_AVAILABLE
        orig_psutil = getattr(smart_fan_controller, 'psutil', None)
        try:
            smart_fan_controller.PSUTIL_AVAILABLE = True
            smart_fan_controller.psutil = mock_psutil
            result = self.source.is_zwift_running()
            self.assertFalse(result)
        finally:
            smart_fan_controller.PSUTIL_AVAILABLE = orig_available
            if orig_psutil is None:
                if hasattr(smart_fan_controller, 'psutil'):
                    del smart_fan_controller.psutil
            else:
                smart_fan_controller.psutil = orig_psutil


class TestAntplusLoopReconnect(unittest.TestCase):
    """Test that _antplus_loop reconnects on normal node stop (no break)."""

    def test_loop_continues_after_normal_stop(self):
        """When antplus_node.start() returns normally, loop should retry."""
        from smart_fan_controller import DataSourceManager

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['ble']['skip_connection'] = True
        controller = PowerZoneController(settings_file=None)
        controller.settings = settings

        with patch('smart_fan_controller.Node') as MockNode, \
             patch('smart_fan_controller.PowerMeter'), \
             patch('smart_fan_controller.HeartRate'):
            mock_node_instance = MagicMock()
            MockNode.return_value = mock_node_instance

            dsm = DataSourceManager.__new__(DataSourceManager)
            dsm.running = True
            dsm.antplus_node = mock_node_instance
            dsm.antplus_last_data = 0
            dsm.ANTPLUS_MAX_RETRIES = 3
            dsm.ANTPLUS_RECONNECT_DELAY = 0
            dsm._stop_antplus_node = MagicMock()
            dsm._init_antplus_node = MagicMock()

            call_count = [0]

            def start_side_effect():
                call_count[0] += 1
                if call_count[0] >= 2:
                    dsm.running = False

            mock_node_instance.start = start_side_effect

            dsm._antplus_loop()

            self.assertGreaterEqual(call_count[0], 2,
                                    "antplus_node.start() should be called more than once after normal stop")


if __name__ == '__main__':
    unittest.main()
