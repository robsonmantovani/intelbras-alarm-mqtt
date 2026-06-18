"""Integration tests for MQTT publishing (using mocked paho-mqtt)."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeMqttClient:
    """Mock paho.mqtt.client.Client for testing without a broker."""

    def __init__(self, *args, **kwargs):
        self.published = []  # (topic, payload, qos, retain)
        self.subscribed = []  # list of topics
        self.connected = False
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def connect(self, host, port, keepalive=60):
        self.connected = True
        if self.on_connect:
            self.on_connect(self, None, None, 0)

    def disconnect(self, *args, **kwargs):
        self.connected = False
        if self.on_disconnect:
            self.on_disconnect(self, None, None, 0)

    def loop_start(self):
        pass

    def loop_stop(self, *args, **kwargs):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        msg_info = MagicMock()
        msg_info.wait_for_publish.return_value = None
        msg_info.is_published.return_value = True
        msg_info.mid = len(self.published)
        return msg_info

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def will_set(self, *args, **kwargs):
        pass

    def username_pw_set(self, *args, **kwargs):
        pass

    def simulate_message(self, topic, payload):
        """Simulate an incoming MQTT message."""
        if self.on_message:
            msg = type("msg", (), {
                "topic": topic,
                "payload": payload.encode() if isinstance(payload, str) else payload,
            })()
            self.on_message(self, None, msg)


class TestMQTTIntegration(unittest.TestCase):
    """Test MQTT publishing and subscription with mocked client."""

    def _make_bridge(self):
        """Create a bridge with a FakeMqttClient."""
        from app import AlarmBridge
        bridge = AlarmBridge.__new__(AlarmBridge)
        bridge.client = FakeMqttClient()
        bridge.mqtt_cfg = {
            "host": "localhost",
            "port": 1883,
            "username": "",
            "password": "",
            "client_id": "test-client",
            "topic_prefix": "intelbras_alarm",
        }
        bridge.alarm_cfg = {
            "mac": "AABBCCDDEEFF",
            "password": "1234",
            "total_zones": 24,
        }
        bridge.zones_cfg = {}
        bridge._alarm = None
        bridge._alarm_lock = threading.Lock()
        bridge._consecutive_failures = 0
        bridge._mqtt_connected = True
        # Skip slow verify polls - just return reported
        bridge._verify_arm_state = lambda reported, expected_armed: reported
        return bridge

    def test_publish_status_publishes_to_correct_topic(self):
        """_mqtt_publish_status sends JSON to intelbras_alarm/status."""
        import json
        bridge = self._make_bridge()
        status_dict = {
            "armed": True,
            "arm_mode": "armed_away",
            "zones_open": [],
            "siren_triggered": False,
        }
        bridge._mqtt_publish_status(status_dict)
        status_pubs = [p for p in bridge.client.published
                       if p[0] == "intelbras_alarm/status"]
        self.assertEqual(len(status_pubs), 1)
        topic, payload, qos, retain = status_pubs[0]
        self.assertEqual(qos, 1)
        self.assertTrue(retain)
        data = json.loads(payload)
        self.assertTrue(data["armed"])

    def test_publish_status_skipped_when_disconnected(self):
        """No publish if MQTT not connected."""
        bridge = self._make_bridge()
        bridge._mqtt_connected = False
        bridge._mqtt_publish_status({"armed": False})
        self.assertEqual(len(bridge.client.published), 0)

    def test_publish_availability(self):
        """_mqtt_publish_availability publishes to availability topic."""
        bridge = self._make_bridge()
        bridge._mqtt_publish_availability("online")
        avail_pubs = [p for p in bridge.client.published
                      if "availability" in p[0]]
        self.assertEqual(len(avail_pubs), 1)
        self.assertEqual(avail_pubs[0][1], "online")
        self.assertTrue(avail_pubs[0][3])  # retain

    def test_publish_availability_offline(self):
        """_mqtt_publish_availability can publish offline too."""
        bridge = self._make_bridge()
        bridge._mqtt_publish_availability("offline")
        avail_pubs = [p for p in bridge.client.published
                      if "availability" in p[0]]
        self.assertEqual(avail_pubs[0][1], "offline")

    def test_subscribe_to_command_topics(self):
        """On connect, subscribe to intelbras_alarm/#."""
        bridge = self._make_bridge()
        # Trigger the connect callback manually
        bridge._on_mqtt_connect(bridge.client, None, None, 0)
        # Should subscribe to intelbras_alarm/#
        self.assertIn("intelbras_alarm/#", bridge.client.subscribed)

    def test_command_received_dispatches_to_arm(self):
        """ARM_AWAY MQTT message -> alarm.arm() called."""
        called = []
        class FakeAlarm:
            def arm(self): called.append("arm"); return True
        bridge = self._make_bridge()
        bridge._alarm = FakeAlarm()
        bridge.poll_once = lambda: None
        bridge._on_mqtt_message(None, None, type("msg", (), {
            "topic": "intelbras_alarm/command",
            "payload": b"ARM_AWAY",
        })())
        self.assertIn("arm", called)

    def test_command_received_dispatches_to_disarm(self):
        """DISARM MQTT message -> alarm.disarm() called."""
        called = []
        class FakeAlarm:
            def disarm(self): called.append("disarm"); return True
        bridge = self._make_bridge()
        bridge._alarm = FakeAlarm()
        bridge.poll_once = lambda: None
        bridge._on_mqtt_message(None, None, type("msg", (), {
            "topic": "intelbras_alarm/command",
            "payload": b"DISARM",
        })())
        self.assertIn("disarm", called)

    def test_emergency_button_dispatches_panic(self):
        """Emergency button -> alarm.panic() called."""
        called = []
        class FakeAlarm:
            def panic(self): called.append("panic"); return True
        bridge = self._make_bridge()
        bridge._alarm = FakeAlarm()
        bridge.poll_once = lambda: None
        bridge._on_mqtt_message(None, None, type("msg", (), {
            "topic": "intelbras_alarm/emergency",
            "payload": b"PRESS",
        })())
        self.assertIn("panic", called)

    def test_status_topic_retained(self):
        """Status topic must be retained so HA sees last value on connect."""
        bridge = self._make_bridge()
        bridge._mqtt_publish_status({"armed": False, "arm_mode": "disarmed"})
        status_pubs = [p for p in bridge.client.published
                       if p[0] == "intelbras_alarm/status"]
        self.assertTrue(status_pubs[0][3], "Status topic must be retained")


if __name__ == "__main__":
    unittest.main()
