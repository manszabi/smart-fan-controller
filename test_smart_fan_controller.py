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
sys.modules['openant'] = MagicMock()
sys.modules['openant.easy'] = MagicMock()
sys.modules['openant.easy.node'] = mock_node_module
sys.modules['openant.devices'] = mock_devices_module
sys.modules['openant.devices.power_meter'] = mock_power_meter_module
mock_devices_module.ANTPLUS_NETWORK_KEY = b'\x00' * 8
mock_power_meter_module.PowerMeter = MagicMock
mock_power_meter_module.PowerData = type('PowerData', (), {'instantaneous_power': 0})

# Mock bleak
sys.modules['bleak'] = MagicMock()

# Now import the module under test
import smart_fan_controller
from smart_fan_controller import (
    PowerZoneController,
    BLEController,
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
        tmp_file = '/tmp/nonexistent_settings_12345.json'
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


if __name__ == '__main__':
    unittest.main()
