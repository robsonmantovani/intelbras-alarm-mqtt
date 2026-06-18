"""Unit tests for app.py (config loading, status dict, MQTT dispatch)."""
import sys
import unittest
import tempfile
import os
import threading
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestLoadConfig(unittest.TestCase):
    """Test config loading and validation."""

    def _write_config(self, content):
        """Helper: write content to temp YAML and return path."""
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        )
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_minimal_config(self):
        from app import load_config
        path = self._write_config(
            'mqtt:\n'
            '  host: 192.168.1.100\n'
            '  port: 1883\n'
            'alarm:\n'
            '  mac: "AABBCCDDEEFF"\n'
            '  password: "1234"\n'
            '  total_zones: 24\n'
        )
        cfg = load_config(path)
        self.assertEqual(cfg["mqtt"]["host"], "192.168.1.100")
        self.assertEqual(cfg["mqtt"]["port"], 1883)
        self.assertEqual(cfg["alarm"]["mac"], "AABBCCDDEEFF")
        self.assertEqual(cfg["alarm"]["password"], "1234")
        self.assertEqual(cfg["alarm"]["total_zones"], 24)

    def test_default_mqtt(self):
        from app import load_config
        path = self._write_config(
            'alarm:\n'
            '  mac: "AABBCCDDEEFF"\n'
            '  password: "1234"\n'
        )
        cfg = load_config(path)
        self.assertIn("mqtt", cfg)
        self.assertEqual(cfg["mqtt"]["host"], "localhost")
        self.assertEqual(cfg["mqtt"]["port"], 1883)
        # alarm total_zones defaults to 24
        self.assertEqual(cfg["alarm"]["total_zones"], 24)

    def test_topic_prefix_default(self):
        from app import load_config
        path = self._write_config(
            'alarm:\n'
            '  mac: "AABBCCDDEEFF"\n'
            '  password: "1234"\n'
        )
        cfg = load_config(path)
        # Default topic_prefix is intelbras_alarm
        self.assertEqual(cfg["mqtt"]["topic_prefix"], "intelbras_alarm")

    def test_zones_config(self):
        from app import load_config
        path = self._write_config(
            'alarm:\n'
            '  mac: "AABBCCDDEEFF"\n'
            '  password: "1234"\n'
            'zones:\n'
            '  6:\n'
            '    name: "Porta da Frente"\n'
            '    device_class: "door"\n'
            '  7:\n'
            '    name: "Janela da Sala"\n'
            '    device_class: "window"\n'
        )
        cfg = load_config(path)
        zones = cfg["zones"]
        self.assertIn("6", zones)
        self.assertIn("7", zones)
        self.assertEqual(zones["6"]["name"], "Porta da Frente")
        self.assertEqual(zones["6"]["device_class"], "door")

    def test_zones_keys_normalized_to_string(self):
        """Critical: YAML parses 6 as int, code must normalize to str."""
        from app import load_config
        path = self._write_config(
            'alarm:\n'
            '  mac: "AABBCCDDEEFF"\n'
            '  password: "1234"\n'
            'zones:\n'
            '  6:\n'
            '    name: "Test Zone"\n'
        )
        cfg = load_config(path)
        zones = cfg["zones"]
        # String key must work
        self.assertIn("6", zones)
        self.assertEqual(zones["6"]["name"], "Test Zone")

    def test_null_alarm_section(self):
        """YAML with alarm: null should not crash."""
        from app import load_config
        path = self._write_config(
            'alarm: null\n'
            'mqtt: null\n'
        )
        # Should not raise TypeError
        cfg = load_config(path)
        # Defaults should be filled
        self.assertEqual(cfg["alarm"]["total_zones"], 24)
        self.assertEqual(cfg["mqtt"]["host"], "localhost")


class TestStatusToDict(unittest.TestCase):
    """Test the conversion from AlarmStatus to MQTT payload dict."""

    def test_basic_status(self):
        from app import AlarmBridge
        from lib.isecnet import AlarmStatus

        bridge = AlarmBridge.__new__(AlarmBridge)  # bypass __init__
        status = AlarmStatus()
        status.armed = True
        status.arm_mode = "armed_away"
        status.zones_open = [1, 6]
        status.zones_violated = []
        status.zones_bypassed = []
        status.total_zones = 24
        status.siren_triggered = False
        status.alarm_triggered = False
        status.ac_power_loss = False
        status.battery_low = True
        status.firmware_version = "6.6"
        status.model_name = "ANM 24 NET"
        status.connected = True

        d = bridge._status_to_dict(status)
        self.assertTrue(d["armed"])
        self.assertEqual(d["arm_mode"], "armed_away")
        self.assertEqual(d["zones_open"], [1, 6])
        self.assertEqual(d["total_zones"], 24)
        self.assertTrue(d["battery_low"])
        self.assertEqual(d["firmware_version"], "6.6")
        self.assertEqual(d["model_name"], "ANM 24 NET")
        self.assertTrue(d["connected"])
        self.assertIn("last_update", d)
        self.assertIn("date_time", d)


class TestMQTTCommandHandling(unittest.TestCase):
    """Test the MQTT message dispatcher."""

    def _make_bridge(self, alarm):
        from app import AlarmBridge
        bridge = AlarmBridge.__new__(AlarmBridge)
        bridge._alarm = alarm
        bridge._alarm_lock = threading.Lock()
        bridge.mqtt_cfg = {"topic_prefix": "intelbras_alarm"}
        # Skip slow verify polls (which would call alarm.get_status)
        bridge._verify_arm_state = lambda reported, expected_armed: reported
        return bridge

    def _make_msg(self, topic, payload):
        return type("msg", (), {
            "topic": topic, "payload": payload.encode()
        })()

    def test_arm_away_command(self):
        called = []
        class FakeAlarm:
            def arm(self): called.append("arm"); return True
        bridge = self._make_bridge(FakeAlarm())
        bridge.poll_once = lambda: None  # skip cloud connection
        bridge._on_mqtt_message(None, None, self._make_msg(
            "intelbras_alarm/command", "ARM_AWAY"
        ))
        self.assertEqual(called, ["arm"])

    def test_disarm_command(self):
        called = []
        class FakeAlarm:
            def disarm(self): called.append("disarm"); return True
        bridge = self._make_bridge(FakeAlarm())
        bridge.poll_once = lambda: None
        bridge._on_mqtt_message(None, None, self._make_msg(
            "intelbras_alarm/command", "DISARM"
        ))
        self.assertEqual(called, ["disarm"])

    def test_arm_home_command(self):
        called = []
        class FakeAlarm:
            def arm_stay(self): called.append("arm_stay"); return True
        bridge = self._make_bridge(FakeAlarm())
        bridge.poll_once = lambda: None
        bridge._on_mqtt_message(None, None, self._make_msg(
            "intelbras_alarm/command", "ARM_HOME"
        ))
        self.assertEqual(called, ["arm_stay"])

    def test_panic_command(self):
        called = []
        class FakeAlarm:
            def panic(self): called.append("panic"); return True
        bridge = self._make_bridge(FakeAlarm())
        bridge.poll_once = lambda: None
        bridge._on_mqtt_message(None, None, self._make_msg(
            "intelbras_alarm/command", "PANIC"
        ))
        self.assertEqual(called, ["panic"])

    def test_emergency_button(self):
        called = []
        class FakeAlarm:
            def panic(self): called.append("panic"); return True
        bridge = self._make_bridge(FakeAlarm())
        bridge.poll_once = lambda: None
        bridge._on_mqtt_message(None, None, self._make_msg(
            "intelbras_alarm/emergency", "PRESS"
        ))
        self.assertEqual(called, ["panic"])

    def test_unknown_command_ignored(self):
        called = []
        class FakeAlarm:
            def arm(self): called.append("arm")
            def disarm(self): called.append("disarm")
        bridge = self._make_bridge(FakeAlarm())
        bridge.poll_once = lambda: None
        # Unknown payload - should not call any method
        bridge._on_mqtt_message(None, None, self._make_msg(
            "intelbras_alarm/command", "BOGUS_COMMAND"
        ))
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
