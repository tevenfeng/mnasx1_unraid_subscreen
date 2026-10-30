#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Golden-vector tests for the reverse-engineered Subscreen protocol."""

import os
import struct
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import subscreen_protocol as protocol  # noqa: E402
from subscreen_protocol import (
    build_full_sequence,  # noqa: E402
    AIM_DISK,
    AIM_HOME,
    AIM_INFO,
    AIM_MODE,
    AIM_SYSTEM,
    AIM_TIME,
    AIM_WLAN,
    DiskItem,
    PacketBuilder,
    SystemItem,
    SystemSnapshot,
    WlanItem,
    build_full_sequence,
    checksum,
    encode_disk_size,
    encode_wlan_total,
    encode_wlan_speed,
    panel_timestamp,
    parse_packet,
    selftest,
    utc_offset,
)

TS = 0x11223344


def hx(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


class ProtocolTest(unittest.TestCase):
    def test_home_golden(self):
        # Windows selftest: total 13, len 0x0A, checksum = sum(first 12)
        pkt = PacketBuilder(0).home(TS)
        self.assertEqual(pkt, hx("A5 5A 00 0A 02 00 01 01 44 33 22 11 B7"))

    def test_time_golden(self):
        pkt = PacketBuilder(1).time(TS)
        self.assertEqual(pkt, hx("A5 5A 01 08 02 01 44 33 22 11 B5"))

    def test_system_page_golden(self):
        pkt = PacketBuilder(3).system_page(1, 2, 3, [
            SystemItem(0, 0x2A, 0x37, 0xFE, "CPU"),
            SystemItem(1, 0x00, 0xFF, 0xFE, "iGPU"),
        ])
        self.assertEqual(
            pkt,
            hx("A5 5A 03 22 02 10 01 02 03 02 "
               "0D 00 2A 37 FE 43 50 55 00 00 00 00 00 "
               "0D 01 00 FF FE 69 47 50 55 00 00 00 00 F2"),
        )

    def test_disk_page_golden(self):
        # Capacities are GB-based: 12000 GB is promoted to 11 TB (unit 0x33),
        # 500 GB stays GB (unit 0x22).
        pkt = PacketBuilder(5).disk_page(1, 1, 2, [
            DiskItem(0, 12000, 8000, 41, "disk1"),
            DiskItem(1, 500, 200, 0xFF, "cache"),
        ])
        self.assertEqual(
            pkt,
            hx("A5 5A 05 3A 02 50 01 01 02 02 "
               "19 00 33 00 0B 00 07 00 29 64 69 73 6B 31 00 00 00 00 00 00 00 00 00 00 00 "
               "19 01 22 00 F4 01 C8 00 FF 63 61 63 68 65 00 00 00 00 00 00 00 00 00 00 00 E5"),
        )

    def test_wlan_page_golden(self):
        pkt = PacketBuilder(6).wlan_page(
            1, 1, 1, WlanItem(0, 1200, 3400, 12345, (192, 168, 1, 10), "br0"))
        self.assertEqual(
            pkt,
            hx("A5 5A 06 28 02 60 01 01 01 01 01 1F 00 11 01 00 03 00 "
               "C0 A8 01 0A 62 72 30 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3F"),
        )

    def test_mode_page_golden(self):
        pkt = PacketBuilder(7).mode_page()
        self.assertEqual(pkt, hx("A5 5A 07 0A 02 70 01 01 03 03 01 00 8B"))

    def test_info_page_layout(self):
        pkt = PacketBuilder(8).info_page(1, 4, "Unraid")
        self.assertEqual(len(pkt), 60)
        self.assertEqual(pkt[3], 0x39)
        self.assertEqual(pkt[6:11], bytes((1, 4, 0, 0, 6)))
        self.assertEqual(pkt[11:17], b"Unraid")
        self.assertEqual(pkt[-1], checksum(pkt[:-1]))
        self.assertEqual(pkt[5], AIM_INFO)

    def test_all_packet_lengths(self):
        snap = SystemSnapshot(
            timestamp=TS, cpu_usage=10, cpu_temp=40, mem_usage=20,
            disks=[DiskItem(0, 1000, 500, 0xFF, "disk1")],
            networks=[WlanItem(0, 1, 2, 3, (10, 0, 0, 1), "eth0")],
        )
        packets = build_full_sequence(snap, PacketBuilder(0))
        allowed = {9, 11, 12, 13, 14, 37, 43, 60, 61}
        for pkt in packets:
            self.assertIn(len(pkt), allowed)
            self.assertEqual(pkt[0:2], b"\xA5\x5A")
            self.assertEqual(pkt[3], len(pkt) - 3)
            self.assertEqual(pkt[-1], checksum(pkt[:-1]))
            parsed = parse_packet(pkt)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["aim"], pkt[5])
            self.assertTrue(parsed["crc_ok"])

    def test_unit_encoding(self):
        # Disk capacities are GB-based (unit nibble 2 = GB, 3 = TB, 4 = PB).
        self.assertEqual(encode_disk_size(100, 50), (0x22, 100, 50))
        self.assertEqual(encode_disk_size(4000, 4000), (0x22, 4000, 4000))
        self.assertEqual(encode_disk_size(12000, 8000), (0x33, 11, 7))
        self.assertEqual(encode_disk_size(4001, 4001), (0x33, 3, 3))
        # Network speeds stay KB-based (unit nibble 0 = KB/s, max 3).
        self.assertEqual(encode_wlan_speed(100, 200), (0x00, 100, 200))
        self.assertEqual(encode_wlan_speed(1200, 3400), (0x11, 1, 3))

    def test_selftest(self):
        self.assertTrue(selftest(verbose=False))

    def test_wlan_total_scales_instead_of_saturating(self):
        # The panel renders unit 0 as KB, so the vendor's "always 0, saturate
        # at 0xFFFF" freezes at 64 MB. Scale it like the speed field.
        self.assertEqual(encode_wlan_total(0), (0, 0))
        self.assertEqual(encode_wlan_total(500), (0, 500))
        self.assertEqual(encode_wlan_total(1500), (1, 1))
        self.assertEqual(encode_wlan_total(2048), (1, 2))
        self.assertEqual(encode_wlan_total(8 * 1024 ** 3), (3, 8))
        self.assertEqual(encode_wlan_total(10 ** 15), (3, 0xFFFF))

    def test_wlan_total_unit_can_be_pinned(self):
        eight_tb_kb = 8 * 1024 ** 3
        self.assertEqual(encode_wlan_total(eight_tb_kb, unit=2), (2, 8192))
        self.assertEqual(encode_wlan_total(eight_tb_kb, unit=0), (0, 0xFFFF))
        self.assertEqual(encode_wlan_total(2048, unit=0), (0, 2048))

    def test_wlan_total_packet_golden(self):
        net = WlanItem(0, 0, 0, total_kb=8 * 1024 ** 3, name="br0")
        pkt = PacketBuilder(9).wlan_total(net)
        self.assertEqual(pkt,
                         hx("A5 5A 09 08 02 64 00 03 08 00 81"))
        self.assertEqual(pkt[6], 0)
        self.assertEqual(pkt[7], 3)          # unit byte: TB
        self.assertEqual(pkt[8:10], b"\x08\x00")

    def test_aim_constants(self):
        self.assertEqual(
            (AIM_HOME, AIM_TIME, AIM_SYSTEM, AIM_DISK, AIM_WLAN, AIM_MODE),
            (0x00, 0x01, 0x10, 0x50, 0x60, 0x70),
        )


class TimezoneTest(unittest.TestCase):
    """The panel renders the timestamp as UTC, so the wire carries the local
    wall clock expressed as epoch seconds."""

    def test_panel_timestamp_is_local_wall_clock(self):
        moment = 1700000000.0
        wire = panel_timestamp(moment)
        self.assertEqual(wire, int(moment) + time.localtime(moment).tm_gmtoff)
        self.assertEqual(
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(wire)),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment)))

    def test_utc_offset_is_dst_aware(self):
        moment = 1700000000.0
        self.assertEqual(utc_offset(moment),
                         int(time.localtime(moment).tm_gmtoff))

    def test_default_packets_use_panel_timestamp(self):
        with mock.patch.object(protocol, "panel_timestamp",
                               lambda: 1793143346):
            home = PacketBuilder(0).home()
            clock = PacketBuilder(0).time()
        self.assertEqual(struct.unpack_from("<I", home, 8)[0], 1793143346)
        self.assertEqual(struct.unpack_from("<I", clock, 6)[0], 1793143346)

    def _offset_in_tz(self, tz):
        code = ("import sys; sys.path.insert(0, %r);"
                "import subscreen_protocol as p;"
                "print(p.panel_timestamp(1700000000) - 1700000000)" % ROOT)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                env=dict(os.environ, TZ=tz),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout.strip())

    def test_offset_matches_each_timezone(self):
        # 1700000000 is in November, so none of these zones is on DST.
        self.assertEqual(self._offset_in_tz("UTC"), 0)
        self.assertEqual(self._offset_in_tz("Asia/Shanghai"), 8 * 3600)
        self.assertEqual(self._offset_in_tz("America/New_York"), -5 * 3600)
        self.assertEqual(self._offset_in_tz("Asia/Kolkata"), 5 * 3600 + 1800)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SystemPageLayoutTest(unittest.TestCase):
    """One card per SystemPage; two blocks per page never survive.

    The panel repaints the first block into the second slot, so slot 2 mirrors
    slot 1 whatever the second block holds. One card per page is the only
    layout that fills slots independently.
    """

    def _snapshot(self, dgpu=False):
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=11, cpu_temp=12,
                                  cpu_fan=13, mem_usage=31, mem_temp=41,
                                  mem_fan=51, igpu_usage=21, igpu_temp=22,
                                  igpu_fan=23)
        if dgpu:
            snapshot.dgpu_present = True
            snapshot.dgpu_usage = 55
            snapshot.dgpu_temp = 61
            snapshot.dgpu_fan = 70
        return snapshot

    def _pages(self, dgpu=False, **kwargs):
        snapshot = self._snapshot(dgpu)
        kwargs.setdefault("syscount", 4 if dgpu else 3)
        kwargs.setdefault("layout", "per-page")
        packets = build_full_sequence(snapshot, PacketBuilder(0), **kwargs)
        return [p for p in packets if p[5] == 0x10]

    def test_one_card_per_page(self):
        pages = self._pages(dgpu=True)
        self.assertEqual(len(pages), 4)
        for order, page in enumerate(pages, 1):
            self.assertEqual(page[6], order)   # order
            self.assertEqual(page[7], 4)       # total
            self.assertEqual(page[8], 4)       # syscount
            self.assertEqual(page[9], 1)       # one item

    def test_no_dgpu_gives_three_pages(self):
        pages = self._pages(dgpu=False)
        self.assertEqual(len(pages), 3)
        self.assertEqual([p[8] for p in pages], [3, 3, 3])
        self.assertEqual([p[11] for p in pages], [0, 1, 2])

    def test_unmeasurable_gpu_cards_are_dropped(self):
        # Neither a counter nor a sensor: drawing a permanent "0% / --" card is
        # worse than drawing no card at all.
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=10, cpu_temp=50,
                                  mem_usage=60)
        snapshot.dgpu_present = True
        pages = [p for p in build_full_sequence(snapshot, PacketBuilder(0),
                                                syscount=4, layout="per-page")
                 if p[5] == 0x10]
        self.assertEqual([p[11] for p in pages], [0, 2])
        self.assertEqual([p[7] for p in pages], [2, 2])

    def test_each_page_carries_its_own_card(self):
        pages = self._pages(dgpu=True)
        self.assertEqual([p[11] for p in pages], [0, 1, 2, 3])   # id bytes
        self.assertEqual(pages[0][12:15], bytes((11, 12, 13)))   # cpu
        self.assertEqual(pages[2][12:15], bytes((31, 41, 51)))   # memory
        self.assertEqual(pages[3][12:15], bytes((55, 61, 70)))   # dgpu

    def test_paired_layout_is_still_available(self):
        pages = self._pages(dgpu=True, layout="paired")
        self.assertEqual(len(pages), 2)
        self.assertEqual([p[9] for p in pages], [2, 2])

    def test_unknown_usage_never_ships_as_255(self):
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=5, mem_usage=50)
        packets = build_full_sequence(snapshot, PacketBuilder(0))
        for packet in packets:
            if packet[5] == 0x11:
                self.assertNotEqual(packet[7], 0xFF)


class BothChannelsTest(unittest.TestCase):
    """Cards read different channels, so real values go out on both."""

    def _build(self, mode="both"):
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=37, cpu_temp=50,
                                  cpu_fan=0, mem_usage=65, mem_temp=41,
                                  mem_fan=0)
        return build_full_sequence(snapshot, PacketBuilder(0),
                                   system_mode=mode, layout="paired")

    def test_both_puts_values_in_pages_and_items(self):
        packets = self._build()
        pages = [p for p in packets if p[5] == 0x10]
        self.assertEqual(pages[0][12:15], bytes((37, 50, 0)))
        items = [p for p in packets if p[5] == 0x11]
        self.assertEqual(items[0][6:10], bytes((0, 37, 50, 0)))

    def test_both_carries_memory_usage_in_the_page_block(self):
        page1 = [p for p in self._build() if p[5] == 0x10][0]
        # paired layout: block 2 of page 1 is the Memory card (id 2).
        self.assertEqual(page1[24], 2)      # id of block 2
        self.assertEqual(page1[25], 65)     # usage of block 2

    def test_vendor_mode_blanks_the_page_blocks(self):
        pages = [p for p in self._build("vendor") if p[5] == 0x10]
        self.assertEqual(pages[0][12:15], bytes((0, 0xFF, 0xFE)))


class CardOrderTest(unittest.TestCase):
    """Cards move between slots; their ids (and so their values) stay put."""

    def _pages(self, order):
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=11, cpu_temp=12,
                                  mem_usage=31, mem_temp=41, card_order=order)
        packets = build_full_sequence(snapshot, PacketBuilder(0), syscount=3,
                                      layout="paired")
        return [p for p in packets if p[5] == 0x10]

    def test_default_order(self):
        pages = self._pages((0, 1, 2, 3))
        # paired layout: page 1 holds CPU (id 0) then Memory (id 2), because
        # the iGPU card is unreadable here and gets dropped.
        self.assertEqual([pages[0][11], pages[0][24]], [0, 2])

    def test_memory_can_lead(self):
        pages = self._pages((2, 0, 1, 3))
        self.assertEqual([pages[0][11], pages[0][24]], [2, 0])
        self.assertEqual(pages[0][6], 1)                    # order byte
        self.assertEqual(pages[0][12:15], bytes((31, 41, 0)))

    def test_unlisted_cards_keep_their_relative_order(self):
        pages = self._pages((3,))
        self.assertEqual([pages[0][11], pages[0][24]], [0, 2])

    def test_per_page_layout_moves_whole_pages(self):
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=11, cpu_temp=12,
                                  mem_usage=31, mem_temp=41,
                                  card_order=(2, 0, 1, 3))
        pages = [p for p in build_full_sequence(snapshot, PacketBuilder(0),
                                                syscount=3, layout="per-page")
                 if p[5] == 0x10]
        self.assertEqual([p[11] for p in pages], [2, 0])


class PairedLayoutHeaderTest(unittest.TestCase):
    """syscount is the card count, total is the page count."""

    def _pages(self, **kwargs):
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=37, cpu_temp=50,
                                  cpu_fan=0, igpu_usage=12, igpu_temp=43,
                                  igpu_fan=0, mem_usage=65, mem_temp=0xFF,
                                  mem_fan=0)
        snapshot.dgpu_present = True
        snapshot.dgpu_usage = 3
        snapshot.dgpu_temp = 38
        snapshot.dgpu_fan = 30
        kwargs.setdefault("layout", "paired")
        packets = build_full_sequence(snapshot, PacketBuilder(0), **kwargs)
        return [p for p in packets if p[5] == 0x10]

    def test_two_pages_four_cards(self):
        pages = self._pages(syscount=4)
        self.assertEqual([(p[6], p[7], p[8], p[9]) for p in pages],
                         [(1, 2, 4, 2), (2, 2, 4, 2)])

    def test_never_more_than_two_pages(self):
        # The panel only ever accepted total=2; more locks its UI up.
        pages = self._pages(syscount=4)
        self.assertTrue(all(p[7] <= 2 for p in pages))


class WlanSlotTest(unittest.TestCase):
    """Unused adapter slots must be overwritten, not left stale."""

    def _pages(self, networks, **kwargs):
        snapshot = SystemSnapshot(timestamp=1, cpu_usage=10, cpu_temp=50,
                                  mem_usage=60)
        snapshot.networks = networks
        packets = build_full_sequence(snapshot, PacketBuilder(0), **kwargs)
        return [p for p in packets if p[5] == 0x60]

    def test_single_nic_fills_three_slots(self):
        pages = self._pages([WlanItem(0, 44, 55, 66 * 1024,
                                      (192, 168, 1, 2), "br0")])
        self.assertEqual(len(pages), 3)
        self.assertEqual([p[6] for p in pages], [1, 2, 3])   # order
        self.assertEqual([p[7] for p in pages], [3, 3, 3])   # total
        self.assertEqual([p[12] for p in pages], [0, 1, 2])  # net id

    def test_placeholder_slots_carry_zero_traffic_and_no_ip(self):
        pages = self._pages([WlanItem(0, 44, 55, 66 * 1024,
                                      (192, 168, 1, 2), "br0")])
        for page in pages[1:]:
            self.assertEqual(page[13:18], bytes(5))          # unit/up/down = 0
            self.assertEqual(page[18:22], bytes(4))          # ip = 0.0.0.0
            self.assertEqual(page[22:34], bytes(12))         # blank name

    def test_real_nic_keeps_its_values(self):
        pages = self._pages([WlanItem(0, 44, 55, 66 * 1024,
                                      (192, 168, 1, 2), "br0")])
        self.assertEqual(pages[0][18:22], bytes((192, 168, 1, 2)))
        self.assertEqual(pages[0][22:27], b"br0\x00\x00")

    def test_three_real_nics_are_untouched(self):
        nets = [WlanItem(i, 10 * i, 20 * i, 0, (10, 0, 0, i + 1), "eth%d" % i)
                for i in range(3)]
        pages = self._pages(nets)
        self.assertEqual(len(pages), 3)
        self.assertEqual([p[12] for p in pages], [0, 1, 2])

    def test_slot_count_can_be_overridden(self):
        pages = self._pages([WlanItem(0, 1, 2, 0, (1, 2, 3, 4), "br0")],
                            wlan_slots=1)
        self.assertEqual(len(pages), 1)
