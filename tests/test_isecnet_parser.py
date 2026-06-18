"""Unit tests for lib.isecnet parsers (no network)."""
import sys
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.isecnet import (
    _checksum, _int_to_bits, _build_v1_frame, _parse_zone_status,
    parse_v1_status, parse_action_code,
    CMD_PANIC_AUDIBLE, CMD_ACTIVATE, CMD_DEACTIVATE,
)


class TestChecksum(unittest.TestCase):
    def test_checksum_xor_with_0xFF(self):
        # _checksum is XOR of all bytes then XOR with 0xFF
        # [0x01, 0x02, 0x03] XOR = 0x00, then 0x00 ^ 0xFF = 0xFF
        self.assertEqual(_checksum([0x01, 0x02, 0x03]), 0xFF)

    def test_checksum_password(self):
        # "1234" = [0x31, 0x32, 0x33, 0x34], XOR = 0x04, then 0x04 ^ 0xFF = 0xFB
        self.assertEqual(_checksum([0x31, 0x32, 0x33, 0x34]), 0xFB)

    def test_checksum_empty(self):
        # Empty list XOR = 0, then 0 ^ 0xFF = 0xFF
        self.assertEqual(_checksum([]), 0xFF)


class TestIntToBits(unittest.TestCase):
    def test_int_to_bits_8bit(self):
        # 0x03 = 00000011
        self.assertEqual(_int_to_bits(0x03), "00000011")

    def test_int_to_bits_zero(self):
        self.assertEqual(_int_to_bits(0), "00000000")

    def test_int_to_bits_full(self):
        self.assertEqual(_int_to_bits(0xFF), "11111111")


class TestBuildV1Frame(unittest.TestCase):
    def test_frame_audible_panic(self):
        # Audible panic: [0x45, 0x01] with password "3664"
        frame = _build_v1_frame("3664", CMD_PANIC_AUDIBLE)
        # Frame format: [size, 0xE9, 0x21, password..., cmd..., 0x21, checksum]
        # size = len(password) + len(cmd) + 3 = 4 + 2 + 3 = 9
        self.assertEqual(frame[0], 9)  # size byte
        self.assertEqual(frame[1], 0xE9)  # ISEC_PROGRAM
        self.assertEqual(frame[2], 0x21)  # FRAME_DELIMITER
        # Password as ASCII
        self.assertEqual(frame[3:7], b"3664")
        # Command bytes
        self.assertEqual(frame[7], 0x45)
        self.assertEqual(frame[8], 0x01)
        # Frame delimiter before checksum
        self.assertEqual(frame[9], 0x21)
        # Last byte is checksum (XOR of ALL previous bytes then ^ 0xFF)
        xor = 0xFF
        for b in frame[:-1]:
            xor ^= b
        self.assertEqual(frame[-1], xor)

    def test_frame_arm(self):
        # CMD_ACTIVATE = [0x41, 0x41] (arm partition A)
        frame = _build_v1_frame("1234", CMD_ACTIVATE)
        self.assertEqual(frame[1], 0xE9)
        # Password ASCII
        self.assertEqual(frame[3:7], b"1234")
        # Command bytes
        self.assertEqual(frame[7], 0x41)
        self.assertEqual(frame[8], 0x41)
        # size = 4 + 2 + 3 = 9
        self.assertEqual(frame[0], 9)


class TestParseZoneStatus(unittest.TestCase):
    def test_no_zones_open(self):
        # data[1..3] = 0x00, 0x00, 0x00 (no zones open)
        data = [0xE9, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        zo, zv, zb = _parse_zone_status(data, 24)
        self.assertEqual(zo, [])
        self.assertEqual(zv, [])
        self.assertEqual(zb, [])

    def test_zone_1_open(self):
        # data[1] = 0x01 (zone 1 open, bit 0)
        data = [0xE9, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00]
        zo, zv, zb = _parse_zone_status(data, 24)
        self.assertEqual(zo, [1])
        self.assertEqual(zv, [])

    def test_zones_1_and_6_open(self):
        # data[1] = 0x21 = 00100001 = bits 0 and 5 = zones 1 and 6
        data = [0xE9, 0x21, 0x00, 0x00, 0x00, 0x00, 0x00]
        zo, _, _ = _parse_zone_status(data, 24)
        self.assertEqual(sorted(zo), [1, 6])

    def test_zones_1_6_11_12_open(self):
        # data[1] = 0x21 = 00100001 = bits 0 and 5 = zones 1 and 6
        # data[2] = 0x0C = 00001100 = bits 2 and 3 = zones 11 and 12
        data = [0xE9, 0x21, 0x0C, 0x00, 0x00, 0x00, 0x00]
        zo, _, _ = _parse_zone_status(data, 24)
        self.assertEqual(sorted(zo), [1, 6, 11, 12])

    def test_zone_violated_takes_priority_over_open(self):
        # Zone 1 is BOTH open (data[1]=0x01) and alarmed (data[4]=0x01)
        data = [0xE9, 0x01, 0x00, 0x00, 0x01, 0x00, 0x00]
        zo, zv, _ = _parse_zone_status(data, 24)
        # When alarmed, zone goes to violated (not open)
        self.assertEqual(zo, [])
        self.assertEqual(zv, [1])


class TestParseV1Status(unittest.TestCase):
    def _build_status(self, partitions=0x00, zones_open_byte1=0x00,
                      zones_open_byte2=0x00, zones_open_byte3=0x00,
                      output_byte=0x00, battery=0xFF,
                      model_code=0x24, firmware=0x66, is_partitioned=True):
        """Build a minimal 46-byte V1 status payload."""
        data = [0xE9]  # command echo
        data += [zones_open_byte1, zones_open_byte2, zones_open_byte3]  # zones open
        data += [0x00, 0x00, 0x00]  # zone alarm (clear)
        # Pad to data[19] (model code)
        while len(data) < 19:
            data.append(0x00)
        data.append(model_code)  # data[19] = model code
        data.append(firmware)  # data[20] = firmware
        data.append(1 if is_partitioned else 0)  # data[21] = partitioned
        data.append(partitions)  # data[22] = partition bits
        # Pad to data[31] (battery)
        while len(data) < 31:
            data.append(0x00)
        data.append(battery)  # data[31] = battery level
        # Pad to data[38] (output)
        while len(data) < 38:
            data.append(0x00)
        data.append(output_byte)  # data[38] = output/siren
        # Pad to 46 bytes
        while len(data) < 46:
            data.append(0x00)
        return data

    def test_disarmed(self):
        data = self._build_status(partitions=0x00, is_partitioned=True)
        status = parse_v1_status(data, 24)
        self.assertFalse(status.armed)
        self.assertEqual(status.arm_mode, "disarmed")
        self.assertEqual(status.zones_open, [])

    def test_armed_away_partition_a(self):
        # Partition A armed (bit 0)
        data = self._build_status(partitions=0x01, is_partitioned=True)
        status = parse_v1_status(data, 24)
        self.assertTrue(status.armed)
        self.assertEqual(status.arm_mode, "armed_away")
        self.assertTrue(status.partition_a)
        self.assertFalse(status.partition_b)

    def test_armed_home_partition_b_only(self):
        # Only partition B armed (bit 1)
        data = self._build_status(partitions=0x02, is_partitioned=True)
        status = parse_v1_status(data, 24)
        self.assertTrue(status.armed)
        self.assertEqual(status.arm_mode, "armed_home")
        self.assertFalse(status.partition_a)
        self.assertTrue(status.partition_b)

    def test_armed_away_both_partitions(self):
        # A + B (full away)
        data = self._build_status(partitions=0x03, is_partitioned=True)
        status = parse_v1_status(data, 24)
        self.assertTrue(status.armed)
        self.assertEqual(status.arm_mode, "armed_away")
        self.assertTrue(status.partition_a)
        self.assertTrue(status.partition_b)

    def test_zones_open(self):
        data = self._build_status(
            partitions=0x00, zones_open_byte1=0x21, zones_open_byte2=0x0C
        )
        status = parse_v1_status(data, 24)
        self.assertEqual(sorted(status.zones_open), [1, 6, 11, 12])

    def test_battery_low(self):
        # battery < 20 means low
        data = self._build_status(battery=0x10)
        status = parse_v1_status(data, 24)
        self.assertTrue(status.battery_low)

    def test_battery_ok(self):
        data = self._build_status(battery=0x80)
        status = parse_v1_status(data, 24)
        self.assertFalse(status.battery_low)

    def test_ac_power_loss(self):
        # data[22] bit 7 = AC power loss
        data = self._build_status(partitions=0x80)  # bit 7 + nothing
        status = parse_v1_status(data, 24)
        self.assertTrue(status.ac_power_loss)

    def test_alarm_triggered_via_zone(self):
        # Zone 1 alarmed (data[4]=0x01)
        data = self._build_status()
        data[4] = 0x01
        status = parse_v1_status(data, 24)
        self.assertTrue(status.alarm_triggered)

    def test_siren_triggered(self):
        # data[38] bit 7 = siren
        data = self._build_status(output_byte=0x80)
        status = parse_v1_status(data, 24)
        self.assertTrue(status.siren_triggered)

    def test_model_anm_24_net(self):
        data = self._build_status(model_code=0x24)
        status = parse_v1_status(data, 24)
        self.assertIn("ANM", status.model_name)

    def test_short_data_marks_disconnected(self):
        # Less than 10 bytes - not a valid status
        status = parse_v1_status([0xE9, 0x00], 24)
        self.assertFalse(status.connected)


class TestParseActionCode(unittest.TestCase):
    """Test the action response code parser directly."""

    def test_success_code_0xFE(self):
        self.assertTrue(parse_action_code(0xFE, "Test"))

    def test_success_code_0x00(self):
        self.assertTrue(parse_action_code(0x00, "Test"))

    def test_soft_success_0xE7(self):
        # 0xE7 is treated as soft success (queued)
        self.assertTrue(parse_action_code(0xE7, "Test"))

    def test_failure_invalid_password(self):
        self.assertFalse(parse_action_code(0xE1, "Test"))

    def test_failure_invalid_command(self):
        self.assertFalse(parse_action_code(0xE2, "Test"))

    def test_failure_open_zones(self):
        self.assertFalse(parse_action_code(0xE4, "Test"))

    def test_failure_no_partitions(self):
        self.assertFalse(parse_action_code(0xE3, "Test"))

    def test_failure_unknown_code(self):
        # Unknown codes (not in the table) should fail
        self.assertFalse(parse_action_code(0x99, "Test"))


if __name__ == "__main__":
    unittest.main()
