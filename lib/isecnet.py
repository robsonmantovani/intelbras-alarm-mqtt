"""
ISECNet Cloud Relay client for Intelbras alarm panels.
Connects via Intelbras cloud servers (amt.intelbras.com.br:9015 for V1)
to communicate with alarm panels that don't accept direct TCP connections.

Protocol flow (V1 Cloud):
1. GET_BYTE: [0x01, 0xFB, checksum] -> server returns byte_value
2. CONNECT: V1 packet with client_id + MAC, XOR-encrypted with byte_value
3. After CONNECT success, use ISECNet V1 commands (0xE9 frames) directly
"""

import socket
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

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
CMD_ACTIVATE = [0x41]           # 'A' - arm total
CMD_ACTIVATE_STAY = [0x41, 0x50]  # 'A' + 'P' - arm partial/stay
CMD_ACTIVATE_PART_A = [0x41, 0x41]  # 'A' + 'A' - arm partition A
CMD_ACTIVATE_PART_B = [0x41, 0x42]  # 'A' + 'B' - arm partition B
CMD_DEACTIVATE = [0x44]         # 'D' - disarm
CMD_SIREN_OFF = [0x4F]          # 'O' - turn off siren
CMD_PANIC = [0x45]              # 'E' - panic alarm (triggers siren)
CMD_BYPASS = 0x42               # 'B' - bypass zones (followed by bitmask)

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
    ac_power_loss: bool = False
    battery_low: bool = False
    tamper: bool = False
    firmware_version: str = ""
    firmware_version_number: int = 0
    date_time: str = ""
    raw_response: Optional[list[int]] = None


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
    try:
        p = data[22]
        status.partition_a = bool(p & 0x01)
        status.partition_b = bool(p & 0x02)
        status.partition_c = bool(p & 0x04)
        status.partition_d = bool(p & 0x08)
        status.armed = any([status.partition_a, status.partition_b,
                           status.partition_c, status.partition_d])
        if status.armed:
            all_armed = all([status.partition_a, status.partition_b,
                            status.partition_c, status.partition_d])
            # If any partition is in stay mode, treat as armed_home
            # Otherwise armed_away (total)
            is_stay = bool(p & 0x10)
            status.arm_mode = "armed_home" if is_stay else "armed_away"
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

    # AC power from data[22] bit 7 (MSB)
    # 0 = AC power OK, 1 = AC power lost
    try:
        status.ac_power_loss = bool(data[22] & 0x80)
    except IndexError:
        pass

    # Siren/output at data[38]
    try:
        bits38 = _int_to_bits(data[38])
        status.siren_triggered = bool(int(bits38[4]))
        status.tamper = bool(int(bits38[3]))
    except IndexError:
        pass

    # Zones
    zo, zv, zb = _parse_zone_status(data, total_zones)
    status.zones_open = zo
    status.zones_violated = zv
    status.zones_bypassed = zb

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
        self._sock: Optional[socket.socket] = None
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
        except socket.timeout:
            pass
        return data

    def _send_v1_command(self, command: list[int], recv_timeout: float = 5.0) -> Optional[list[int]]:
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
        """Arm the alarm in PARTIAL/STAY mode (interior zones bypassed)."""
        data = self._send_v1_command(CMD_ACTIVATE_STAY, recv_timeout=5.0)
        if data is None:
            return False
        return self._parse_action_response(data, "Arm (STAY)")

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
        """Disarm the alarm."""
        data = self._send_v1_command(CMD_DEACTIVATE, recv_timeout=5.0)
        if data is None:
            return False
        return self._parse_action_response(data, "Disarm")

    def siren_off(self) -> bool:
        """Turn off siren."""
        data = self._send_v1_command(CMD_SIREN_OFF, recv_timeout=5.0)
        if data is None:
            return False
        return self._parse_action_response(data, "Siren off")

    def _parse_action_response(self, data: list[int], action: str) -> bool:
        """Parse the response of an action command (arm/disarm/panic/siren).

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
        if len(data) < 2:
            logger.warning(f"{action}: short response ({len(data)} bytes): {bytes(data).hex()}")
            return False

        code = data[1]
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
            0xE7: "COMMAND_QUEUED",
            0xE8: "BYPASS_CENTRAL_ACTIVATED",
            0xFF: "INVALID_MODEL",
        }
        code_name = code_names.get(code, f"UNKNOWN(0x{code:02X})")

        if code in (0x00, 0xFE, 0xE7):
            # 0xE7 (DEACTIVATION_DENIED) is treated as a soft success
            # because the ANM 24 NET panel can return this when the
            # action actually works but the panel is processing other
            # commands. The next status poll will confirm.
            label = "OK" if code in (0x00, 0xFE) else "queued"
            logger.info(f"{action}: {label} (code 0x{code:02X})")
            return True
        else:
            logger.warning(f"{action}: FAILED — {code_name} (code 0x{code:02X})")
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected
