"""
Intelbras Alarm MQTT Bridge
Connects Intelbras AMT/ANM alarm panels via Cloud Relay to Home Assistant via MQTT.
Handles arm/disarm and full zone status publishing.
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import Optional, Dict, Any

import paho.mqtt.client as mqtt
import yaml

from lib.isecnet import CloudRelayClient, AlarmStatus

logger = logging.getLogger("int-alarm")

# Per-module loggers
mqtt_logger = logging.getLogger("int-alarm.mqtt")
cloud_logger = logging.getLogger("int-alarm.cloud")
zone_logger = logging.getLogger("int-alarm.zones")


def load_config(path: str) -> dict:
    """Load YAML config with defaults. Real MAC/password should come from config.yml."""
    config = {
        "alarm": {
            "mac": "REPLACE_WITH_YOUR_PANEL_MAC",
            "password": "REPLACE_WITH_YOUR_PASSWORD",
            "server": "amt.intelbras.com.br",
            "port": 9015,
            "total_zones": 24,
            "poll_interval": 5,
            "command_timeout": 10,
        },
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "username": "",
            "password": "",
            "client_id": "intelbras-alarm-bridge",
            "discovery_prefix": "homeassistant",
            "topic_prefix": "intelbras_alarm",
        },
        "zones": {},
    }
    if os.path.exists(path):
        with open(path, "r") as f:
            user_config = yaml.safe_load(f) or {}
        config["alarm"].update(user_config.get("alarm", {}))
        config["mqtt"].update(user_config.get("mqtt", {}))
        config["zones"].update(user_config.get("zones", {}))
    else:
        logger.warning(f"Config not found at {path}, using defaults")
    return config


def _zone_device_class(n: int) -> str:
    if n <= 8:
        return "door"
    elif n <= 16:
        return "motion"
    else:
        return "safety"


def _has_real_credentials(config: dict) -> bool:
    """Check if MAC/password were actually filled in (not the placeholder)."""
    mac = config["alarm"].get("mac", "")
    pwd = config["alarm"].get("password", "")
    if mac.startswith("REPLACE") or pwd.startswith("REPLACE") or not mac or not pwd:
        return False
    return True


class AlarmBridge:
    """Main bridge between Intelbras alarm panel and MQTT."""

    def __init__(self, config: dict):
        self.config = config
        self.alarm_cfg = config["alarm"]
        self.mqtt_cfg = config["mqtt"]
        self.zones_cfg = config.get("zones", {}) or {}
        self._alarm: Optional[CloudRelayClient] = None
        self.client = mqtt.Client(
            client_id=self.mqtt_cfg["client_id"],
            protocol=mqtt.MQTTv311,
        )
        self._running = True
        self._mqtt_connected = False
        self._cloud_connected = False
        self._last_status: Optional[dict] = None
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5

        # MQTT callbacks
        self.client.on_connect = self._on_mqtt_connect
        self.client.on_disconnect = self._on_mqtt_disconnect
        self.client.on_message = self._on_mqtt_message

        # Last-will: mark as offline if we crash
        self.client.will_set(
            f"{self.mqtt_cfg['topic_prefix']}/availability",
            payload="offline", qos=1, retain=True,
        )

        if self.mqtt_cfg.get("username"):
            self.client.username_pw_set(
                self.mqtt_cfg["username"], self.mqtt_cfg.get("password", "")
            )

    # ------------------------------------------------------------------ Cloud
    def connect_cloud(self) -> bool:
        if self._alarm is None:
            cloud_logger.info(
                f"Initializing cloud relay: server={self.alarm_cfg.get('server', 'amt.intelbras.com.br')}:{self.alarm_cfg.get('port', 9015)}, mac={self.alarm_cfg['mac']}"
            )
            self._alarm = CloudRelayClient(
                mac=self.alarm_cfg["mac"],
                password=self.alarm_cfg["password"],
                server=self.alarm_cfg.get("server", "amt.intelbras.com.br"),
                port=self.alarm_cfg.get("port", 9015),
                timeout=self.alarm_cfg.get("command_timeout", 10),
            )

        cloud_logger.info("Connecting to Intelbras cloud relay...")
        if self._alarm.connect():
            self._cloud_connected = True
            cloud_logger.info("✅ Cloud relay connected")
            return True
        else:
            self._cloud_connected = False
            cloud_logger.error("❌ Cloud relay connection failed")
            return False

    def disconnect_cloud(self):
        if self._alarm:
            cloud_logger.info("Disconnecting from cloud relay")
            self._alarm.disconnect()
            self._alarm = None
        self._cloud_connected = False

    def arm(self) -> bool:
        if not self._alarm:
            cloud_logger.error("Cannot arm: not connected to alarm")
            return False
        cloud_logger.info("Sending ARM command to alarm...")
        result = self._alarm.arm()
        cloud_logger.info(f"Arm result: {'OK' if result else 'FAILED'}")
        return result

    def disarm(self) -> bool:
        if not self._alarm:
            cloud_logger.error("Cannot disarm: not connected to alarm")
            return False
        cloud_logger.info("Sending DISARM command to alarm...")
        result = self._alarm.disarm()
        cloud_logger.info(f"Disarm result: {'OK' if result else 'FAILED'}")
        return result

    def siren_off(self) -> bool:
        if not self._alarm:
            return False
        cloud_logger.info("Sending SIREN OFF command")
        return self._alarm.siren_off()

    # -------------------------------------------------------------------- MQTT
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._mqtt_connected = True
            mqtt_logger.info(
                f"✅ MQTT connected to {self.mqtt_cfg['host']}:{self.mqtt_cfg['port']}"
            )
            # Subscribe to command topics
            cmd_topic = f"{self.mqtt_cfg['topic_prefix']}/command/#"
            client.subscribe(cmd_topic)
            mqtt_logger.info(f"Subscribed to {cmd_topic}")
            siren_topic = f"{self.mqtt_cfg['topic_prefix']}/siren/control"
            client.subscribe(siren_topic)
            mqtt_logger.info(f"Subscribed to {siren_topic}")
            # Mark online
            self._mqtt_publish_availability("online")
        else:
            self._mqtt_connected = False
            mqtt_logger.error(f"❌ MQTT connection failed (rc={rc})")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        self._mqtt_connected = False
        mqtt_logger.warning(f"MQTT disconnected (rc={rc})")

    def _on_mqtt_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8", errors="replace").strip()
        except Exception:
            payload = str(msg.payload)
        mqtt_logger.info(f"📩 MQTT message: topic={topic} payload={payload!r}")

        base = self.mqtt_cfg["topic_prefix"]
        if topic == f"{base}/command/arm":
            self.arm()
        elif topic == f"{base}/command/disarm":
            self.disarm()
        elif topic == f"{base}/siren/control":
            if payload.upper() == "ON":
                cloud_logger.info("Siren ON requested - triggering via bypass+arm hack")
                self._trigger_siren_via_bypass()
            elif payload.upper() == "OFF":
                self.siren_off()
        else:
            mqtt_logger.debug(f"Unhandled topic: {topic}")

    def _trigger_siren_via_bypass(self):
        """Best-effort: try to trigger siren by arming with one zone bypassed."""
        # This is a stub - real implementation would need bypass+arm sequence
        cloud_logger.warning(
            "Siren trigger hack not fully implemented. "
            "Consider using a physical panic button or AMT Mobile app."
        )

    def _mqtt_publish_availability(self, status: str):
        if not self._mqtt_connected:
            return
        topic = f"{self.mqtt_cfg['topic_prefix']}/availability"
        self.client.publish(topic, status, qos=1, retain=True)
        mqtt_logger.debug(f"Published availability: {status} -> {topic}")

    def _mqtt_publish_status(self, status_dict: dict):
        if not self._mqtt_connected:
            mqtt_logger.warning("Cannot publish status: MQTT not connected")
            return
        topic = f"{self.mqtt_cfg['topic_prefix']}/status"
        payload = json.dumps(status_dict)
        result = self.client.publish(topic, payload, qos=1, retain=True)
        mqtt_logger.info(
            f"📤 Published status to {topic} "
            f"(arm={status_dict.get('arm_mode')}, "
            f"zones_open={status_dict.get('zones_open')}, "
            f"siren={status_dict.get('siren_triggered')}) "
            f"[{len(payload)} bytes, mid={result.mid}]"
        )

    # --------------------------------------------------------- Polling & loop
    def _status_to_dict(self, status: AlarmStatus) -> dict:
        return {
            "armed": status.armed,
            "arm_mode": status.arm_mode,
            "is_partitioned": status.is_partitioned,
            "zones_open": status.zones_open,
            "zones_violated": status.zones_violated,
            "zones_bypassed": status.zones_bypassed,
            "total_zones": status.total_zones,
            "siren_triggered": status.siren_triggered,
            "ac_power_loss": status.ac_power_loss,
            "battery_low": status.battery_low,
            "tamper": status.tamper,
            "firmware_version": status.firmware_version,
            "model_name": status.model_name,
            "date_time": status.date_time,
            "connected": status.connected,
            "last_update": datetime.now().isoformat(),
        }

    def poll_once(self):
        if not self._cloud_connected:
            if not self.connect_cloud():
                return
        try:
            total_zones = self.alarm_cfg.get("total_zones", 24)
            cloud_logger.debug(f"Polling alarm status (zones={total_zones})...")
            status = self._alarm.get_status(total_zones)

            # Check for NAK or short response (alarm refused the command)
            raw = status.raw_response or []
            if not status.connected or len(raw) < 20:
                self._consecutive_failures += 1
                cloud_logger.warning(
                    f"Status poll failed (consecutive: {self._consecutive_failures}/{self._max_consecutive_failures}). "
                    f"Raw response ({len(raw)} bytes): {bytes(raw).hex() if raw else '(empty)'}"
                )
                # Mark alarm as unavailable in MQTT
                self._mqtt_publish_availability("offline")
                # Reconnect cloud relay after too many failures
                if self._consecutive_failures >= self._max_consecutive_failures:
                    cloud_logger.warning(
                        "Too many consecutive failures, reconnecting to cloud relay..."
                    )
                    self.disconnect_cloud()
                    self._consecutive_failures = 0
                return

            # Success — reset failure counter
            if self._consecutive_failures:
                cloud_logger.info(
                    f"Status poll recovered after {self._consecutive_failures} failure(s)"
                )
                self._consecutive_failures = 0

            status_dict = self._status_to_dict(status)
            cloud_logger.debug(
                f"Status received: armed={status.armed} mode={status.arm_mode} "
                f"zones_open={status.zones_open} zones_violated={status.zones_violated} "
                f"siren={status.siren_triggered} battery_low={status.battery_low}"
            )
            self._mqtt_publish_status(status_dict)
            self._mqtt_publish_availability("online")
            self._last_status = status_dict
        except Exception as e:
            cloud_logger.error(f"Poll failed: {e}", exc_info=logger.isEnabledFor(logging.DEBUG))
            self._consecutive_failures += 1

    def connect_mqtt(self):
        host = self.mqtt_cfg["host"]
        port = self.mqtt_cfg["port"]
        while self._running:
            try:
                mqtt_logger.info(f"Connecting to MQTT broker at {host}:{port}...")
                self.client.connect(host, port, keepalive=60)
                self.client.loop_start()
                return
            except Exception as e:
                mqtt_logger.error(f"MQTT connection failed: {e}")
                mqtt_logger.info("Retrying in 10s...")
                time.sleep(10)

    def run(self):
        self.connect_mqtt()
        poll_interval = self.alarm_cfg.get("poll_interval", 5)
        cloud_logger.info(f"Main loop started (poll every {poll_interval}s)")

        # Initial poll
        self.poll_once()

        while self._running:
            time.sleep(poll_interval)
            if self._running:
                self.poll_once()

        self.shutdown()

    def shutdown(self):
        logger.info("Shutting down...")
        self._running = False
        self.disconnect_cloud()
        if self._mqtt_connected:
            self._mqtt_publish_availability("offline")
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
        logger.info("Shutdown complete")


# --------------------------------------------------------------------------- main
def main():
    config_path = os.environ.get("CONFIG_PATH", "./config.yml")
    config = load_config(config_path)

    # LOG_LEVEL env var: DEBUG, INFO (default), WARNING, ERROR
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Also turn up paho.mqtt logging when DEBUG is requested
    if log_level <= logging.DEBUG:
        logging.getLogger("paho.mqtt").setLevel(logging.DEBUG)
    if log_level <= logging.INFO:
        # Quiet down very chatty libraries by default
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    logger.info("=" * 60)
    logger.info("Intelbras Alarm MQTT Bridge")
    logger.info("=" * 60)
    logger.info(f"Config: {config_path}")
    logger.info(f"Log level: {log_level_name}")
    logger.info(
        f"Alarm: {config['alarm'].get('server', 'amt.intelbras.com.br')}:"
        f"{config['alarm'].get('port', 9015)} mac={config['alarm']['mac']}"
    )
    logger.info(f"MQTT: {config['mqtt']['host']}:{config['mqtt']['port']}")
    logger.info(f"Zones configured: {len(config.get('zones', {}) or {})}")
    logger.info(f"Poll interval: {config['alarm'].get('poll_interval', 5)}s")

    if not _has_real_credentials(config):
        logger.error(
            "❌ MAC and/or password not set in config.yml. "
            "Copy config.example.yml to config.yml and fill in real values."
        )
        sys.exit(1)

    bridge = AlarmBridge(config)

    def _signal_handler(sig, frame):
        logger.info(f"Signal {sig} received, shutting down...")
        bridge.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.shutdown()


if __name__ == "__main__":
    main()
