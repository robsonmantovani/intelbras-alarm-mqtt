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
import threading
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

        # Normalize zone keys to strings (YAML parses "6:" as int, but we
        # need string keys for the rest of the code to work)
        user_zones = user_config.get("zones", {}) or {}
        normalized_zones = {str(k): v for k, v in user_zones.items()}
        config["zones"].update(normalized_zones)
    else:
        logger.warning(f"Config not found at {path}, using defaults")
    return config


def _has_real_credentials(config: dict) -> bool:
    """Check if MAC/password were actually filled in (not the placeholder)."""
    mac = config["alarm"].get("mac", "")
    pwd = config["alarm"].get("password", "")
    if mac.startswith("REPLACE") or pwd.startswith("REPLACE") or not mac or not pwd:
        return False
    return True


def _zone_device_class(n: int) -> str:
    """Default device class for a zone number."""
    if n <= 8:
        return "door"
    elif n <= 16:
        return "motion"
    else:
        return "safety"


def _discovery_topic(prefix: str, component: str, object_id: str) -> str:
    """Build HA MQTT discovery topic."""
    return f"{prefix}/{component}/{object_id}/config"


def publish_discovery(client: mqtt.Client, config: dict):
    """Publish Home Assistant MQTT discovery payloads for all entities."""
    mqtt_cfg = config["mqtt"]
    alarm_cfg = config["alarm"]
    zones_cfg = config.get("zones", {}) or {}
    prefix = mqtt_cfg["discovery_prefix"]
    topic_base = mqtt_cfg["topic_prefix"]
    device_name = "Central de Alarme Intelbras"
    device_id = "intelbras_alarm_central"

    device = {
        "identifiers": [device_id],
        "name": device_name,
        "manufacturer": "Intelbras",
        "model": alarm_cfg.get("panel_model", "ANM 24 NET"),
        "sw_version": "1.0.0",
    }

    # --- Alarm Control Panel ---
    # ANM 24 NET only supports 2 arm modes: ARM_AWAY (total) and ARM_HOME (partial)
    # plus DISARM. Night/vacation/custom_bypass are not supported.
    alarm_payload = {
        "name": "Alarme Intelbras",
        "unique_id": f"{device_id}_panel",
        "device": device,
        "state_topic": f"{topic_base}/status",
        "value_template": "{{ value_json.arm_mode }}",
        "command_topic": f"{topic_base}/command",
        "availability_topic": f"{topic_base}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        "code_arm_required": "false",
        # HA alarm_control_panel standard payloads
        "payload_arm_away": "ARM_AWAY",
        "payload_arm_home": "ARM_HOME",
        "payload_arm_night": "ARM_HOME",  # map to partial (ANM has no night)
        "payload_arm_custom_bypass": "ARM_AWAY",  # map to total
        "payload_disarm": "DISARM",
    }

    client.publish(
        _discovery_topic(prefix, "alarm_control_panel", f"{device_id}_panel"),
        json.dumps(alarm_payload), retain=True,
    )
    mqtt_logger.info(f"Published HA discovery: alarm_control_panel (ARM_AWAY, ARM_HOME, DISARM)")

    # --- Zone binary_sensors ---
    if zones_cfg:
        # Only publish zones listed in config
        zone_list = sorted(int(k) for k in zones_cfg.keys())
        mqtt_logger.info(
            f"Publishing {len(zone_list)} zones from config: {zone_list}"
        )
        for zn in zone_list:
            zname = zones_cfg.get(str(zn), {}).get("name", f"Zona {zn}")
            mqtt_logger.info(f"  Zone {zn}: '{zname}'")
    else:
        # No zone config — publish all zones with default names
        zone_list = list(range(1, alarm_cfg.get("total_zones", 24) + 1))
        mqtt_logger.info(
            f"No zone names configured, publishing all {len(zone_list)} zones with default names"
        )

    for zone_num in zone_list:
        zone_key = str(zone_num)
        zone_info = zones_cfg.get(zone_key, {}) if zones_cfg else {}
        zone_name = zone_info.get("name", f"Zona {zone_num}")
        zone_class = zone_info.get("device_class", _zone_device_class(zone_num))

        zone_id = f"{device_id}_zone_{zone_num}"
        zone_payload = {
            "name": zone_name,
            "unique_id": zone_id,
            "device": device,
            "state_topic": f"{topic_base}/status",
            "value_template": (
                f"{{{{ 'ON' if {zone_num} in (value_json.zones_open or []) "
                f"or {zone_num} in (value_json.zones_violated or []) else 'OFF' }}}}"
            ),
            "device_class": zone_class,
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": f"{topic_base}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        client.publish(
            _discovery_topic(prefix, "binary_sensor", zone_id),
            json.dumps(zone_payload), retain=True,
        )
    mqtt_logger.info(f"Published HA discovery: {len(zone_list)} zone binary_sensors")

    # --- Siren ---
    client.publish(
        _discovery_topic(prefix, "binary_sensor", f"{device_id}_siren"),
        json.dumps({
            "name": "Sirene",
            "unique_id": f"{device_id}_siren",
            "device": device,
            "state_topic": f"{topic_base}/status",
            "value_template": "{{ 'ON' if value_json.siren_triggered else 'OFF' }}",
            "device_class": "sound",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": f"{topic_base}/availability",
            "payload_available": "online", "payload_not_available": "offline",
        }), retain=True,
    )

    # --- AC Power ---
    client.publish(
        _discovery_topic(prefix, "binary_sensor", f"{device_id}_ac_power"),
        json.dumps({
            "name": "Rede Elétrica",
            "unique_id": f"{device_id}_ac_power",
            "device": device,
            "state_topic": f"{topic_base}/status",
            "value_template": "{{ 'OFF' if value_json.ac_power_loss else 'ON' }}",
            "device_class": "power",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": f"{topic_base}/availability",
            "payload_available": "online", "payload_not_available": "offline",
        }), retain=True,
    )

    # --- Battery ---
    client.publish(
        _discovery_topic(prefix, "binary_sensor", f"{device_id}_battery"),
        json.dumps({
            "name": "Bateria Baixa",
            "unique_id": f"{device_id}_battery",
            "device": device,
            "state_topic": f"{topic_base}/status",
            "value_template": "{{ 'ON' if value_json.battery_low else 'OFF' }}",
            "device_class": "battery",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": f"{topic_base}/availability",
            "payload_available": "online", "payload_not_available": "offline",
        }), retain=True,
    )

    # --- Tamper ---
    client.publish(
        _discovery_topic(prefix, "binary_sensor", f"{device_id}_tamper"),
        json.dumps({
            "name": "Violação (Tamper)",
            "unique_id": f"{device_id}_tamper",
            "device": device,
            "state_topic": f"{topic_base}/status",
            "value_template": "{{ 'ON' if value_json.tamper else 'OFF' }}",
            "device_class": "tamper",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": f"{topic_base}/availability",
            "payload_available": "online", "payload_not_available": "offline",
        }), retain=True,
    )

    mqtt_logger.info(
        "Published HA discovery: alarm_control_panel, "
        f"{len(zone_list)} zones, siren, AC power, battery, tamper"
    )


class AlarmBridge:
    """Main bridge between Intelbras alarm panel and MQTT."""

    def __init__(self, config: dict):
        self.config = config
        self.alarm_cfg = config["alarm"]
        self.mqtt_cfg = config["mqtt"]
        self.zones_cfg = config.get("zones", {}) or {}
        # Zones that should be bypassed before arming (e.g., a panic button
        # that's always "open" but shouldn't prevent the alarm from arming)
        self.always_bypass_zones = config.get("always_bypass_zones", []) or []
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
        # High threshold because the ANM 24 NET is slow to respond right
        # after arm/disarm commands. Don't auto-reconnect on a few failures.
        self._max_consecutive_failures = 20
        # Lock to serialize access to the alarm (verify + poll can't both
        # use the same socket at the same time)
        self._alarm_lock = threading.Lock()

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
        # Bypass zones that should be ignored (e.g., panic button always open)
        if self.always_bypass_zones:
            cloud_logger.info(
                f"Bypassing zones {self.always_bypass_zones} before ARM"
            )
            with self._alarm_lock:
                self._alarm.bypass_zones(self.always_bypass_zones, bypass=True)
            # Give the panel time to process bypass
            time.sleep(2)
        else:
            # AUTO-BYPASS: check current status, and if any zones are open,
            # try bypassing them automatically so arm doesn't fail.
            cloud_logger.info(
                "No always_bypass_zones configured - checking current "
                "zones and auto-bypassing any open ones"
            )
            try:
                with self._alarm_lock:
                    cur_status = self._alarm.get_status(24)
                if cur_status.zones_open:
                    cloud_logger.info(
                        f"Auto-bypassing currently open zones: "
                        f"{cur_status.zones_open}"
                    )
                    with self._alarm_lock:
                        self._alarm.bypass_zones(
                            cur_status.zones_open, bypass=True
                        )
                    time.sleep(2)
            except Exception as e:
                cloud_logger.warning(f"Auto-bypass check failed: {e}")
        cloud_logger.info("Sending ARM AWAY (full) command to alarm...")
        with self._alarm_lock:
            result = self._alarm.arm()
        # Verify with actual status (panel may queue command but not execute)
        result = self._verify_arm_state(result, expected_armed=True)
        cloud_logger.info(f"Arm result: {'OK' if result else 'FAILED'}")
        return result

    def arm_stay(self) -> bool:
        if not self._alarm:
            cloud_logger.error("Cannot arm: not connected to alarm")
            return False
        if self.always_bypass_zones:
            cloud_logger.info(
                f"Bypassing zones {self.always_bypass_zones} before ARM STAY"
            )
            with self._alarm_lock:
                self._alarm.bypass_zones(self.always_bypass_zones, bypass=True)
            time.sleep(2)
        else:
            # AUTO-BYPASS (same as arm)
            cloud_logger.info(
                "No always_bypass_zones configured - checking current "
                "zones and auto-bypassing any open ones"
            )
            try:
                with self._alarm_lock:
                    cur_status = self._alarm.get_status(24)
                if cur_status.zones_open:
                    cloud_logger.info(
                        f"Auto-bypassing currently open zones: "
                        f"{cur_status.zones_open}"
                    )
                    with self._alarm_lock:
                        self._alarm.bypass_zones(
                            cur_status.zones_open, bypass=True
                        )
                    time.sleep(2)
            except Exception as e:
                cloud_logger.warning(f"Auto-bypass check failed: {e}")
        cloud_logger.info("Sending ARM STAY (partial) command to alarm...")
        with self._alarm_lock:
            result = self._alarm.arm_stay()
        result = self._verify_arm_state(result, expected_armed=True)
        cloud_logger.info(f"Arm-stay result: {'OK' if result else 'FAILED'}")
        return result

    def panic(self) -> bool:
        if not self._alarm:
            cloud_logger.error("Cannot panic: not connected to alarm")
            return False
        cloud_logger.info("Sending PANIC command to alarm...")
        with self._alarm_lock:
            result = self._alarm.panic()
        cloud_logger.info(f"Panic result: {'OK' if result else 'FAILED'}")
        return result

    def disarm(self) -> bool:
        if not self._alarm:
            cloud_logger.error("Cannot disarm: not connected to alarm")
            return False
        cloud_logger.info("Sending DISARM command to alarm...")
        with self._alarm_lock:
            result = self._alarm.disarm()
        result = self._verify_arm_state(result, expected_armed=False)
        # Remove the bypass we set when arming, so zones return to normal
        if result and self.always_bypass_zones:
            cloud_logger.info(
                f"Removing bypass for zones {self.always_bypass_zones}"
            )
            with self._alarm_lock:
                self._alarm.bypass_zones(self.always_bypass_zones, bypass=False)
            time.sleep(2)
        cloud_logger.info(f"Disarm result: {'OK' if result else 'FAILED'}")
        return result

    def _verify_arm_state(self, reported: bool, expected_armed: bool) -> bool:
        """Verify the arm/disarm actually took effect by polling status.

        The ANM 24 NET is SLOW to process arm/disarm commands. After sending
        the command, it can take up to 10+ seconds for the status to actually
        change. We poll multiple times with delays before giving up.
        """
        if not reported or not self._alarm:
            return reported

        expected_str = "armed" if expected_armed else "disarmed"

        # ANM 24 NET can take 10+ seconds to process arm/disarm.
        # Poll multiple times with increasing delays.
        # IMPORTANT: do NOT trigger reconnect on verify poll failures -
        # the panel is just busy and will eventually respond.
        wait_times = [2, 5, 10]
        for wait in wait_times:
            time.sleep(wait)
            try:
                with self._alarm_lock:
                    status = self._alarm.get_status(24)
            except Exception as e:
                cloud_logger.debug(f"Status verify poll error (will retry): {e}")
                continue

            actually_armed = status.armed
            if actually_armed == expected_armed:
                cloud_logger.info(
                    f"Status verify: panel is {status.arm_mode} ✓ "
                    f"(matched expected after {wait}s)"
                )
                return True
            cloud_logger.debug(
                f"Status verify: panel still {status.arm_mode} "
                f"after {wait}s, waiting more..."
            )

        # Final check - report failure with full context
        try:
            with self._alarm_lock:
                status = self._alarm.get_status(24)
            cloud_logger.warning(
                f"Status verify FAILED after 17s: panel reports {status.arm_mode} "
                f"but we expected {expected_str}. "
                f"zones_open={status.zones_open}, "
                f"siren={status.siren_triggered}, "
                f"ac_loss={status.ac_power_loss}"
            )
        except Exception:
            cloud_logger.warning(
                f"Status verify FAILED: could not reach panel to confirm"
            )
        return False

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
            # Publish HA discovery payloads so HA auto-creates entities
            try:
                publish_discovery(client, self.config)
            except Exception as e:
                mqtt_logger.error(f"HA discovery publish failed: {e}", exc_info=logger.isEnabledFor(logging.DEBUG))
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
        payload_upper = payload.upper().strip()

        # Alarm control commands
        if topic == f"{base}/command":
            # Payload-based dispatch (single command topic for HA alarm_control_panel)
            if payload_upper == "ARM_AWAY":
                cloud_logger.info("ARM_AWAY requested - activating alarm (full)")
                self.arm()
            elif payload_upper == "ARM_HOME":
                cloud_logger.info("ARM_HOME requested - activating alarm (partial/stay)")
                self.arm_stay()
            elif payload_upper == "ARM_NIGHT":
                # ANM 24 NET doesn't have night mode - fall back to partial
                mqtt_logger.info("ARM_NIGHT not supported on ANM 24 NET, using ARM_HOME (partial)")
                self.arm_stay()
            elif payload_upper in ("DISARM", "DISARMED"):
                self.disarm()
            elif payload_upper == "PANIC":
                cloud_logger.info("PANIC requested - sending panic command")
                self.panic()
            else:
                mqtt_logger.warning(
                    f"Unknown alarm command: {payload!r}. "
                    f"Supported: ARM_AWAY, ARM_HOME, DISARM, PANIC"
                )
        # Siren control
        elif topic == f"{base}/siren/control":
            if payload_upper == "OFF":
                self.siren_off()
            else:
                mqtt_logger.warning(
                    f"Siren ON not directly supported by ANM 24 NET. "
                    f"Use PANIC command or app."
                )
        else:
            mqtt_logger.debug(f"Unhandled topic: {topic}")

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
            with self._alarm_lock:
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
