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


class TestIsValidPowerExtended(unittest.TestCase):
    """Test extended power validation: bool, NaN, Inf."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
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


class TestHROnlyUpdatesLastDataTime(unittest.TestCase):
    """BUG #26: process_heart_rate_data should update last_data_time in hr_only mode."""

    def _make_controller(self, zone_mode='hr_only'):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
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


class TestProcessHeartRateDataThreadSafety(unittest.TestCase):
    """BUG #29: process_heart_rate_data must write current_heart_rate under state_lock."""

    def _make_controller(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
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
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(settings, f, indent=2)
        f.close()
        self._tmp = f.name
        return PowerZoneController(f.name)

    def tearDown(self):
        if hasattr(self, '_tmp') and os.path.exists(self._tmp):
            os.unlink(self._tmp)

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


class TestBLEDisconnectTimeout(unittest.TestCase):
    """Test that _disconnect_async uses asyncio.wait_for with timeout."""

    def setUp(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
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


class TestAntplusLoopReconnect(unittest.TestCase):
    """Test that _antplus_loop reconnects on normal node stop (no break)."""

    def test_loop_continues_after_normal_stop(self):
        """When antplus_node.start() returns normally, loop should retry."""
        from smart_fan_controller import DataSourceManager

        settings = copy.deepcopy(DEFAULT_SETTINGS)
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


class TestAntplusLoopRetryReset(unittest.TestCase):
    """BUG #53: retry_count must be reset when antplus_last_data > 0 on node stop."""

    def test_retry_count_resets_after_successful_data(self):
        """retry_count should reset to 0 if antplus_last_data > 0 when node stops."""
        from smart_fan_controller import DataSourceManager

        with patch('smart_fan_controller.Node') as MockNode, \
             patch('smart_fan_controller.PowerMeter'), \
             patch('smart_fan_controller.HeartRate'):
            mock_node_instance = MagicMock()
            MockNode.return_value = mock_node_instance

            dsm = DataSourceManager.__new__(DataSourceManager)
            dsm.running = True
            dsm.antplus_node = mock_node_instance
            dsm.ANTPLUS_MAX_RETRIES = 5
            dsm.ANTPLUS_RECONNECT_DELAY = 0
            dsm._stop_antplus_node = MagicMock()
            dsm._init_antplus_node = MagicMock()

            call_count = [0]

            def start_side_effect():
                call_count[0] += 1
                if call_count[0] == 1:
                    # Simulate successful data received during first run
                    dsm.antplus_last_data = time.time()
                elif call_count[0] == 2:
                    # Stop the loop on second call
                    dsm.running = False

            mock_node_instance.start = start_side_effect
            dsm.antplus_last_data = 0

            dsm._antplus_loop()

            # Loop ran at least twice, meaning retry_count was reset after first stop
            self.assertGreaterEqual(call_count[0], 2)


if __name__ == '__main__':
    unittest.main()
