#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for daemon helpers that do not require the panel hardware."""

import errno
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subscreen_daemon as daemon  # noqa: E402
from subscreen_protocol import (  # noqa: E402
    build_packet,
    checksum,
    encode_disk_size,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def event_frame(aim: int, seq: int = 0, with_crc: bool = False) -> bytes:
    frame = build_packet(0x03, aim, b"", seq)
    return frame if with_crc else frame[:-1]


class FrameExtractionTest(unittest.TestCase):
    def test_single_six_byte_frame(self):
        frames, rest = daemon._extract_frames(event_frame(0x50))
        self.assertEqual(frames, [event_frame(0x50)])
        self.assertEqual(rest, b"")

    def test_two_coalesced_six_byte_frames(self):
        raw = event_frame(0x50, 1) + event_frame(0x81, 2)
        frames, rest = daemon._extract_frames(raw)
        self.assertEqual(frames, [event_frame(0x50, 1), event_frame(0x81, 2)])
        self.assertEqual(rest, b"")

    def test_seven_byte_frame_with_crc(self):
        raw = event_frame(0x50, 3, with_crc=True)
        frames, rest = daemon._extract_frames(raw)
        self.assertEqual(frames, [event_frame(0x50, 3)])
        # A trailing CRC byte is noise and is discarded once no magic remains.
        self.assertEqual(rest, b"")

    def test_crc_byte_before_next_frame(self):
        raw = (event_frame(0x50, 4, with_crc=True)
               + event_frame(0x71, 5, with_crc=True))
        frames, _rest = daemon._extract_frames(raw)
        self.assertEqual(frames[0], event_frame(0x50, 4))
        self.assertEqual(frames[1], event_frame(0x71, 5))

    def test_partial_frame_is_kept(self):
        raw = event_frame(0x50, 6)
        frames, rest = daemon._extract_frames(raw[:4])
        self.assertEqual(frames, [])
        self.assertEqual(rest, raw[:4])
        frames, rest = daemon._extract_frames(rest + raw[4:])
        self.assertEqual(frames, [raw])
        self.assertEqual(rest, b"")

    def test_garbage_before_magic_is_dropped(self):
        raw = b"\x00\x11\x22" + event_frame(0x50, 7)
        frames, rest = daemon._extract_frames(raw)
        self.assertEqual(frames, [event_frame(0x50, 7)])
        self.assertEqual(rest, b"")

    def test_invalid_length_is_resynchronised(self):
        bogus = b"\xA5\x5A\x00\x01\x03\x50"
        raw = bogus + event_frame(0x50, 8)
        frames, _rest = daemon._extract_frames(raw)
        self.assertEqual(frames[-1], event_frame(0x50, 8))


class ChecksumTest(unittest.TestCase):
    def test_event_frame_checksum(self):
        frame = event_frame(0x72, 9, with_crc=True)
        self.assertEqual(frame[-1], checksum(frame[:-1]))


class CtypesFreeTest(unittest.TestCase):
    """The daemon must run on hosts whose _ctypes cannot load libffi.so.7."""

    def test_import_and_map_without_ctypes(self):
        code = textwrap.dedent(
            """
            import sys

            class Blocker:
                def find_spec(self, name, path=None, target=None):
                    if name in ("ctypes", "_ctypes"):
                        raise ImportError("simulated: libffi.so.7 not found")
                    return None

            sys.meta_path.insert(0, Blocker())
            try:
                import ctypes  # noqa: F401
            except ImportError:
                pass
            else:
                raise SystemExit("ctypes was not blocked")

            import subscreen_daemon as d

            region = d.UsbIoRegion()
            try:
                assert region.base, "no buffer address"
                region.region[0:4] = b"test"
                print("OK %d %x" % (d.BULK_REQUEST_SIZE, region.base))
            finally:
                region.close()
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("OK"), result.stdout)

    def test_source_does_not_import_ctypes(self):
        path = os.path.join(ROOT, "subscreen_daemon.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("import ctypes", source)
        self.assertNotIn("_get_libc", source)


class UsbIoRegionTest(unittest.TestCase):
    def test_mapping_address_matches_ctypes(self):
        try:
            import ctypes
        except ImportError:
            self.skipTest("ctypes is unavailable on this host")
        region = daemon.UsbIoRegion()
        try:
            expected = ctypes.addressof(ctypes.c_char.from_buffer(region.region))
            self.assertEqual(region.base, expected)
        finally:
            region.close()

    def test_region_is_writable_and_sized(self):
        region = daemon.UsbIoRegion()
        try:
            self.assertGreaterEqual(len(region.region), daemon.UsbIoRegion.SIZE)
            region.region[daemon.UsbIoRegion.RX_SLOT:
                          daemon.UsbIoRegion.RX_SLOT + 3] = b"\xA5\x5A\x00"
            self.assertEqual(
                bytes(region.region[daemon.UsbIoRegion.RX_SLOT:
                                   daemon.UsbIoRegion.RX_SLOT + 3]),
                b"\xA5\x5A\x00")
        finally:
            region.close()

    def test_close_is_idempotent(self):
        region = daemon.UsbIoRegion()
        region.close()
        region.close()
        self.assertIsNone(region.base)

    def test_bulk_request_layout(self):
        try:
            import ctypes
        except ImportError:
            self.skipTest("ctypes is unavailable on this host")

        class BulkTransfer(ctypes.Structure):
            _fields_ = [("ep", ctypes.c_uint), ("len", ctypes.c_uint),
                        ("timeout", ctypes.c_uint),
                        ("data", ctypes.c_void_p)]

        self.assertEqual(daemon.BULK_REQUEST_SIZE, ctypes.sizeof(BulkTransfer))
        self.assertEqual(daemon._BULK_DATA_OFFSET, BulkTransfer.data.offset)


class BulkTransferTest(unittest.TestCase):
    def setUp(self):
        self.transport = daemon.UsbTransport()
        self.transport.info = {"out_ep": 0x02, "in_ep": 0x81}
        self.transport._io = daemon.UsbIoRegion()
        self.transport._fd = -1  # only passed back into the patched ioctl

    def tearDown(self):
        self.transport._io.close()
        self.transport._io = None

    def _decode(self, arg):
        self.assertIsInstance(arg, (bytearray, bytes))
        endpoint, length, timeout = struct.unpack_from("<III", arg, 0)
        address = struct.unpack_from("P", arg, daemon._BULK_DATA_OFFSET)[0]
        return endpoint, length, timeout, address

    def test_write_puts_payload_at_the_advertised_address(self):
        payload = bytes(range(61))
        seen = {}

        def fake_ioctl(fd, request, arg):
            seen["request"] = request
            endpoint, length, timeout, address = self._decode(arg)
            seen.update(endpoint=endpoint, length=length,
                        timeout=timeout, address=address)
            return length

        with mock.patch.object(daemon.fcntl, "ioctl", fake_ioctl):
            rc = self.transport.write(payload)

        self.assertEqual(rc, len(payload))
        self.assertEqual(seen["request"], daemon.USBDEVFS_BULK)
        self.assertEqual(seen["endpoint"], 0x02)
        self.assertEqual(seen["length"], len(payload))
        self.assertEqual(seen["timeout"], daemon.BULK_WRITE_TIMEOUT_MS)
        self.assertEqual(seen["address"],
                         self.transport._io.base + daemon.UsbIoRegion.TX_SLOT)
        self.assertEqual(
            bytes(self.transport._io.region[
                daemon.UsbIoRegion.TX_SLOT:
                daemon.UsbIoRegion.TX_SLOT + len(payload)]),
            payload)

    def test_write_reports_short_transfer(self):
        def fake_ioctl(fd, request, arg):
            return 3

        with mock.patch.object(daemon.fcntl, "ioctl", fake_ioctl):
            with self.assertRaises(daemon.UsbError):
                self.transport.write(b"0123456789")

    def test_write_wraps_oserror(self):
        def fake_ioctl(fd, request, arg):
            raise OSError(errno.ENODEV, "No such device")

        with mock.patch.object(daemon.fcntl, "ioctl", fake_ioctl):
            with self.assertRaises(daemon.UsbError):
                self.transport.write(b"\xA5")

    def test_read_returns_bytes_from_the_bulk_buffer(self):
        def fake_ioctl(fd, request, arg):
            endpoint, length, _timeout, _address = self._decode(arg)
            self.assertEqual(endpoint, 0x81)
            self.assertEqual(length, 64)
            self.transport._io.region[
                daemon.UsbIoRegion.RX_SLOT:daemon.UsbIoRegion.RX_SLOT + 6] = \
                b"\xA5\x5A\x01\x04\x03\x50"
            return 6

        with mock.patch.object(daemon.fcntl, "ioctl", fake_ioctl):
            self.assertEqual(self.transport.read(64), b"\xA5\x5A\x01\x04\x03\x50")

    def test_read_timeout_returns_empty(self):
        def fake_ioctl(fd, request, arg):
            raise OSError(errno.ETIMEDOUT, "Connection timed out")

        with mock.patch.object(daemon.fcntl, "ioctl", fake_ioctl):
            self.assertEqual(self.transport.read(64), b"")

    def test_claim_uses_a_pointer_to_the_interface_number(self):
        seen = {}

        def fake_ioctl(fd, request, arg):
            seen["request"] = request
            seen["interface"] = struct.unpack_from("<I", arg, 0)[0]
            return 0

        with mock.patch.object(daemon.fcntl, "ioctl", fake_ioctl):
            self.transport._claim(0)
        self.assertEqual(seen["request"], daemon.USBDEVFS_CLAIMINTERFACE)
        self.assertEqual(seen["interface"], 0)
        self.assertTrue(self.transport._claimed)

    def test_transfer_larger_than_the_slot_is_rejected(self):
        with self.assertRaises(daemon.UsbError):
            self.transport.write(bytes(daemon.UsbIoRegion.SLOT_SIZE + 1))


class StatsCollectorTest(unittest.TestCase):
    """CPU / network rates are deltas, so a single sample must not be 0%."""

    def _collector(self):
        return daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])

    def test_cpu_usage_needs_two_samples(self):
        collector = self._collector()
        samples = iter([(1000, 800), (1200, 850)])
        with mock.patch.object(daemon, "_read_proc_stat",
                               lambda: next(samples)):
            self.assertEqual(collector._cpu_usage(), 0)
            # +200 total ticks, +50 idle -> 75% used
            self.assertEqual(collector._cpu_usage(), 75)

    def test_warmup_makes_the_first_sample_meaningful(self):
        collector = self._collector()
        samples = iter([(1000, 800), (1100, 850)])
        with mock.patch.object(daemon, "_read_proc_stat",
                               lambda: next(samples)), \
                mock.patch.object(daemon, "_read_net_dev", lambda: {}):
            collector.warmup(window=0)
            # +100 total ticks, +50 idle -> 50% used
            self.assertEqual(collector._cpu_usage(), 50)

    def test_warmup_seeds_net_counters(self):
        collector = self._collector()
        with mock.patch.object(daemon, "_read_proc_stat",
                               lambda: (1000, 900)), \
                mock.patch.object(daemon, "_read_net_dev",
                                  lambda: {"test0": (10, 20)}):
            collector.warmup(window=0)
        self.assertEqual(collector._prev_net, {"test0": (10, 20)})

    def test_wlan_total_is_reported_in_kb(self):
        collector = self._collector()
        collector._prev_net = {"test0": (0, 0)}
        counters = {"test0": (5 * 1024 ** 3, 3 * 1024 ** 3)}
        with mock.patch.object(daemon, "_read_net_dev", lambda: counters):
            nets = collector._networks(1.0)
        self.assertEqual(nets[0].total_kb, 8 * 1024 ** 2)   # 8 GiB in KB
        self.assertIsNone(nets[0].total_unit)   # auto

    def test_wlan_total_unit_flag_is_threaded_through(self):
        args = daemon.build_parser().parse_args(["--wlan-total-unit", "gb", "dump"])
        collector = daemon._make_collector(args)
        self.assertEqual(collector.wlan_total_unit, 2)
        args = daemon.build_parser().parse_args(["dump"])
        self.assertIsNone(daemon._make_collector(args).wlan_total_unit)

    def test_network_rates_from_delta(self):
        collector = self._collector()
        collector._prev_net = {"test0": (0, 0)}
        counters = {"test0": (2 * 1024 * 1024, 1024 * 1024)}
        with mock.patch.object(daemon, "_read_net_dev", lambda: counters):
            nets = collector._networks(1.0)
        self.assertEqual(len(nets), 1)
        self.assertEqual(nets[0].down_kbps, 2048)
        self.assertEqual(nets[0].up_kbps, 1024)
        self.assertEqual(nets[0].total_kb, 3 * 1024)

    def test_single_shot_commands_warm_up_first(self):
        calls = []

        def fake_warmup(self, window=0.3):
            calls.append(window)

        with mock.patch.object(daemon.StatsCollector, "warmup", fake_warmup):
            daemon.main(["dump", "--mock"])
            daemon.main(["selftest"])
        self.assertEqual(len(calls), 2)


class InterfaceSelectionTest(unittest.TestCase):
    """Unraid bridge members and macvlan shims must not reach the panel."""

    def _pick(self, names, addressed, up):
        with mock.patch.object(daemon.os, "listdir", lambda _path: names), \
                mock.patch.object(daemon, "_iface_has_ipv4",
                                  lambda name: name in addressed), \
                mock.patch.object(daemon, "_iface_up", lambda name: name in up):
            return daemon._default_ifaces()

    def test_only_the_bridge_that_owns_an_address_is_sent(self):
        self.assertEqual(
            self._pick(["lo", "br0", "eth0", "shim-br0", "docker0"],
                       {"br0"}, {"br0", "eth0"}),
            ["br0"])

    def test_macvlan_shims_are_excluded(self):
        self.assertEqual(
            self._pick(["lo", "shim-br0", "shim-eth0", "br0", "tailscale0"],
                       {"shim-br0", "shim-eth0", "br0"},
                       {"shim-br0", "shim-eth0", "br0"}),
            ["br0"])

    def test_falls_back_to_up_interfaces_without_addresses(self):
        self.assertEqual(
            self._pick(["lo", "br0", "eth0", "bond0"], set(), {"br0", "eth0"}),
            ["br0", "eth0"])

    def test_priority_order_and_three_adapter_cap(self):
        self.assertEqual(
            self._pick(["eth2", "br0", "en1", "en2", "en3"],
                       {"br0", "eth2", "en1", "en2", "en3"}, set()),
            ["br0", "en1", "en2"])

    def test_loopback_and_absent_interfaces(self):
        self.assertTrue(daemon._iface_has_ipv4("lo"))
        self.assertFalse(daemon._iface_has_ipv4("subscreen-no-such-iface"))

    def test_explicit_interfaces_are_never_refreshed(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["eth9"])
        with mock.patch.object(daemon, "_default_ifaces", lambda: ["br0"]):
            collector._refresh_ifaces(time.monotonic() + 120)
        self.assertEqual(collector.ifaces, ["eth9"])

    def test_automatic_interfaces_refresh_later(self):
        # /boot/config/go runs before br0 has an address on Unraid.
        with mock.patch.object(daemon, "_default_ifaces", lambda: ["eth0"]):
            collector = daemon.StatsCollector(disk_paths=["/proc"])
        self.assertEqual(collector.ifaces, ["eth0"])
        with mock.patch.object(daemon, "_default_ifaces", lambda: ["br0"]):
            collector._refresh_ifaces(time.monotonic() + 120)
        self.assertEqual(collector.ifaces, ["br0"])


class DiskCapacityUnitTest(unittest.TestCase):
    """The panel's disk unit base is GB, not MB (vendor: bytes >> 30)."""

    def test_disk_capacity_is_reported_in_gb(self):
        collector = daemon.StatsCollector(disk_paths=["/fake/disk1"],
                                          ifaces=["test0"])
        fake = types.SimpleNamespace(f_frsize=1, f_bsize=1,
                                     f_blocks=500 * 1024 ** 3,
                                     f_bfree=200 * 1024 ** 3)
        with mock.patch.object(daemon.os, "statvfs", lambda _path: fake):
            disks = collector._disks()
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].total_gb, 500)
        self.assertEqual(disks[0].used_gb, 300)
        # 500 GB must stay in GB; a MiB-based value would be promoted to GB
        # and the panel would show the wrong label.
        self.assertEqual(encode_disk_size(disks[0].total_gb, disks[0].used_gb),
                         (0x22, 500, 300))

    def test_mib_based_values_would_be_promoted(self):
        # Regression guard: 500 GiB expressed as MiB is > 4000 u16 and gets
        # shifted into GB, which is exactly the bug that was reported.
        unit, total, _used = encode_disk_size(500 * 1024, 200 * 1024)
        self.assertEqual(unit & 0x0F, 0x3)
        self.assertEqual(total, 500)


class DiskTemperatureTest(unittest.TestCase):
    """Disk temps come from hwmon when present, else from smartctl."""

    def setUp(self):
        daemon._smart_temp_cache.clear()
        daemon._smart_dev_type.clear()
        self._saved_binary = daemon._smartctl_binary
        daemon._smartctl_binary = "/usr/sbin/smartctl"

    def tearDown(self):
        daemon._smart_temp_cache.clear()
        daemon._smart_dev_type.clear()
        daemon._smartctl_binary = self._saved_binary

    def test_parses_ata_json(self):
        self.assertEqual(
            daemon._parse_smart_temp('{"temperature": {"current": 34}}'), 34)

    def test_parses_nvme_json(self):
        payload = '{"nvme_smart_health_information_log": {"temperature": 41}}'
        self.assertEqual(daemon._parse_smart_temp(payload), 41)

    def test_parses_ata_text(self):
        payload = ("194 Temperature_Celsius     0x0022   034   020   045"
                   "   000   034    -    34  (Min/Max 20/45)")
        self.assertEqual(daemon._parse_smart_temp(payload), 34)

    def test_parses_nvme_text(self):
        payload = "Temperature:                        38 Celsius"
        self.assertEqual(daemon._parse_smart_temp(payload), 38)

    def test_rejects_nonsense(self):
        self.assertIsNone(daemon._parse_smart_temp(""))
        self.assertIsNone(daemon._parse_smart_temp("NO DATA"))
        self.assertIsNone(
            daemon._parse_smart_temp('{"temperature": {"current": 999}}'))

    def test_real_smartctl_argv_is_not_mistaken_for_standby(self):
        # smartctl 7.5 echoes argv into the JSON, and that argv contains
        # "-n standby"; a naive substring scan turns every disk into "asleep".
        payload = (
            '{"json_format_version":[1,0],"smartctl":{"version":[7,5],'
            '"argv":["smartctl","-A","-n","standby","--json=c","/dev/nvme1n1"],'
            '"exit_status":0},"device":{"name":"/dev/nvme1n1","type":"nvme"},'
            '"temperature":{"current":36}}')
        result = types.SimpleNamespace(returncode=0, stdout=payload, stderr="")
        self.assertFalse(daemon._smart_says_standby(result))
        self.assertEqual(daemon._parse_smart_temp(payload), 36)

    def test_power_mode_sleep_is_standby(self):
        payload = ('{"smartctl":{"argv":["smartctl","-n","standby"]},'
                   '"power_mode":{"ata_value":-1,"name":"SLEEP"},'
                   '"messages":[{"string":"Device is in SLEEP mode, exit(2)"}]}')
        result = types.SimpleNamespace(returncode=2, stdout=payload, stderr="")
        self.assertTrue(daemon._smart_says_standby(result))

    def test_active_power_mode_is_not_standby(self):
        payload = '{"power_mode":{"name":"ACTIVE or IDLE"},"temperature":{"current":34}}'
        result = types.SimpleNamespace(returncode=0, stdout=payload, stderr="")
        self.assertFalse(daemon._smart_says_standby(result))

    def test_exit_two_with_a_reading_is_not_standby(self):
        payload = '{"temperature":{"current":33}}'
        result = types.SimpleNamespace(returncode=2, stdout=payload, stderr="")
        self.assertFalse(daemon._smart_says_standby(result))

    def test_exit_two_without_a_reading_is_standby(self):
        result = types.SimpleNamespace(returncode=2, stdout="", stderr="")
        self.assertTrue(daemon._smart_says_standby(result))

    def test_standby_does_not_wake_the_disk(self):
        asleep = types.SimpleNamespace(
            returncode=2, stdout="Device is in STANDBY mode, exit(2)", stderr="")
        with mock.patch.object(daemon, "_run_smartctl",
                               lambda dev, device_type=None, use_json=True, wake=False: asleep):
            self.assertEqual(daemon._query_smart_temp("sdb"), (None, "standby:sdb"))

    def test_real_nvme_payload_yields_a_temperature(self):
        payload = (
            '{"smartctl":{"argv":["smartctl","-A","-n","standby","--json=c",'
            '"/dev/nvme0n1"],"exit_status":0},"power_mode":{"name":"ACTIVE or IDLE"},'
            '"temperature":{"current":34}}')
        ok = types.SimpleNamespace(returncode=0, stdout=payload, stderr="")
        with mock.patch.object(daemon, "_run_smartctl",
                               lambda dev, device_type=None, use_json=True, wake=False: ok):
            self.assertEqual(daemon._query_smart_temp("nvme0n1"),
                             (34, "smartctl:nvme0n1"))

    def test_json_flag_failure_falls_back_to_text(self):
        calls = []

        def fake_run(dev, device_type=None, use_json=True, wake=False):
            calls.append((dev, device_type, use_json))
            if use_json:
                return types.SimpleNamespace(returncode=1, stdout="")
            return types.SimpleNamespace(
                returncode=0,
                stdout="194 Temperature_Celsius 0x0022 ... 34 (Min/Max 20/45)")

        with mock.patch.object(daemon, "_run_smartctl", fake_run):
            self.assertEqual(daemon._query_smart_temp("sdb")[0], 34)
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1][2])

    def test_device_type_is_retried_until_one_works(self):
        tried = []

        def fake_run(dev, device_type=None, use_json=True, wake=False):
            tried.append(device_type)
            if device_type == "sat":
                return types.SimpleNamespace(
                    returncode=0,
                    stdout='{"temperature": {"current": 32}}')
            return types.SimpleNamespace(returncode=4, stdout="")

        with mock.patch.object(daemon, "_run_smartctl", fake_run):
            value, reason = daemon._query_smart_temp("sdc")
        self.assertEqual(value, 32)
        self.assertEqual(reason, "smartctl:sdc(-d sat)")
        self.assertEqual(tried[:2], [None, "sat"])
        # The working type is remembered, so the next query goes straight to it.
        self.assertEqual(daemon._smart_dev_type["sdc"], "sat")
        with mock.patch.object(daemon, "_run_smartctl", fake_run):
            tried.clear()
            daemon._query_smart_temp("sdc")
        self.assertEqual(tried, ["sat"])

    def test_failure_reason_includes_the_exit_code(self):
        failed = types.SimpleNamespace(returncode=4, stdout="Read Device Identity failed")
        with mock.patch.object(daemon, "_run_smartctl",
                               lambda dev, device_type=None, use_json=True, wake=False: failed):
            self.assertEqual(daemon._query_smart_temp("sdb"),
                             (None, "no-temp:sdb(exit=4)"))

    def test_missing_smartctl_is_tolerated(self):
        daemon._smartctl_binary = ""
        self.assertEqual(daemon._query_smart_temp("sdb"), (None, "no-smartctl"))

    def test_temperature_is_cached(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        with mock.patch.object(daemon, "_query_smart_temp",
                               return_value=(33, "smartctl:sdb")) as query:
            self.assertEqual(collector._smart_temp("sdb"), (33, "smartctl:sdb"))
            self.assertEqual(collector._smart_temp("sdb"), (33, "smartctl:sdb"))
        self.assertEqual(query.call_count, 1)

    def test_first_lookups_are_never_throttled(self):
        # A single-shot `once`/`dump`/`selftest` must report every disk, so
        # the refresh budget cannot apply to devices with no cached value.
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        collector._smart_budget = 0
        with mock.patch.object(daemon, "_query_smart_temp",
                               side_effect=lambda dev, wake=False: (30, "smartctl:" + dev)) as query:
            self.assertEqual(collector._smart_temp("sdb"), (30, "smartctl:sdb"))
            self.assertEqual(collector._smart_temp("sdc"), (30, "smartctl:sdc"))
        self.assertEqual(query.call_count, 2)

    def test_budget_throttles_refreshes(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        collector._smart_budget = 0
        with mock.patch.object(daemon, "_query_smart_temp",
                               return_value=(30, "smartctl:sdb")) as query:
            collector._smart_temp("sdb")
            collector._smart_temp("sdb")          # still inside the TTL
        self.assertEqual(query.call_count, 1)
        daemon._smart_temp_cache["sdb"] = (0.0, 30, "smartctl:sdb")
        with mock.patch.object(daemon, "_query_smart_temp",
                               return_value=(31, "smartctl:sdb")) as query:
            self.assertEqual(collector._smart_temp("sdb"), (30, "smartctl:sdb"))
        self.assertEqual(query.call_count, 0)

    def test_hwmon_wins_over_smartctl(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["sdb"]), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: [45]), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  return_value=(30, "smartctl:sdb")):
            self.assertEqual(collector._disk_temp_detail("/dev/sdb"),
                             (45, "hwmon:sdb"))

    def test_smartctl_is_the_fallback(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["md1", "sdb"]), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  return_value=(30, "smartctl:sdb")):
            self.assertEqual(collector._disk_temp_detail("/dev/md1"),
                             (30, "smartctl:sdb"))

    def test_failure_reason_survives_to_the_report(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["md1", "sdb"]), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  return_value=(None, "no-temp:sdb(exit=4)")):
            self.assertEqual(collector._disk_temp_detail("/dev/md1"),
                             (0xFF, "no-temp:sdb(exit=4)"))

    def test_unraid_disks_ini_maps_mount_names(self):
        sample = textwrap.dedent('''
            ["disk1"]
            idx="1"
            name="disk1"
            device="sdc"

            ["cache"]
            name="cache"
            device="nvme0n1"
            ''')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "disks.ini")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(sample)
            with mock.patch.object(daemon, "UNRAID_DISKS_INI", path):
                mapping = daemon._unraid_disk_map()
        self.assertEqual(mapping["disk1"], "/dev/sdc")
        self.assertEqual(mapping["cache"], "/dev/nvme0n1")
        self.assertEqual(mapping["sdc"], "/dev/sdc")

    def test_unraid_map_is_used_when_slaves_are_missing(self):
        # Unraid's md driver exposes no slaves/, so without disks.ini the
        # physical disk behind /mnt/disk1 is never reached.
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        table = {"disk1": {"device": "sdc", "temp": "*"}}
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["md1"]), \
                mock.patch.object(daemon, "_unraid_disk_table",
                                  lambda: table), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  lambda dev, wake=False: (36, "smartctl:" + dev)):
            self.assertEqual(
                collector._disk_temp_detail("/dev/md1", "disk1"),
                (36, "smartctl:sdc"))

    def test_fresh_smartctl_wins_over_stale_disks_ini(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        table = {"disk1": {"device": "nvme1n1", "temp": "40", "spundown": "0"}}
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["md1p1", "nvme1n1"]), \
                mock.patch.object(daemon, "_unraid_disk_table",
                                  lambda: table), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  lambda dev, wake=False: (37, "smartctl:" + dev)):
            self.assertEqual(
                collector._disk_temp_detail("/dev/md1p1", "disk1"),
                (37, "smartctl:nvme1n1"))

    def test_disks_ini_is_used_when_smartctl_has_nothing(self):
        # A sleeping disk: smartctl refuses to read it, so the value Unraid
        # cached for the web UI is the best available answer.
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        table = {"disk1": {"device": "sdc", "temp": "34", "spundown": "1"}}
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["md1"]), \
                mock.patch.object(daemon, "_unraid_disk_table",
                                  lambda: table), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  lambda dev, wake=False: (None, "standby:" + dev)):
            self.assertEqual(
                collector._disk_temp_detail("/dev/md1", "disk1"),
                (34, "unraid:disk1(sleep)"))

    def test_whole_disks_are_probed_before_partitions(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        probed = []
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["sdb1", "sdb"]), \
                mock.patch.object(daemon, "_unraid_disk_table", lambda: {}), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  lambda dev, wake=False: (probed.append(dev) or 35,
                                               "smartctl:" + dev)):
            self.assertEqual(
                collector._disk_temp_detail("/dev/sdb1", "TevenDisk"),
                (35, "smartctl:sdb"))
        self.assertEqual(probed, ["sdb"])

    def test_sleeping_disk_is_labelled(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        table = {"disk1": {"device": "sdc", "temp": "31", "spundown": "1"}}
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["md1"]), \
                mock.patch.object(daemon, "_unraid_disk_table",
                                  lambda: table), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []):
            self.assertEqual(
                collector._disk_temp_detail("/dev/md1", "disk1"),
                (31, "unraid:disk1(sleep)"))

    def test_boot_mount_uses_the_flash_entry(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        table = {"flash": {"device": "sda", "temp": "27"}}
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["sda1"]), \
                mock.patch.object(daemon, "_unraid_disk_table",
                                  lambda: table), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []):
            self.assertEqual(
                collector._disk_temp_detail("/dev/sda1", "boot"),
                (27, "unraid:boot"))

    def test_unknown_unraid_temperature_falls_through(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        table = {"disk1": {"device": "sdc", "temp": "*"}}
        with mock.patch.object(daemon, "_related_block_devices",
                               lambda name: ["md1"]), \
                mock.patch.object(daemon, "_unraid_disk_table",
                                  lambda: table), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  lambda dev, wake=False: (None, "standby:" + dev)):
            self.assertEqual(
                collector._disk_temp_detail("/dev/md1", "disk1"),
                (0xFF, "standby:sdc"))

    def test_disks_ini_temp_is_parsed(self):
        sample = textwrap.dedent('''
            ["disk1"]
            name="disk1"
            device="sdc"
            temp="38"
            spundown="0"
            ''')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "disks.ini")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(sample)
            with mock.patch.object(daemon, "UNRAID_DISKS_INI", path):
                table = daemon._unraid_disk_table()
        self.assertEqual(table["disk1"]["device"], "sdc")
        self.assertEqual(daemon._sane_temp(table["disk1"]["temp"]), 38)

    def test_missing_disks_ini_is_tolerated(self):
        with mock.patch.object(daemon, "UNRAID_DISKS_INI",
                               "/nonexistent/disks.ini"):
            self.assertEqual(daemon._unraid_disk_map(), {})

    def test_unmounted_disk_is_reported_as_such(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        self.assertEqual(collector._disk_temp_detail(""), (0xFF, "no-device"))

    def test_related_devices_include_md_slaves(self):
        names = daemon._related_block_devices("md1")
        self.assertIn("md1", names)
        self.assertTrue(all(isinstance(name, str) and name for name in names))

    def _fake_smartctl(self, tmp, body):
        path = os.path.join(tmp, "smartctl")
        with open(path, "w", encoding="ascii") as handle:
            handle.write(body)
        os.chmod(path, 0o755)
        return path

    def test_real_subprocess_call_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon._smartctl_binary = self._fake_smartctl(
                tmp, "#!/bin/sh\necho '{\"temperature\": {\"current\": 36}}'\n")
            self.assertEqual(daemon._query_smart_temp("sdb"),
                             (36, "smartctl:sdb"))

    def test_real_subprocess_standby_exit_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon._smartctl_binary = self._fake_smartctl(
                tmp, "#!/bin/sh\necho 'Device is in STANDBY mode'\nexit 2\n")
            self.assertEqual(daemon._query_smart_temp("sdb"),
                             (None, "standby:sdb"))


class DiskDiscoveryTest(unittest.TestCase):
    """Unassigned Devices mounts are reported like array disks."""

    def _paths(self, mounts, table=None):
        with mock.patch.object(daemon, "_read_mounts", lambda: mounts), \
                mock.patch.object(daemon, "_unraid_disk_table",
                                  lambda: table or {}), \
                mock.patch.object(daemon.os, "listdir", lambda path: []), \
                mock.patch.object(daemon.os.path, "isdir", lambda path: True):
            return daemon._default_disk_paths()

    def test_order_puts_array_disks_first(self):
        mounts = {"/mnt/disk2": "/dev/md2", "/mnt/disk10": "/dev/md10",
                  "/mnt/disk1": "/dev/md1", "/mnt/cache": "/dev/nvme0n1p1",
                  "/boot": "/dev/sda1", "/mnt/disks/USB": "/dev/sdd1"}
        table = {"disk1": {}, "disk2": {}, "disk10": {}, "cache": {},
                 "flash": {}}
        self.assertEqual(self._paths(mounts, table),
                         ["/mnt/disk1", "/mnt/disk2", "/mnt/disk10",
                          "/mnt/cache", "/mnt/disks/USB"])

    def test_unassigned_devices_and_extra_pools_are_included(self):
        mounts = {
            "/mnt/user": "shfs",
            "/mnt/user0": "shfs",
            "/mnt/remotes/NAS": "//192.168.1.9/media",
            "/mnt/addons/plugin": "/dev/loop0",
            "/mnt/rootshare": "/mnt/user",
            "/boot": "/dev/sda1",
            "/mnt/cache": "/dev/nvme0n1p1",
            "/mnt/fast": "/dev/nvme1n1p1",
            "/mnt/disks/USB_2TB": "/dev/sdd1",
            "/mnt/disks/WD_4TB": "/dev/sde1",
        }
        paths = self._paths(mounts)
        self.assertIn("/mnt/disks/USB_2TB", paths)
        self.assertIn("/mnt/disks/WD_4TB", paths)
        self.assertIn("/mnt/fast", paths)
        self.assertIn("/mnt/cache", paths)
        for excluded in ("/mnt/user", "/mnt/user0", "/mnt/remotes/NAS",
                         "/mnt/addons/plugin", "/mnt/rootshare", "/boot"):
            self.assertNotIn(excluded, paths)

    def test_boot_flash_is_hidden_by_default(self):
        # A USB stick with no SMART temperature and no bearing on the array.
        paths = self._paths({"/boot": "/dev/sda1",
                             "/mnt/disk1": "/dev/md1p1"})
        self.assertNotIn("/boot", paths)
        self.assertIn("/mnt/disk1", paths)

    def test_boot_can_still_be_requested_explicitly(self):
        collector = daemon.StatsCollector(disk_paths=["/boot"], ifaces=["test0"])
        self.assertEqual(collector.disk_paths, ["/boot"])

    def test_non_block_mounts_are_skipped(self):
        paths = self._paths({"/mnt/disks/ZFS": "tank/dataset",
                             "/mnt/disks/TMPFS": "tmpfs"})
        self.assertNotIn("/mnt/disks/ZFS", paths)
        self.assertNotIn("/mnt/disks/TMPFS", paths)

    def test_labels_prefer_the_unassigned_device_name(self):
        self.assertEqual(daemon._disk_label("/mnt/disks/USB_2TB"), "USB_2TB")
        self.assertEqual(daemon._disk_label("/mnt/disks/USB_2TB/sub/dir"),
                         "USB_2TB")
        self.assertEqual(daemon._disk_label("/mnt/disk1"), "disk1")
        self.assertEqual(daemon._disk_label("/boot"), "boot")
        self.assertEqual(daemon._disk_label("/mnt/fast/"), "fast")

    def test_unassigned_disk_is_collected_end_to_end(self):
        mounts = {"/mnt/disks/USB_2TB": "/dev/sdd1"}
        gb = 1024 ** 3
        fake = types.SimpleNamespace(f_frsize=1, f_bsize=1,
                                     f_blocks=2000 * gb, f_bfree=500 * gb)
        collector = daemon.StatsCollector(disk_paths=["/mnt/disks/USB_2TB"],
                                          ifaces=["test0"])
        with mock.patch.object(daemon, "_read_mounts", lambda: mounts), \
                mock.patch.object(daemon, "_unraid_disk_table", lambda: {}), \
                mock.patch.object(daemon, "_related_block_devices",
                                  lambda name: [name]), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  lambda dev, wake=False: (33, "smartctl:" + dev)), \
                mock.patch.object(daemon.os, "statvfs", lambda path: fake):
            disks = collector._disks()
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].name, "USB_2TB")
        self.assertEqual(disks[0].total_gb, 2000)
        self.assertEqual(disks[0].used_gb, 1500)
        self.assertEqual(disks[0].temp, 33)

    def test_disk_count_is_capped(self):
        gb = 1024 ** 3
        fake = types.SimpleNamespace(f_frsize=1, f_bsize=1,
                                     f_blocks=100 * gb, f_bfree=50 * gb)
        paths = ["/mnt/disks/d%d" % i for i in range(12)]
        collector = daemon.StatsCollector(disk_paths=paths, ifaces=["test0"])
        with mock.patch.object(daemon, "_read_mounts", lambda: {}), \
                mock.patch.object(daemon, "_unraid_disk_table", lambda: {}), \
                mock.patch.object(daemon, "_related_block_devices",
                                  lambda name: [name]), \
                mock.patch.object(collector, "_block_temp_values",
                                  lambda name: []), \
                mock.patch.object(daemon, "_query_smart_temp",
                                  lambda dev, wake=False: (None, "standby:" + dev)), \
                mock.patch.object(daemon.os, "statvfs", lambda path: fake):
            disks = collector._disks()
        self.assertEqual(len(disks), daemon.MAX_DISKS)


class CliVersionTest(unittest.TestCase):
    """`version` identifies the files in use, which is how a stale copy on
    the Unraid box gets caught."""

    def test_version_command_reports_both_files(self):
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdout", buffer):
            self.assertEqual(daemon.main(["version"]), 0)
        output = buffer.getvalue()
        self.assertIn("version: daemon ", output)
        self.assertIn("protocol", output)
        self.assertIn("subscreen_daemon.py", output)

    def test_signature_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.py")
            with open(path, "w", encoding="ascii") as handle:
                handle.write("a = 1\n")
            first = daemon._file_signature(path).split()[0]
            with open(path, "w", encoding="ascii") as handle:
                handle.write("a = 2\n")
            second = daemon._file_signature(path).split()[0]
        self.assertNotEqual(first, second)

    def test_missing_file_signature_is_safe(self):
        self.assertEqual(daemon._file_signature("/nonexistent/x.py"),
                         "unreadable")


    def test_a_miss_is_retried_sooner_than_a_reading(self):
        collector = daemon.StatsCollector(disk_paths=["/proc"], ifaces=["test0"])
        now = time.monotonic()
        daemon._smart_temp_cache["sdb"] = (now - 60.0, 33, "smartctl:sdb")
        daemon._smart_temp_cache["sdc"] = (now - 60.0, None, "standby:sdc")
        with mock.patch.object(daemon, "_query_smart_temp",
                               side_effect=lambda dev, wake=False: (35, "smartctl:" + dev)) as query:
            self.assertEqual(collector._smart_temp("sdb"), (33, "smartctl:sdb"))
            self.assertEqual(collector._smart_temp("sdc"), (35, "smartctl:sdc"))
        self.assertEqual(query.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class GpuAndFanTest(unittest.TestCase):
    """The four SystemPage cards: CPU / iGPU / Memory / DGPU."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self._saved = (daemon.DRM_ROOT, daemon.HWMON_ROOT)
        daemon.DRM_ROOT = os.path.join(self.root, "drm")
        daemon.HWMON_ROOT = os.path.join(self.root, "hwmon")
        os.makedirs(daemon.DRM_ROOT)
        os.makedirs(daemon.HWMON_ROOT)

    def tearDown(self):
        daemon.DRM_ROOT, daemon.HWMON_ROOT = self._saved

    def _file(self, path, value):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="ascii") as handle:
            handle.write(str(value))

    def _card(self, index, vendor, busy=None, temp_mc=None, pwm=None):
        base = "drm/card%d" % index
        self._file(base + "/device/vendor", vendor)
        self._file(base + "/device/class", "0x030000")
        if busy is not None:
            self._file(base + "/device/gpu_busy_percent", busy)
        if temp_mc is not None or pwm is not None:
            hw = base + "/device/hwmon/hwmon0"
            if temp_mc is not None:
                self._file(hw + "/temp1_input", temp_mc)
            if pwm is not None:
                self._file(hw + "/pwm1", pwm)
        return base

    def test_amd_busy_counter_temp_and_pwm(self):
        self._card(0, "0x1002", busy=77, temp_mc=45000, pwm=128)
        igpu, dgpu = daemon._scan_gpus()
        reading = igpu or dgpu
        self.assertEqual(reading.vendor, "1002")
        self.assertEqual(reading.usage, 77)
        self.assertEqual(reading.temp, 45)
        self.assertEqual(reading.fan, 50)      # pwm 128/255
        self.assertEqual(reading.source, "busy:gpu_busy_percent")

    def test_intel_usage_comes_from_the_gt_frequency_range(self):
        base = self._card(0, "0x8086")
        self._file(base + "/gt_min_freq_mhz", 200)
        self._file(base + "/gt_max_freq_mhz", 1000)
        # Idle sits on the minimum frequency: that must read 0%, not 20%.
        self._file(base + "/gt_cur_freq_mhz", 200)
        self.assertEqual(daemon._scan_gpus()[0].usage, 0)
        self._file(base + "/gt_cur_freq_mhz", 600)
        self.assertEqual(daemon._scan_gpus()[0].usage, 50)
        self._file(base + "/gt_cur_freq_mhz", 1000)
        self.assertEqual(daemon._scan_gpus()[0].usage, 100)

    def test_intel_without_a_counter_reports_no_data(self):
        self._card(0, "0x8086")
        igpu = daemon._scan_gpus()[0]
        self.assertEqual(igpu.usage, daemon.NO_VALUE)
        self.assertEqual(igpu.source, "no-counter")

    def test_nvidia_card_is_the_dgpu(self):
        base = self._card(0, "0x8086", temp_mc=60000)
        self._file(base + "/gt_min_freq_mhz", 100)
        self._file(base + "/gt_max_freq_mhz", 900)
        self._file(base + "/gt_cur_freq_mhz", 100)
        self._card(1, "0x10de", temp_mc=55000, pwm=200)
        igpu, dgpu = daemon._scan_gpus()
        self.assertEqual(igpu.vendor, "8086")
        self.assertEqual(dgpu.vendor, "10de")
        self.assertEqual(dgpu.temp, 55)
        self.assertEqual(dgpu.fan, 78)

    def test_non_display_pci_functions_are_ignored(self):
        self._file("drm/card0/device/vendor", "0x8086")
        self._file("drm/card0/device/class", "0x040300")   # audio function
        self.assertEqual(daemon._scan_gpus(), (None, None))

    def test_fan_prefers_pwm_then_rpm(self):
        path = os.path.join(self.root, "hwmon", "hwmon0")
        os.makedirs(path)
        with open(os.path.join(path, "pwm1"), "w", encoding="ascii") as handle:
            handle.write("255")
        self.assertEqual(daemon._fan_percent(path), 100)
        # pwm reads 0 while the EC keeps the fan spinning.
        with open(os.path.join(path, "pwm1"), "w", encoding="ascii") as handle:
            handle.write("0")
        with open(os.path.join(path, "fan1_input"), "w", encoding="ascii") as handle:
            handle.write("1200")
        with open(os.path.join(path, "fan1_max"), "w", encoding="ascii") as handle:
            handle.write("2400")
        self.assertEqual(daemon._fan_percent(path), 50)
        os.remove(os.path.join(path, "fan1_max"))
        self.assertEqual(daemon._fan_percent(path), 12)    # rpm/100 proxy
        os.remove(os.path.join(path, "fan1_input"))
        self.assertEqual(daemon._fan_percent(path), 0)

    def test_fan_without_any_sensor_is_no_value(self):
        path = os.path.join(self.root, "hwmon", "hwmon0")
        os.makedirs(path)
        self.assertEqual(daemon._fan_percent(path), daemon.NO_VALUE)

    def test_unknown_fan_renders_as_blank_percent(self):
        self.assertEqual(daemon._format_percent(0xFF), "--")
        self.assertEqual(daemon._format_percent(42), "42%")

    def test_cpu_fan_is_cached_for_two_seconds(self):
        path = os.path.join(daemon.HWMON_ROOT, "hwmon0")
        os.makedirs(path)
        with open(os.path.join(path, "pwm1"), "w", encoding="ascii") as handle:
            handle.write("153")
        collector = daemon.StatsCollector(disk_paths=["/nonexistent"],
                                          ifaces=["lo"])
        self.assertEqual(collector._cpu_fan(0.0), 60)
        with open(os.path.join(path, "pwm1"), "w", encoding="ascii") as handle:
            handle.write("255")
        self.assertEqual(collector._cpu_fan(0.5), 60)
        self.assertEqual(collector._cpu_fan(5.0), 100)


class ReportLinesTest(unittest.TestCase):
    """`once` prints the same card values it pushes, for panel comparisons."""

    def _snapshot(self):
        from subscreen_protocol import SystemSnapshot, DiskItem, WlanItem
        return SystemSnapshot(
            timestamp=1793233270, cpu_usage=27, cpu_temp=84, cpu_fan=37,
            mem_usage=65, igpu_usage=8, igpu_temp=84, igpu_fan=37,
            dgpu_present=False,
            disks=[DiskItem(0, 476, 299, 34, "disk1")],
            networks=[WlanItem(0, 1200, 3400, 8 * 1024 ** 3,
                               (192, 168, 31, 33), "br0")],
        )

    def test_report_lists_every_card_in_panel_order(self):
        lines = daemon._report_lines(self._snapshot())
        body = [line.strip() for line in lines]
        self.assertEqual(body[0][:9], "snapshot:")
        self.assertEqual(body[1][:5], "time:")
        self.assertIn("sys[0] CPU", body[2])
        self.assertIn("usage=27%", body[2])
        self.assertIn("fan=37%", body[2])
        self.assertIn("sys[1] iGPU", body[3])
        self.assertIn("sys[2] Memory", body[4])
        self.assertIn("temp=N/A", body[4])         # memory has no sensor
        # No DGPU card at all when the machine has none.
        self.assertFalse(any("sys[3]" in line for line in body))
        self.assertTrue(any("disk[0] disk1" in line for line in body))
        self.assertTrue(any("net[0] br0" in line for line in body))

    def test_report_shows_the_dgpu_card_when_present(self):
        snapshot = self._snapshot()
        snapshot.dgpu_present = True
        snapshot.dgpu_usage = 55
        snapshot.dgpu_temp = 61
        snapshot.dgpu_fan = 70
        body = [line.strip() for line in daemon._report_lines(snapshot)]
        self.assertTrue(any("sys[3] DGPU" in line and "usage=55%" in line
                            for line in body))

    def test_report_survives_an_empty_machine(self):
        from subscreen_protocol import SystemSnapshot
        body = daemon._report_lines(SystemSnapshot(timestamp=1))
        self.assertTrue(any("disk: none" in line for line in body))
        self.assertTrue(any("net: none" in line for line in body))

    def test_once_mock_prints_the_cards_before_the_packets(self):
        snapshot = self._snapshot()
        args = daemon.build_parser().parse_args(["once", "--mock"])
        out = io.StringIO()
        with mock.patch.object(daemon, "StatsCollector") as collector_cls, \
                mock.patch("sys.stdout", out):
            collector = collector_cls.return_value
            collector.collect.return_value = snapshot
            collector.temp_sources = {}
            self.assertEqual(daemon.cmd_once(args), 0)
        text = out.getvalue()
        self.assertIn("sys[0] CPU", text)
        self.assertIn("sys[2] Memory", text)
        self.assertIn("packets:", text)
        self.assertLess(text.index("sys[0] CPU"), text.index("packets:"))


class PatternAndRepeatTest(unittest.TestCase):
    """The diagnostic pattern maps every byte to a card cell."""

    def test_pattern_has_four_distinct_cards(self):
        snapshot = daemon._pattern_snapshot()
        self.assertEqual(
            (snapshot.cpu_usage, snapshot.cpu_temp, snapshot.cpu_fan),
            (11, 12, 13))
        self.assertEqual(
            (snapshot.igpu_usage, snapshot.igpu_temp, snapshot.igpu_fan),
            (21, 22, 23))
        self.assertEqual(
            (snapshot.mem_usage, snapshot.mem_temp, snapshot.mem_fan),
            (31, 32, 33))
        self.assertTrue(snapshot.dgpu_present)
        self.assertEqual(
            (snapshot.dgpu_usage, snapshot.dgpu_temp, snapshot.dgpu_fan),
            (41, 42, 43))

    def test_pattern_values_reach_the_item_packets(self):
        from subscreen_protocol import build_full_sequence, PacketBuilder
        packets = build_full_sequence(daemon._pattern_snapshot(),
                                      PacketBuilder(0))
        items = [p for p in packets if p[5] == 0x11]
        self.assertEqual(items[0][6:10], bytes((0, 11, 12, 13)))
        self.assertEqual(items[1][6:10], bytes((1, 21, 22, 23)))
        self.assertEqual(items[2][6:10], bytes((2, 31, 32, 33)))
        self.assertEqual(items[3][6:10], bytes((3, 41, 42, 43)))

    def test_item_refreshes_can_be_dropped(self):
        from subscreen_protocol import build_full_sequence, PacketBuilder
        snapshot = daemon._pattern_snapshot()
        full = build_full_sequence(snapshot, PacketBuilder(0))
        pages_only = build_full_sequence(snapshot, PacketBuilder(0),
                                         include_items=False)
        self.assertTrue(any(packet[5] == 0x11 for packet in full))
        self.assertFalse(any(packet[5] == 0x11 for packet in pages_only))
        self.assertLess(len(pages_only), len(full))
        # The pages themselves are unchanged.
        self.assertEqual(full[:2], pages_only[:2])

    def test_once_repeats_the_frame(self):
        from subscreen_protocol import SystemSnapshot
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=5)
        args = daemon.build_parser().parse_args(["once", "--repeat", "3"])
        written = []

        class FakeTransport:
            def open(self):
                pass

            def close(self):
                pass

            def write(self, packet):
                written.append(packet)

        with mock.patch.object(daemon, "StatsCollector") as collector_cls, \
                mock.patch.object(daemon, "UsbTransport", FakeTransport), \
                mock.patch("sys.stdout", io.StringIO()), \
                mock.patch.object(daemon.time, "sleep", lambda _s: None):
            collector = collector_cls.return_value
            collector.collect.return_value = snapshot
            collector.temp_sources = {}
            self.assertEqual(daemon.cmd_once(args), 0)
        from subscreen_protocol import build_full_sequence, PacketBuilder
        per_frame = len(build_full_sequence(snapshot, PacketBuilder(0)))
        self.assertEqual(len(written), per_frame * 3)


class PacketPacingTest(unittest.TestCase):
    """A 31-packet burst overruns the panel's MCU; the vendor trickles them."""

    def _transport(self):
        written = []

        class Fake:
            def write(self, packet):
                written.append(packet)

        return Fake(), written

    def test_write_frame_pauses_between_packets(self):
        transport, written = self._transport()
        sleeps = []
        with mock.patch.object(daemon.time, "sleep", sleeps.append):
            daemon._write_frame(transport, [b"a", b"b", b"c"], 0.05)
        self.assertEqual(written, [b"a", b"b", b"c"])
        self.assertEqual(sleeps, [0.05, 0.05, 0.05])

    def test_zero_delay_sends_back_to_back(self):
        transport, written = self._transport()
        sleeps = []
        with mock.patch.object(daemon.time, "sleep", sleeps.append):
            daemon._write_frame(transport, [b"a", b"b"], 0.0)
        self.assertEqual(written, [b"a", b"b"])
        self.assertEqual(sleeps, [])

    def test_default_delay_matches_the_vendor_cadence(self):
        args = daemon.build_parser().parse_args(["once"])
        self.assertAlmostEqual(args.packet_delay, 0.03)
        # 31 packets at 30 ms is about a second per frame, same as the client.
        self.assertLess(abs(31 * args.packet_delay - 0.93), 0.05)


class OnlyFilterTest(unittest.TestCase):
    """`--only` bisects which packets the panel actually consumes."""

    def _packets(self):
        from subscreen_protocol import build_full_sequence, PacketBuilder
        return build_full_sequence(daemon._pattern_snapshot(), PacketBuilder(0))

    def test_all_keeps_everything(self):
        packets = self._packets()
        self.assertEqual(daemon._filter_packets(packets, "all"), packets)

    def test_page1_is_a_single_packet(self):
        selected = daemon._filter_packets(self._packets(), "page1")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][5], 0x10)
        self.assertEqual(selected[0][6], 1)

    def test_page2_is_a_single_packet(self):
        selected = daemon._filter_packets(self._packets(), "page2")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][5], 0x10)
        self.assertEqual(selected[0][6], 2)

    def test_items_are_the_zero_eleven_packets(self):
        selected = daemon._filter_packets(self._packets(), "items")
        self.assertTrue(selected)
        self.assertTrue(all(packet[5] == 0x11 for packet in selected))
        # id, usage, temp, fan
        first = selected[0]
        self.assertEqual(first[6:10], bytes((0, 11, 12, 13)))

    def test_home_keeps_home_and_time_only(self):
        selected = daemon._filter_packets(self._packets(), "home")
        # The sequence carries a second Time packet at the end.
        self.assertEqual([packet[5] for packet in selected], [0x00, 0x01, 0x01])

    def test_unknown_fan_is_zero_not_255(self):
        # 0xFF has no sentinel on the panel: it prints "255%".
        self.assertEqual(daemon.FAN_UNKNOWN, 0)
        collector = daemon.StatsCollector(disk_paths=["/nonexistent"],
                                          ifaces=["lo"])
        collector._cpu_fan = lambda _now: daemon.NO_VALUE
        collector._gpus = lambda _now: (None, None)
        collector._cpu_usage = lambda: 5
        collector._cpu_temp = lambda: 40
        collector._mem_usage = lambda: 50
        collector._disks = lambda: []
        collector._networks = lambda _e: []
        collector._device_name_value = lambda: "x"
        collector._cpu_name_value = lambda: "y"
        collector._os_name_value = lambda: "z"
        collector._serial_value = lambda: "s"
        snapshot = collector.collect()
        self.assertEqual(snapshot.cpu_fan, 0)
        self.assertIn("fan=0%", "\n".join(daemon._report_lines(snapshot)))


class GpuDiscoveryTest(unittest.TestCase):
    """i915 moved its frequency knobs between kernels; paths are discovered."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpu-")
        self.card = os.path.join(self.tmp, "card0")
        self.device = os.path.join(self.card, "device")
        os.makedirs(self.device)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, relative, value):
        path = os.path.join(self.device, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as handle:
            handle.write(str(value))

    def test_gen12_rps_paths(self):
        self._write("gt/gt0/rps_cur_freq_mhz", 1050)
        self._write("gt/gt0/rps_min_freq_mhz", 300)
        self._write("gt/gt0/rps_max_freq_mhz", 1300)
        usage, source = daemon._gpu_usage(self.device, self.card)
        self.assertEqual(usage, 75)
        self.assertTrue(source.startswith("freq"))

    def test_xe_tile_paths(self):
        self._write("tile0/gt0/freq0/act_freq", 600)
        self._write("tile0/gt0/freq0/min_freq", 100)
        self._write("tile0/gt0/freq0/max_freq", 1100)
        usage, _source = daemon._gpu_usage(self.device, self.card)
        self.assertEqual(usage, 50)

    def test_amdgpu_busy_counter_wins(self):
        self._write("gpu_busy_percent", 63)
        self.assertEqual(daemon._gpu_usage(self.device, self.card),
                         (63, "busy:gpu_busy_percent"))

    def test_idle_gpu_reports_zero_not_twenty(self):
        self._write("gt/gt0/rps_cur_freq_mhz", 300)
        self._write("gt/gt0/rps_min_freq_mhz", 300)
        self._write("gt/gt0/rps_max_freq_mhz", 1300)
        self.assertEqual(daemon._gpu_usage(self.device, self.card)[0], 0)

    def test_no_counter_means_no_reading(self):
        self.assertEqual(daemon._gpu_usage(self.device, self.card),
                         (daemon.NO_VALUE, "no-counter"))

    def test_nested_hwmon_temperature(self):
        hwmon = os.path.join(self.device, "hwmon", "hwmon2")
        os.makedirs(hwmon)
        with open(os.path.join(hwmon, "temp1_input"), "w",
                  encoding="ascii") as handle:
            handle.write("47000")
        self.assertEqual(daemon._gpu_temp(self.device, self.card), 47)


class NvidiaSmiTest(unittest.TestCase):
    """sysfs knows the card exists but exposes no counters for it."""

    def _collector(self, output, returncode=0):
        collector = daemon.StatsCollector(disk_paths=[], ifaces=[])
        collector._nvidia_checked = True
        collector._nvidia_smi = "/usr/bin/nvidia-smi"
        result = types.SimpleNamespace(returncode=returncode, stdout=output,
                                       stderr="")
        return collector, mock.patch.object(daemon.subprocess, "run",
                                            return_value=result)

    def test_usage_temp_fan_come_from_smi(self):
        collector, patcher = self._collector("0, 38, 30\n")
        with patcher:
            reading = collector._nvidia_gpu()
        self.assertEqual((reading.usage, reading.temp, reading.fan), (0, 38, 30))
        self.assertEqual(reading.source, "nvidia-smi")
        self.assertEqual(reading.kind, "dgpu")

    def test_na_fields_are_not_readings(self):
        collector, patcher = self._collector("[N/A], 38, [N/A]\n")
        with patcher:
            reading = collector._nvidia_gpu()
        self.assertEqual(reading.usage, daemon.NO_VALUE)
        self.assertEqual(reading.temp, 38)
        self.assertEqual(reading.fan, daemon.NO_VALUE)

    def test_smi_wins_over_a_sysfs_shell(self):
        # _scan_gpus finds the NVIDIA card but cannot read it; the merged
        # reading must come from nvidia-smi instead.
        shell = daemon.GpuReading(kind="dgpu", vendor="10de",
                                  source="no-counter")
        collector, patcher = self._collector("42, 61, 55\n")
        with mock.patch.object(daemon, "_scan_gpus",
                               return_value=(None, shell)), patcher:
            igpu, dgpu = collector._gpus(time.time())
        self.assertEqual(dgpu.usage, 42)
        self.assertEqual(dgpu.temp, 61)
        self.assertEqual(dgpu.source, "nvidia-smi")

    def test_failure_backs_off_instead_of_respawning(self):
        collector, patcher = self._collector("", returncode=9)
        collector._gpu_next = 0
        with mock.patch.object(daemon, "_scan_gpus",
                               return_value=(None, None)), patcher as run:
            collector._gpus(time.time() + 100)
            collector._gpus(time.time() + 101)
        self.assertEqual(run.call_count, 1)


class CardOrderCliTest(unittest.TestCase):
    def _args(self, spec):
        return types.SimpleNamespace(card_order=spec)

    def test_reorders_and_keeps_every_card(self):
        snapshot = daemon._pattern_snapshot()
        daemon._apply_card_order(snapshot, self._args("mem,cpu"))
        self.assertEqual(snapshot.card_order, (2, 0, 1, 3))

    def test_unknown_name_is_rejected(self):
        snapshot = daemon._pattern_snapshot()
        with self.assertRaises(SystemExit):
            daemon._apply_card_order(snapshot, self._args("mem,tpu"))

    def test_duplicate_is_rejected(self):
        snapshot = daemon._pattern_snapshot()
        with self.assertRaises(SystemExit):
            daemon._apply_card_order(snapshot, self._args("cpu,cpu"))

    def test_no_spec_leaves_the_default(self):
        snapshot = daemon._pattern_snapshot()
        daemon._apply_card_order(snapshot, self._args(None))
        self.assertEqual(snapshot.card_order, (0, 1, 2, 3))

    def test_report_lines_follow_the_slot_order(self):
        snapshot = daemon._pattern_snapshot()
        daemon._apply_card_order(snapshot, self._args("mem,cpu,dgpu,igpu"))
        lines = "\n".join(daemon._sys_lines(snapshot))
        self.assertLess(lines.index("Memory"), lines.index("CPU"))
        self.assertIn("sys[0] Memory", lines)


class PanelStatusFrameTest(unittest.TestCase):
    """A5 5A <seq> 04 02 <page>: the panel's own status tick."""

    def _handle(self, raw, verbose):
        info = daemon.parse_packet(raw)
        info["raw"] = raw
        args = types.SimpleNamespace(allow_hibernate=False)
        state = {}
        with mock.patch.object(daemon, "_log") as log, \
                mock.patch.object(daemon, "_VERBOSE", verbose):
            daemon._handle_incoming(info, args, state)
        return log

    def test_status_frame_is_quiet_by_default(self):
        log = self._handle(bytes.fromhex("A55A2504020A"), verbose=False)
        messages = [c.args[0] for c in log.call_args_list]
        self.assertFalse(any("unexpected" in m for m in messages))

    def test_status_frame_is_named_when_verbose(self):
        log = self._handle(bytes.fromhex("A55A2504020A"), verbose=True)
        messages = [c.args[0] for c in log.call_args_list]
        self.assertTrue(any("status page=0x0A" in m for m in messages))

    def test_real_unknown_command_still_warns(self):
        log = self._handle(bytes.fromhex("A55A01040577"), verbose=False)
        messages = [c.args[0] for c in log.call_args_list]
        self.assertTrue(any("unexpected command=0x05" in m for m in messages))


class ResetPortTest(unittest.TestCase):
    """USBDEVFS_RESET is the only way back from a wedged panel."""

    def test_reset_ioctl_number(self):
        self.assertEqual(daemon.USBDEVFS_RESET, 0x5514)

    def test_reset_port_uses_the_open_fd(self):
        transport = types.SimpleNamespace(_fd=7)
        with mock.patch.object(daemon.fcntl, "ioctl") as ioctl:
            self.assertTrue(daemon._reset_port(transport))
        self.assertEqual(ioctl.call_args.args[0], 7)
        self.assertEqual(ioctl.call_args.args[1], daemon.USBDEVFS_RESET)

    def test_reset_port_reports_failure_without_raising(self):
        transport = types.SimpleNamespace(_fd=7)
        with mock.patch.object(daemon.fcntl, "ioctl",
                               side_effect=OSError(errno.EINVAL, "nope")):
            self.assertFalse(daemon._reset_port(transport))

    def test_reset_port_is_a_no_op_when_closed(self):
        self.assertFalse(daemon._reset_port(types.SimpleNamespace(_fd=None)))


class RunPaceTest(unittest.TestCase):
    """The panel drains ~3 packets a second; `run` must not outrun it."""

    def _args(self, pace=None, delay=0.03):
        return types.SimpleNamespace(pace=pace, packet_delay=delay)

    def test_default_pace_matches_the_panel(self):
        self.assertEqual(daemon._run_pace(self._args()), daemon.RUN_PACE)
        self.assertAlmostEqual(daemon.RUN_PACE, 1.5)

    def test_explicit_pace_wins(self):
        self.assertEqual(daemon._run_pace(self._args(pace=0.05)), 0.05)

    def test_a_slower_packet_delay_is_never_speeded_up(self):
        self.assertEqual(daemon._run_pace(self._args(delay=2.5)), 2.5)

    def test_zero_pace_is_allowed_for_probing(self):
        self.assertEqual(daemon._run_pace(self._args(pace=0)), 0.0)

    def test_default_pace_is_the_verified_safe_value(self):
        # 1.5 s per packet is the fastest rate confirmed not to wedge the panel.
        self.assertGreaterEqual(daemon.RUN_PACE, 1.5)


class LeanFrameTest(unittest.TestCase):
    """Static pages are re-sent rarely; the panel's bandwidth is scarce."""

    def _packets(self):
        from subscreen_protocol import build_full_sequence, PacketBuilder
        return build_full_sequence(daemon._pattern_snapshot(),
                                   PacketBuilder(0))

    def test_full_frame_keeps_everything(self):
        packets = self._packets()
        self.assertEqual(daemon._lean_frame(packets, True), packets)

    def test_lean_frame_drops_only_static_pages(self):
        packets = self._packets()
        lean = daemon._lean_frame(packets, False)
        aims = {p[5] for p in lean}
        self.assertFalse(aims & set(daemon.STATIC_AIMS))
        self.assertTrue({0x10, 0x11, 0x62} <= aims)

    def test_each_tier_fires_on_its_own_cadence(self):
        packets = self._packets()
        cadence = daemon.DEFAULT_CADENCE

        def aims(passes):
            trimmed = daemon._lean_frame(packets, False, passes=passes,
                                         cadence=cadence)
            return {p[5] for p in trimmed}

        self.assertIn(0x11, aims(1))        # fast: every frame
        self.assertNotIn(0x01, aims(1))     # medium: every 2nd
        self.assertIn(0x01, aims(2))
        self.assertNotIn(0x51, aims(2))     # slow: every 4th
        self.assertIn(0x51, aims(4))
        self.assertNotIn(0x00, aims(4))     # static: every 12th
        self.assertIn(0x00, aims(12))

    def test_blank_adapter_slots_ride_the_static_tier(self):
        packets = self._packets()
        blanks = [p for p in packets if p[5] == 0x60
                  and not p[22:42].strip(b"\x00")]
        self.assertTrue(blanks)
        for packet in blanks:
            self.assertEqual(daemon._tier_of(packet), daemon.TIER_STATIC)

    def test_cadence_can_be_overridden_from_the_cli(self):
        args = types.SimpleNamespace(every_fast=3, every_medium=None,
                                     every_slow=None, every_static=None)
        cadence = daemon._cadence_of(args)
        self.assertEqual(cadence[daemon.TIER_FAST], 3)
        self.assertEqual(cadence[daemon.TIER_STATIC],
                         daemon.DEFAULT_CADENCE[daemon.TIER_STATIC])

    def test_cadence_never_goes_below_one(self):
        args = types.SimpleNamespace(every_fast=0, every_medium=-4,
                                     every_slow=None, every_static=None)
        cadence = daemon._cadence_of(args)
        self.assertEqual(cadence[daemon.TIER_FAST], 1)
        self.assertEqual(cadence[daemon.TIER_MEDIUM], 1)

    def test_lean_frame_is_substantially_smaller(self):
        packets = self._packets()
        self.assertLess(len(daemon._lean_frame(packets, False)),
                        len(packets) * 0.8)

    def test_full_frame_returns_every_twelve_passes(self):
        self.assertEqual(daemon.LEAN_FULL_EVERY, 12)

    def test_slow_pass_keeps_disk_pages(self):
        packets = self._packets()
        live = daemon._lean_frame(packets, False, slow=False)
        slow = daemon._lean_frame(packets, False, slow=True)
        self.assertFalse({0x50, 0x51} & {p[5] for p in live})
        self.assertTrue({0x50} <= {p[5] for p in slow})
        self.assertGreater(len(slow), len(live))

    def test_slow_cadence_is_every_fourth_pass(self):
        self.assertEqual(daemon.LEAN_SLOW_EVERY, 4)


class NetRateSamplerTest(unittest.TestCase):
    """The displayed rate is a one-second sample, not a frame-long average."""

    def _collector(self):
        collector = daemon.StatsCollector(disk_paths=[], ifaces=["br0"])
        collector._net_rate = {"br0": (12.0, 340.0)}
        collector._net_rate_at = {"br0": time.monotonic()}
        return collector

    def test_fresh_sample_is_used(self):
        self.assertEqual(self._collector()._net_rate_for("br0"), (12, 340))

    def test_stale_sample_is_ignored(self):
        collector = self._collector()
        collector._net_rate_at["br0"] = time.monotonic() - 60
        self.assertIsNone(collector._net_rate_for("br0"))

    def test_unknown_interface_has_no_sample(self):
        self.assertIsNone(self._collector()._net_rate_for("eth9"))

    def test_smoothed_rate_never_jumps_to_a_single_spike(self):
        collector = daemon.StatsCollector(disk_paths=[], ifaces=["br0"])
        collector._net_rate = {"br0": (10.0, 10.0)}
        collector._net_rate_at = {}
        previous = {"br0": (0, 0)}
        now = time.monotonic()
        with mock.patch.object(daemon, "_read_net_dev",
                               return_value={"br0": (27 * 1024 ** 2, 27 * 1024 ** 2)}), \
                mock.patch.object(daemon.time, "monotonic", return_value=now + 1), \
                mock.patch.object(daemon.time, "sleep", return_value=None):
            collector._net_sampler = None
            thread_state = {"done": False}

            def one_round():
                up = 27 * 1024 ** 2 / 1.0 / 1024
                prev = collector._net_rate["br0"]
                smoothed = prev[0] + (up - prev[0]) * daemon.NET_RATE_ALPHA
                self.assertLess(smoothed, 27 * 1024)
                thread_state["done"] = True
            one_round()
        self.assertTrue(thread_state["done"])
        self.assertEqual(previous["br0"], (0, 0))


class ConfigFileTest(unittest.TestCase):
    """The plugin's INI file supplies defaults; CLI arguments still win."""

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False,
                                             encoding="utf-8")
        handle.write(textwrap.dedent(text))
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_reads_values_and_ignores_noise(self):
        path = self._write("""\
            ; a comment
            [subscreen]
            pace=2.2
            layout=paired
            card_order=mem,cpu,dgpu,igpu
            disks=/mnt/disk1, /mnt/cache
            unknown_key=whatever
            broken line without equals
            """)
        defaults = daemon._config_defaults(path)
        self.assertEqual(defaults["pace"], 2.2)
        self.assertEqual(defaults["layout"], "paired")
        self.assertEqual(defaults["card_order"], "mem,cpu,dgpu,igpu")
        self.assertEqual(defaults["disks"], ["/mnt/disk1", "/mnt/cache"])

    def test_values_are_clamped(self):
        path = self._write("""\
            pace=99
            every_fast=0
            wlan_slots=9
            """)
        defaults = daemon._config_defaults(path)
        self.assertEqual(defaults["pace"], 5.0)
        self.assertEqual(defaults["every_fast"], 1)
        self.assertEqual(defaults["wlan_slots"], 3)

    def test_invalid_choice_falls_back_to_default(self):
        path = self._write("""\
            layout=nonsense
            system_mode=whatever
            """)
        defaults = daemon._config_defaults(path)
        self.assertNotIn("layout", defaults)
        self.assertNotIn("system_mode", defaults)

    def test_auto_syscount_means_no_override(self):
        path = self._write("syscount=auto\n")
        self.assertIsNone(daemon._config_defaults(path)["syscount"])

    def test_source_toggles_map_to_negative_flags(self):
        path = self._write("""\
            gpu=0
            gpu_igpu=0
            nvidia_smi=0
            temp_smartctl=0
            temp_disksini=0
            wake_disks=1
            """)
        defaults = daemon._config_defaults(path)
        self.assertTrue(defaults["no_gpu"])
        self.assertTrue(defaults["no_igpu"])
        self.assertTrue(defaults["no_nvidia_smi"])
        self.assertTrue(defaults["no_smartctl_temp"])
        self.assertTrue(defaults["no_disksini_temp"])
        self.assertTrue(defaults["wake_disks"])

    def test_missing_file_is_harmless(self):
        self.assertEqual(daemon._config_defaults("/nonexistent/x.cfg"), {})

    def test_config_path_is_found_in_argv(self):
        self.assertEqual(
            daemon._config_path_from_argv(["run", "--config", "/tmp/a.cfg"]),
            "/tmp/a.cfg")
        self.assertEqual(
            daemon._config_path_from_argv(["run", "--config=/tmp/b.cfg"]),
            "/tmp/b.cfg")
        self.assertEqual(daemon._config_path_from_argv(["run"]),
                         daemon.CONFIG_PATH_DEFAULT)

    def test_cli_beats_the_config_file(self):
        path = self._write("pace=3.0\nlayout=paired\n")
        parser = daemon.build_parser()
        parser.set_defaults(**daemon._config_defaults(path))
        args = parser.parse_args(["run", "--pace", "1.2"])
        self.assertEqual(args.pace, 1.2)          # explicit CLI wins
        self.assertEqual(args.layout, "paired")   # config fills the rest

    def test_config_supplies_defaults_when_cli_is_silent(self):
        path = self._write("pace=3.0\nevery_slow=9\n")
        parser = daemon.build_parser()
        parser.set_defaults(**daemon._config_defaults(path))
        args = parser.parse_args(["run"])
        self.assertEqual(args.pace, 3.0)
        self.assertEqual(args.every_slow, 9)


class CollectorSwitchesTest(unittest.TestCase):
    """The settings page's toggles must actually reach the collector."""

    def _collector(self, **flags):
        args = types.SimpleNamespace(**{
            "no_gpu": False, "no_igpu": False, "no_dgpu": False,
            "no_nvidia_smi": False, "no_hwmon_temp": False,
            "no_smartctl_temp": False, "no_disksini_temp": False,
            "wake_disks": False, "disks": [], "ifaces": [],
            "wlan_total_unit": "auto",
        })
        for key, value in flags.items():
            setattr(args, key, value)
        return daemon._make_collector(args)

    def test_defaults_enable_every_source(self):
        collector = self._collector()
        self.assertTrue(collector.temp_hwmon)
        self.assertTrue(collector.temp_smartctl)
        self.assertTrue(collector.temp_disksini)
        self.assertTrue(collector.use_nvidia_smi)
        self.assertFalse(collector.wake_disks)

    def test_flags_turn_sources_off(self):
        collector = self._collector(no_hwmon_temp=True, no_nvidia_smi=True,
                                    wake_disks=True, no_dgpu=True)
        self.assertFalse(collector.temp_hwmon)
        self.assertFalse(collector.use_nvidia_smi)
        self.assertTrue(collector.wake_disks)
        self.assertFalse(collector.use_dgpu)

    def test_disabling_both_expensive_tiers_leaves_the_fallback(self):
        collector = self._collector(no_hwmon_temp=True, no_smartctl_temp=True)
        collector._smart_temp = mock.Mock(return_value=(40, "smartctl:sdb"))
        collector._block_temp_values = mock.Mock(return_value=[41])
        collector.temp_disksini = True
        value, source = collector._disk_temp_detail("/dev/sdb", "disk1",
                                                    table={"disk1": {"temp": "38"}})
        self.assertEqual((value, source), (38, "unraid:disk1"))
        collector._smart_temp.assert_not_called()
        collector._block_temp_values.assert_not_called()

    def test_wake_flag_reaches_smartctl(self):
        class Result:
            returncode = 0
            stdout = '{"temperature": {"current": 33}}'
            stderr = ""

        seen = []

        def fake_run(command, **kwargs):
            seen.append(command)
            return Result()

        with mock.patch.object(daemon, "_find_smartctl",
                               return_value="/usr/sbin/smartctl"), \
                mock.patch.object(daemon.subprocess, "run", fake_run):
            daemon._query_smart_temp("sdb", wake=False)
            daemon._query_smart_temp("sdb", wake=True)
        self.assertIn("-n", seen[0])
        self.assertNotIn("-n", seen[1])
