import json
import os
import time
import copy
import tempfile
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
        """HR of 250 should be valid."""
        self.controller.process_heart_rate_data(250)
        self.assertEqual(self.controller.current_heart_rate, 250)


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
