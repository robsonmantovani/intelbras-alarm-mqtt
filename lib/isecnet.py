"""
ISECNet Cloud Relay client for Intelbras alarm panels.
Connects via Intelbras cloud servers (amt.intelbras.com.br:9015 for V1)
to communicate with alarm panels that don't accept direct TCP connections.

Protocol flow (V1 Cloud):
1. GET_BYTE: [0x01, 0xFB, checksum] -> server returns byte_value
2. CONNECT: V1 packet with client_id + MAC, XOR-encrypted with byte_value
3. After CONNECT success, use ISECNet V1 commands (0xE9 frames) directly
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field

logger = logging.getLogger("isecnet")

# Cloud relay servers
AMT_SERVER_V1 = "amt.intelbras.com.br"
AMT_PORT_V1 = 9015

# Protocol constants
ISEC_PROGRAM = 0xE9
FRAME_DELIMITER = 0x21

# V1 Server commands
GET_BYTE = 0xFB
CONNECT = 0xE5
SERVER_TYPE_GUARDIAN = 5
CONNECTION_TYPE_ETHERNET = 0x45

# V1 Status commands
CMD_PARTIAL_STATUS = [0x5A]   # ANM 24 NET, AMT 2018 (46 bytes)
CMD_EXTENDED_STATUS = [0x5B]  # AMT 4010 Smart (~96 bytes)
CMD_SMART_STATUS = [0x5D]     # AMT 2018 E Smart (135+ bytes)

# V1 Action commands
# IMPORTANT: The ANM 24 NET requires the partition byte after the command!
# Single-byte commands (just 0x41 or 0x44) return 0xE7 (DEACTIVATION_DENIED).
#
# Mapping for the typical ANM 24 NET setup (2 partitions):
# - Partition A: full/away arming (all zones active, used when nobody is home)
# - Partition B: stay/partial arming (interior zones bypassed, used at night)
#
# So ARM_AWAY uses partition A and ARM_HOME uses partition B.
# DISARM must disarm BOTH partitions to fully deactivate.
CMD_ACTIVATE = [0x41, 0x41]        # 'A' + 'A' - arm partition A (total/away)
CMD_ACTIVATE_PART_B = [0x41, 0x42]  # 'A' + 'B' - arm partition B (stay/home)
CMD_DEACTIVATE = [0x44, 0x41]      # 'D' + 'A' - disarm partition A
CMD_DEACTIVATE_PART_B = [0x44, 0x42]  # 'D' + 'B' - disarm partition B

# Panic commands (per APK source):
#   0 = Silent panic (no siren, sends monitoring alert)
#   1 = Audible panic (activates siren)  <-- this is the "Emergency" button
#   2 = Fire panic (AMT 8000 only)
#   3 = Medical emergency (AMT 8000 only)
# V1 syntax: [0x45, type]
CMD_PANIC_SILENT = [0x45, 0]       # silent panic
CMD_PANIC_AUDIBLE = [0x45, 1]      # audible panic (triggers siren) - "Emergency"
CMD_PANIC_FIRE = [0x45, 2]         # fire panic (AMT 8000 only)
CMD_PANIC_MEDICAL = [0x45, 3]      # medical emergency (AMT 8000 only)

# For the bridge, we use audible panic (this is the "Emergency" button
# the user knows from the AMT Mobile V3 app)
CMD_PANIC = CMD_PANIC_AUDIBLE

# NOTE: The ANM 24 NET V1 protocol does NOT expose a siren-off command.
# 0x4F returns 0xE2 INVALID_COMMAND on this panel. To stop the siren,
# use DISARM (the app uses the same approach).
CMD_BYPASS = 0x42                  # 'B' - bypass zones (followed by bitmask)

# Model names
MODEL_NAMES = {
    24: "AMT 2018",
    36: "ANM 24 NET",
    37: "ANM 24 NET G2",
    52: "AMT 2018 E SMART",
    54: "AMT 1000 SMART",
    65: "AMT 4010 SMART",
}


@dataclass
class AlarmStatus:
    connected: bool = False
    model_code: int = 0
    model_name: str = ""
    armed: bool = False
    arm_mode: str = "disarmed"
    is_partitioned: bool = False
    partition_a: bool = False
    partition_b: bool = False
    partition_c: bool = False
    partition_d: bool = False
    zones_open: list[int] = field(default_factory=list)
    zones_violated: list[int] = field(default_factory=list)
    zones_bypassed: list[int] = field(default_factory=list)
    total_zones: int = 24
    siren_triggered: bool = False
    alarm_triggered: bool = False
    ac_power_loss: bool = False
    battery_low: bool = False
    firmware_version: str = ""
    firmware_version_number: int = 0
    date_time: str = ""
    raw_response: list[int] | None = None


def _checksum(data: list[int]) -> int:
    """XOR all bytes, then XOR with 0xFF."""
    r = 0
    for b in data:
        r ^= b
    return r ^ 0xFF


def _int_to_bits(n: int) -> str:
    return format(n, "08b")


def _build_v1_frame(password: str, command: list[int]) -> bytes:
    """Build ISECNet V1 command frame.
    Format: [size] [0xE9] [0x21] [password_ascii] [command] [0x21] [checksum]
    """
    f = [len(password) + len(command) + 3, ISEC_PROGRAM, FRAME_DELIMITER]
    for ch in password:
        f.append(ord(ch))
    f.extend(command)
    f.append(FRAME_DELIMITER)
    f.append(_checksum(f))
    return bytes(f)


def _parse_date(data: list[int]) -> str:
    """Parse date/time from V1 partial status (46-byte) response.
    data[24..28] = minute, hour, day, month, year-2000
    """
    try:
        minute = data[24]
        hour = data[25]
        day = data[26]
        month = data[27]
        year = 2000 + data[28]
        return f"{day:02d}/{month:02d}/{year} {hour:02d}:{minute:02d}"
    except (IndexError, ValueError):
        return ""


def _parse_zone_status(data: list[int], total_zones: int = 24) -> tuple[list[int], list[int], list[int]]:
    """Parse zone status from V1 status response.

    Layout (from APK reverse-engineering):
    - data[0] = ISEC_PROGRAM (0xE9) echo
    - data[1..1+N] = zone open bitmap (1 bit per zone, LSB = zone 0)
    - data[1+N..1+2N] = zone alarm bitmap (1 bit per zone)
    - where N = ceil(total_zones / 8) bytes
    - For ANM 24 NET: N = 3 bytes = 24 zones
    - For AMT 2018: N = 6 bytes = 48 zones
    - For AMT 4010: N = 8 bytes = 64 zones
    """
    zones_open, zones_violated, zones_bypassed = [], [], []
    n_bytes = (total_zones + 7) // 8

    # Need at least n_bytes * 2 + 1 bytes for open+alarm bitmaps after the echo
    if len(data) < 1 + n_bytes * 2:
        return zones_open, zones_violated, zones_bypassed

    zone_byte_start = 1  # After ISEC_PROGRAM echo at data[0]

    for zone_idx in range(total_zones):
        byte_idx = zone_byte_start + (zone_idx // 8)
        bit_idx = zone_idx % 8
        if byte_idx >= len(data):
            break

        # Open bitmap: bit set = zone is open
        is_open = bool(data[byte_idx] & (1 << bit_idx))
        # Alarm bitmap: bit set = zone is in alarm/violated
        alarm_byte_idx = zone_byte_start + n_bytes + (zone_idx // 8)
        is_alarmed = False
        if alarm_byte_idx < len(data):
            is_alarmed = bool(data[alarm_byte_idx] & (1 << bit_idx))

        zn = zone_idx + 1
        if is_alarmed:
            zones_violated.append(zn)
        elif is_open:
            zones_open.append(zn)
        # Note: bypass state is not in the V1 status response

    return zones_open, zones_violated, zones_bypassed


def parse_v1_status(data: list[int], total_zones: int = 24) -> AlarmStatus:
    """Parse ISECNet V1 partial status response (0x5A, 46 bytes)."""
    status = AlarmStatus()
    status.raw_response = data

    # Log the raw response at DEBUG level for diagnostics
    if data:
        logger.debug(f"Raw status response: {bytes(data).hex()}")

    if len(data) < 20:
        logger.warning(
            f"Status data too short: {len(data)} bytes (expected 46). "
            f"Raw: {bytes(data).hex() if data else '(empty)'}"
        )
        # Mark as disconnected so caller doesn't publish fake "all OK" status
        status.connected = False
        return status

    status.connected = True
    status.total_zones = total_zones

    # Model code at data[19]
    try:
        status.model_code = data[19]
        status.model_name = MODEL_NAMES.get(status.model_code, f"Unknown (0x{status.model_code:02X})")
    except IndexError:
        pass

    # Firmware at data[20]
    try:
        fw = data[20]
        status.firmware_version_number = fw
        status.firmware_version = f"{fw >> 4}.{fw & 0x0F}"
    except IndexError:
        pass

    # Partition enabled at data[21]
    try:
        status.is_partitioned = (data[21] == 1)
    except IndexError:
        pass

    # Partition armed bits at data[22]
    # Bit 0 (LSB) = partition A armed
    # Bit 1     = partition B armed
    # Bit 2     = partition C armed
    # Bit 3     = partition D armed
    # Bit 4     = stay mode (armed_home/armed_stay)
    # Bit 7     = AC power loss (1 = lost)
    try:
        p = data[22]
        status.partition_a = bool(p & 0x01)
        status.partition_b = bool(p & 0x02)
        status.partition_c = bool(p & 0x04)
        status.partition_d = bool(p & 0x08)
        status.ac_power_loss = bool(p & 0x80)

        status.armed = any([status.partition_a, status.partition_b,
                           status.partition_c, status.partition_d])
        if status.armed:
            # Logic for the typical 2-partition ANM 24 NET setup:
            # - Partition A = full/away (all zones, including perimeter)
            # - Partition B = stay/home (interior only)
            # If only B is armed -> home mode (you're inside, perimeter active)
            # If A is armed (with or without B) -> away mode (nobody home)
            if status.partition_a:
                status.arm_mode = "armed_away"
            else:
                status.arm_mode = "armed_home"
        else:
            status.arm_mode = "disarmed"
    except IndexError:
        pass

    # Date/time
    status.date_time = _parse_date(data)

    # Battery at data[31]
    try:
        status.battery_low = (data[31] < 20)
    except IndexError:
        pass

    # AC power already read from data[22] bit 7 in the partition section above

    # Output/alarm byte at data[38] - now parsed AFTER zones (below)
    # to allow cross-checking with zone alarm data (more reliable than
    # data[38] alone which has false positives during arm/disarm).
    # For ANM 24 NET, ANM 24 NET does NOT have a cabinet tamper sensor
    # exposed in the V1 Cloud Relay protocol response.

    # Zones
    zo, zv, zb = _parse_zone_status(data, total_zones)
    status.zones_open = zo
    status.zones_violated = zv
    status.zones_bypassed = zb

    # Now that we have zone alarm data, re-evaluate alarm_triggered and
    # siren_triggered. The APK source says data[38] bit 2 has false
    # positives during arm/disarm transitions, so we cross-check with
    # actual zone alarm data.
    try:
        output_byte = data[38]
        siren_bit7 = bool(output_byte & 0x80)
        alarm_bit2 = bool(output_byte & 0x04)

        # If any zone has alarmed, that's the real trigger
        has_zone_alarm = len(zv) > 0

        if has_zone_alarm or siren_bit7:
            # Zone alarm is the reliable signal
            status.alarm_triggered = True
        elif alarm_bit2:
            # Only data[38] bit 2 set (no zone alarm) - probably a
            # transient during arm/disarm. Trust it for now.
            status.alarm_triggered = True
        else:
            status.alarm_triggered = False

        # Siren triggered is reliable: bit 7 of data[38]
        status.siren_triggered = siren_bit7
    except IndexError:
        pass

    return status


class CloudRelayClient:
    """Manages a persistent connection to Intelbras cloud relay."""

    def __init__(self, mac: str, password: str, server: str = AMT_SERVER_V1,
                 port: int = AMT_PORT_V1, timeout: float = 10.0):
        self.mac = mac.replace(":", "").replace("-", "").upper()
        self.password = password
        self.server = server
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._byte_value: int = 0
        self._connected = False

    def connect(self) -> bool:
        """Establish cloud relay connection."""
        try:
            ip = socket.gethostbyname(self.server)
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect((ip, self.port))
            logger.info(f"Connected to {self.server} ({ip}:{self.port})")

            # Step 1: GET_BYTE
            gb = bytes([0x01, GET_BYTE, _checksum([0x01, GET_BYTE])])
            self._sock.send(gb)
            r = self._recv(3)
            if len(r) < 2:
                logger.error("GET_BYTE: no response")
                return False
            self._byte_value = r[1]
            logger.debug(f"Byte value: 0x{self._byte_value:02X}")

            # Step 2: CONNECT V1
            client_id = "0000000000000001"
            cidb = [int(client_id[i:i+2], 16) for i in range(0, 16, 2)]
            macb = [int(self.mac[i:i+2], 16) for i in range(0, 12, 2)]
            conn = [18, CONNECT, SERVER_TYPE_GUARDIAN] + cidb + macb + [0, CONNECTION_TYPE_ETHERNET]
            conn.append(_checksum(conn))
            encrypted = bytes([b ^ self._byte_value for b in conn])
            self._sock.send(encrypted)
            time.sleep(0.3)
            r = self._recv(5)
            if len(r) == 0:
                logger.error("CONNECT: no response")
                return False

            # Check response: 0xE6, 0xFE (254), 0x45 (69), 0x47 (71) = success
            if r[0] in (0xE6, 0xFE, 0x45, 0x47):
                logger.info(f"CONNECT success (code 0x{r[0]:02X})")
                self._connected = True
                return True
            else:
                logger.error(f"CONNECT failed: code 0x{r[0]:02X}")
                return False

        except Exception as e:
            logger.error(f"Cloud relay connection failed: {e}")
            self.disconnect()
            return False

    def disconnect(self):
        """Close connection."""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected = False

    def _recv(self, timeout: float = 3.0) -> bytes:
        """Read available data from socket."""
        if not self._sock:
            return b""
        self._sock.settimeout(timeout)
        data = b""
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except TimeoutError:
            pass
        return data

    def _send_v1_command(self, command: list[int], recv_timeout: float = 5.0) -> list[int] | None:
        """Send ISECNet V1 command and return response payload.

        Response framing: the response MAY have a 1-byte size prefix
        (value 0x01-0x7F), or it may be the raw payload directly. We detect
        and strip the size prefix if present, leaving the parser to index
        the model code at data[19] etc.
        """
        if not self._connected or not self._sock:
            logger.error("Not connected")
            return None

        frame = _build_v1_frame(self.password, command)
        logger.debug(f"Sending V1: {frame.hex()}")
        try:
            self._sock.send(frame)
            time.sleep(0.3)
            raw = self._recv(recv_timeout)
            if not raw:
                logger.warning("No response to V1 command")
                return None

            logger.debug(f"V1 response ({len(raw)}): {raw.hex()}")

            # Strip the size prefix if present. A size byte is a small value
            # (typically 0x20-0x40 for 46-byte responses) that wouldn't
            # otherwise appear as the first byte of a status payload
            # (which always starts with 0xE9 for ISEC responses).
            if len(raw) > 1 and raw[0] != 0xE9 and raw[0] < 0x80:
                logger.debug(f"Stripping size prefix byte 0x{raw[0]:02X}")
                return list(raw[1:])
            return list(raw)
        except OSError as e:
            logger.error(f"Socket error: {e}")
            self._connected = False
            return None

    def get_status(self, total_zones: int = 24) -> AlarmStatus:
        """Request and parse alarm status."""
        data = self._send_v1_command(CMD_PARTIAL_STATUS)
        if data is None:
            return AlarmStatus(connected=False)
        return parse_v1_status(data, total_zones)

    def arm(self) -> bool:
        """Arm the alarm in TOTAL/AWAY mode (all zones active)."""
        data = self._send_v1_command(CMD_ACTIVATE, recv_timeout=5.0)
        if data is None:
            return False
        return self._parse_action_response(data, "Arm (AWAY)")

    def arm_stay(self) -> bool:
        """Arm the alarm in partial/stay mode using partition B.

        On the ANM 24 NET, partition B is typically configured for
        stay/home arming (interior zones bypassed, perimeter active).
        This matches the Home Assistant 'arm_home' action.
        """
        data = self._send_v1_command(CMD_ACTIVATE_PART_B, recv_timeout=5.0)
        if data is None:
            return False
        return self._parse_action_response(data, "Arm (STAY/B)")

    def panic(self) -> bool:
        """Trigger a panic alarm (siren + alarm)."""
        data = self._send_v1_command(CMD_PANIC, recv_timeout=5.0)
        if data is None:
            return False
        return self._parse_action_response(data, "Panic")

    def bypass_zones(self, zone_indices: list[int], bypass: bool = True, total_zones: int = 24) -> bool:
        """Bypass (or unbypass) zones using V1 bypass command.

        Args:
            zone_indices: List of 1-based zone numbers to bypass (e.g., [1, 6])
            bypass: True to bypass, False to unbypass
            total_zones: Total number of zones (default 24 for ANM 24 NET)

        Returns:
            True if the bypass command was accepted.
        """
        # Build bitmask - bit n = zone (n+1), so zone 1 = bit 0
        n_bytes = (total_zones + 7) // 8
        bitmask = [0x00] * n_bytes
        for zone in zone_indices:
            zero_based = zone - 1
            if 0 <= zero_based < total_zones:
                byte_idx = zero_based // 8
                bit_idx = zero_based % 8
                if bypass:
                    bitmask[byte_idx] |= (1 << bit_idx)
                # Note: for unbypass, we don't need to do anything because
                # the bitmask is "full state" — zones not in zone_indices
                # are unbypassed. But we want to PRESERVE existing bypasses
                # of other zones, which we don't know here.
                # For the "always bypass" use case, only this command is sent.

        command = [CMD_BYPASS] + bitmask
        data = self._send_v1_command(command, recv_timeout=5.0)
        if data is None:
            return False
        action = f"Bypass zones {zone_indices} ({'on' if bypass else 'off'})"
        return self._parse_action_response(data, action)

    def disarm(self) -> bool:
        """Disarm the alarm - both partitions A and B.

        On the ANM 24 NET with 2 partitions, you must send two disarm
        commands (one per partition) to fully deactivate the alarm.
        """
        # Disarm partition A
        data_a = self._send_v1_command(CMD_DEACTIVATE, recv_timeout=5.0)
        a_ok = data_a is not None and self._parse_action_response(data_a, "Disarm (A)")

        # Disarm partition B
        data_b = self._send_v1_command(CMD_DEACTIVATE_PART_B, recv_timeout=5.0)
        b_ok = data_b is not None and self._parse_action_response(data_b, "Disarm (B)")

        return a_ok or b_ok

    def siren_off(self) -> bool:
        """Turn off siren - NOT supported on ANM 24 NET V1 protocol.

        The 0x4F command returns 0xE2 INVALID_COMMAND on this panel.
        To stop the siren, use disarm() (the AMT Mobile app does the
        same thing).
        """
        return False  # not supported

    def _parse_action_response(self, data: list[int], action: str) -> bool:
        """Parse the response of an action command (arm/disarm/panic/siren)."""
        if len(data) < 2:
            logger.warning(f"{action}: short response ({len(data)} bytes): {bytes(data).hex()}")
            return False
        return parse_action_code(data[1], action)


def parse_action_code(code: int, action: str = "Action") -> bool:
    """Parse an action response code from the ANM 24 NET panel.

    Response codes (from APK ISECNetResponse enum):
        0x00 = SUCCESS (in status response, byte[2])
        0xFE = SUCCESS (in short action response)
        0xE0 = INVALID_PACKAGE
        0xE1 = INCORRECT_PASSWORD
        0xE2 = INVALID_COMMAND
        0xE3 = NO_PARTITIONS
        0xE4 = OPEN_ZONES
        0xE5 = COMMAND_DEPRECATED
        0xE6 = BYPASS_DENIED
        0xE7 = DEACTIVATION_DENIED / command-queued (treat as soft success)
        0xE8 = BYPASS_CENTRAL_ACTIVATED
        0xFF = INVALID_MODEL

    IMPORTANT: The ANM 24 NET panel returns 0xE7 for arm/disarm followed
    by a status response (46 bytes). Per APK source:
    "No response to ARM is common - panel may be processing exit delay.
     This is NOT necessarily a failure - we'll verify with status check."

    So we treat 0xE7 as a soft success and let the next status poll
    confirm whether the action actually took effect.
    """
    code_names = {
        0x00: "SUCCESS",
        0xFE: "SUCCESS",
        0xE0: "INVALID_PACKAGE",
        0xE1: "INCORRECT_PASSWORD",
        0xE2: "INVALID_COMMAND",
        0xE3: "NO_PARTITIONS",
        0xE4: "OPEN_ZONES",
        0xE5: "COMMAND_DEPRECATED",
        0xE6: "BYPASS_DENIED",
        0xE7: "DEACTIVATION_DENIED",
        0xE8: "BYPASS_CENTRAL_ACTIVATED",
        0xFF: "INVALID_MODEL",
    }
    if code in (0x00, 0xFE):
        logger.info(f"{action}: OK (code 0x{code:02X})")
        return True
    elif code == 0xE7:
        # ANM 24 NET quirk: arm/disarm return 0xE7 then send status.
        # Treat as soft success - the next status poll will confirm.
        logger.info(
            f"{action}: command queued (0xE7), checking status to confirm"
        )
        return True
    else:
        name = code_names.get(code, f"UNKNOWN(0x{code:02X})")
        logger.warning(f"{action}: FAILED — {name} (code 0x{code:02X})")
        return False

    @property
    def is_connected(self) -> bool:
        return self._connected
