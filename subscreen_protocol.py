#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HarborOS Subscreen USB protocol.

Reverse-engineered from SubscreenDrives.exe -> embedded subscreen.exe
(PE32+ x86-64, WinUSB, VID 5448 PID 0002).

Wire format (little-endian):

    offset  size  field
    0       2     magic      0x5AA5  -> bytes A5 5A
    2       1     sequence   incrementing byte, wraps at 256
    3       1     length     4 + len(data)   (sequence..last data byte)
    4       1     command    2 = page/state update, 3 = panel event, 4 = OTA
    5       1     aim        page / item id
    6       N     data
    6+N     1     checksum   (sum of all preceding bytes) & 0xFF

Total packet size = 7 + len(data) = length + 3.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

MAGIC = 0x5AA5
MAGIC_BYTES = b"\xA5\x5A"

CMD_UPDATE = 0x02           # host -> panel
CMD_EVENT = 0x03            # panel -> host (buttons / page selection)
CMD_OTA = 0x04              # panel -> host (OTA ack, firmware version)

# Primary pages (aim)
AIM_HOME = 0x00
AIM_TIME = 0x01
AIM_SYSTEM = 0x10
AIM_DISK = 0x50
AIM_WLAN = 0x60
AIM_MODE = 0x70
AIM_INFO = 0xA0

# Secondary / individual items (aim)
AIM_SYSTEM_ITEM = 0x11
AIM_DISK_ITEM = 0x51
AIM_WLAN_USER = 0x61
AIM_WLAN_SPEED = 0x62
AIM_WLAN_TOTAL = 0x64
AIM_WLAN_IP = 0x65
AIM_UPDATE_FW_INFO = 0xF0
AIM_GET_VERSION = 0xF1
AIM_UPDATE_FW = 0xF2

# Panel -> host button/page ids
EVENT_PAGES = {0x00, 0x10, 0x50, 0x60, 0xA0}
EVENT_MUTE = 0x71
EVENT_PERFORMANCE = 0x72
EVENT_BALANCED = 0x73
EVENT_HIBERNATE = 0x81
EVENT_HIBERNATE_2 = 0x82

MAX_SYSTEM_ITEMS = 2
MAX_DISKS_PER_PAGE = 2
MAX_INFO_NAME = 48
MAX_WLAN_NAME = 20
MAX_DISK_NAME = 16


def _u8(value: int) -> int:
    return int(value) & 0xFF


def _u16(value: int) -> int:
    return int(value) & 0xFFFF


def _u32(value: int) -> int:
    return int(value) & 0xFFFFFFFF


def _fixed_ascii(text: str, size: int) -> bytes:
    """Encode a display string as zero-padded ASCII, truncated to size."""
    raw = str(text or "").encode("ascii", "replace")
    return raw[:size].ljust(size, b"\x00")


def checksum(packet_without_crc: bytes) -> int:
    return sum(packet_without_crc) & 0xFF


def utc_offset(now: Optional[float] = None) -> int:
    """Local UTC offset in seconds, DST aware."""
    moment = time.time() if now is None else now
    try:
        return int(time.localtime(moment).tm_gmtoff)
    except (AttributeError, OSError, ValueError, OverflowError):
        return -int(time.timezone)


def panel_timestamp(now: Optional[float] = None) -> int:
    """Timestamp to put in the HomePage / Time payloads.

    The field is a Unix timestamp, but the panel firmware has no timezone
    and renders it as UTC. The Windows client sends a pure UTC epoch (it
    converts GetSystemTimeAsFileTime and never looks at the local bias), so
    on a UTC+8 host the panel would trail local time by eight hours. Send the
    local wall clock expressed as epoch seconds instead.
    """
    moment = time.time() if now is None else now
    return int(int(moment) + utc_offset(moment))


def local_time_text(now: Optional[float] = None) -> str:
    """Human readable local time plus the offset that is applied on the wire."""
    moment = time.time() if now is None else now
    offset = utc_offset(moment)
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(abs(offset) // 60, 60)
    return "%s UTC%s%02d:%02d" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment)),
        sign, hours, minutes,
    )


def build_packet(command: int, aim: int, data: bytes, sequence: int) -> bytes:
    """Build one wire packet including the trailing additive checksum."""
    if len(data) > 0xFF - 4:
        raise ValueError("payload too large")
    body = struct.pack(
        "<HBBBB",
        MAGIC,
        _u8(sequence),
        4 + len(data),
        _u8(command),
        _u8(aim),
    ) + data
    return body + bytes((checksum(body),))


def parse_packet(raw: bytes) -> Optional[dict]:
    """Parse a panel -> host packet. Returns None when the frame is invalid."""
    if len(raw) < 6:
        return None
    magic, seq, length, cmd, aim = struct.unpack_from("<HBBBB", raw, 0)
    if magic != MAGIC or length < 4:
        return None
    end = 2 + length
    if end > len(raw):
        return None
    data = raw[6:end]
    crc = raw[end] if end < len(raw) else None
    return {
        "sequence": seq,
        "length": length,
        "command": cmd,
        "aim": aim,
        "data": data,
        "crc": crc,
        "crc_ok": None if crc is None else crc == checksum(raw[:end]),
    }


# ---------------------------------------------------------------------------
# Size/speed unit encoding
# ---------------------------------------------------------------------------

def encode_disk_size(total_gb: int, used_gb: int) -> Tuple[int, int, int]:
    """Mirror the Windows encoder for DiskPage entries.

    Verified against subscreen.exe (0x140004ca2): the unit byte starts at
    0x22 and its low nibble is the scale for ``total``, the high nibble the
    scale for ``used``. The vendor collector feeds GiB (``total_bytes >> 30``)
    into the same struct, so nibble 2 = GB, 3 = TB, 4 = PB. Every value above
    4000 is shifted right by 10 and its nibble incremented, up to nibble 15;
    the result is truncated to u16.
    """
    total = max(0, int(total_gb))
    used = max(0, int(used_gb))
    unit = 0x22
    while total > 4000 and (unit & 0x0F) < 0x0F:
        total >>= 10
        unit += 1
    while used > 4000 and (unit & 0xF0) < 0xF0:
        used >>= 10
        unit += 0x10
    return unit & 0xFF, min(total, 0xFFFF), min(used, 0xFFFF)


DISK_UNIT_NAMES = {2: "GB", 3: "TB", 4: "PB", 5: "EB"}


def decode_disk_size(unit: int, total: int, used: int) -> str:
    """Human-readable rendering of an encoded disk size (for logs/tests)."""
    total_unit = DISK_UNIT_NAMES.get(unit & 0x0F, "?")
    used_unit = DISK_UNIT_NAMES.get((unit >> 4) & 0x0F, "?")
    return "%d%s / %d%s" % (total, total_unit, used, used_unit)


def encode_wlan_total(total_kb: int, unit: Optional[int] = None) -> Tuple[int, int]:
    """Mirror the Windows encoder for WlanTotal (aim 0x64).

    Returns (unit_byte, value_u16). The vendor client always writes unit 0 and
    saturates at 0xFFFF (0x1400069ba: ``mov $0x64,%r8b`` / ``movb $0x0,-0x72`` /
    ``mov $0xffff,%edx`` + ``cmova``), so any machine with more than 64 MB of
    traffic shows a frozen "65535KB" on the panel. That byte sits exactly where
    every other page keeps its unit, and the panel renders 0 as KB, so the
    value is scaled the same way as the speed field (0=KB, 1=MB, 2=GB, 3=TB).

    Pass ``unit`` to pin the scale when testing what the firmware honours.
    """
    value = max(0, int(total_kb))
    if unit is None:
        scale = 0
        while value > 0x400 and scale < 3:
            value >>= 10
            scale += 1
    else:
        scale = max(0, min(3, int(unit)))
        value >>= 10 * scale
    return scale, min(value, 0xFFFF)


def encode_wlan_speed(up_kbps: int, down_kbps: int) -> Tuple[int, int, int]:
    """Mirror the Windows encoder for WlanPage / WlanSpeed.

    Returns (unit_byte, up_u16, down_u16). High nibble scales up, low nibble
    scales down, starting at 0 (KB/s), max 3 (TB/s-equivalent).
    """
    up = max(0, int(up_kbps))
    down = max(0, int(down_kbps))
    unit = 0
    while up > 0x400 and (unit >> 4) < 3:
        up >>= 10
        unit += 0x10
    while down > 0x400 and (unit & 0x0F) < 3:
        down >>= 10
        unit += 1
    return unit & 0xFF, min(up, 0xFFFF), min(down, 0xFFFF)


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------

@dataclass
class SystemItem:
    item_id: int
    usage: int = 0xFF          # percent, 0xFF = N/A
    temp: int = 0xFF           # Celsius, 0xFF = N/A
    rpm: int = 0xFF            # fan drive level in percent, 0xFF = N/A
    name: str = ""


@dataclass
class DiskItem:
    disk_id: int
    total_gb: int
    used_gb: int
    temp: int = 0xFF
    name: str = ""


@dataclass
class WlanItem:
    net_id: int
    up_kbps: int = 0
    down_kbps: int = 0
    total_kb: int = 0
    ip: Tuple[int, int, int, int] = (0, 0, 0, 0)
    name: str = ""
    total_unit: Optional[int] = None   # None = auto-scale, 0..3 = KB/MB/GB/TB


@dataclass
class SystemSnapshot:
    timestamp: int
    cpu_usage: int = 0
    cpu_temp: int = 0xFF
    cpu_fan: int = 0xFF
    mem_usage: int = 0
    mem_temp: int = 0xFF
    mem_fan: int = 0xFF
    # The panel draws four cards from the two SystemPages. Each byte is either
    # a reading or 0xFF, which the firmware renders as "--".
    igpu_usage: int = 0xFF
    igpu_temp: int = 0xFF
    igpu_fan: int = 0xFF
    dgpu_present: bool = False
    dgpu_usage: int = 0xFF
    dgpu_temp: int = 0xFF
    dgpu_fan: int = 0xFF
    # Which card goes in which on-screen slot, by card id: 0=CPU, 1=iGPU,
    # 2=Memory, 3=DGPU. The panel places a card by the page's `order` byte, so
    # this reorders the pages; each card keeps its id, so its values follow it.
    card_order: Tuple[int, ...] = (0, 1, 2, 3)
    device_name: str = "Unraid"
    cpu_name: str = "CPU"
    os_name: str = "Unraid"
    serial: str = "Unknown"
    disks: List[DiskItem] = field(default_factory=list)
    networks: List[WlanItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Packet builder
# ---------------------------------------------------------------------------

class PacketBuilder:
    """Stateful builder; keeps the 8-bit sequence counter."""

    def __init__(self, sequence: int = 0):
        self.sequence = _u8(sequence)

    def _next_seq(self) -> int:
        seq = self.sequence
        self.sequence = _u8(self.sequence + 1)
        return seq

    def _build(self, aim: int, data: bytes, command: int = CMD_UPDATE) -> bytes:
        return build_packet(command, aim, data, self._next_seq())

    # -- pages ------------------------------------------------------------
    def home(self, timestamp: Optional[int] = None) -> bytes:
        ts = _u32(panel_timestamp() if timestamp is None else timestamp)
        data = struct.pack("<BBI", 1, 1, ts)  # order, total, unix time
        return self._build(AIM_HOME, data)

    def time(self, timestamp: Optional[int] = None) -> bytes:
        ts = _u32(panel_timestamp() if timestamp is None else timestamp)
        return self._build(AIM_TIME, struct.pack("<I", ts))

    def system_page(self, order: int, total: int, syscount: int,
                    items: Sequence[SystemItem]) -> bytes:
        """aim 0x10: up to two 13-byte system item blocks."""
        if len(items) > MAX_SYSTEM_ITEMS:
            raise ValueError("SystemPage supports at most two items")
        data = bytearray(struct.pack("<BBBB", order, total, syscount, len(items)))
        for item in items:
            data += bytes((
                13,
                _u8(item.item_id),
                _u8(item.usage),
                _u8(item.temp),
                _u8(item.rpm),
            )) + _fixed_ascii(item.name, 8)
        data += b"\xff" * (13 * (MAX_SYSTEM_ITEMS - len(items)))
        return self._build(AIM_SYSTEM, bytes(data))

    def disk_page(self, order: int, total: int, disk_count: int,
                  disks: Sequence[DiskItem]) -> bytes:
        """aim 0x50: up to two 25-byte disk entries."""
        if len(disks) > MAX_DISKS_PER_PAGE:
            raise ValueError("DiskPage supports at most two disks")
        data = bytearray(struct.pack("<BBBB", order, total, disk_count, len(disks)))
        for disk in disks:
            unit, total_v, used_v = encode_disk_size(disk.total_gb, disk.used_gb)
            data += bytes((
                25,
                _u8(disk.disk_id),
                unit,
                0,
            )) + struct.pack("<HH", total_v, used_v) + bytes((_u8(disk.temp),))
            data += _fixed_ascii(disk.name, MAX_DISK_NAME)
        data += bytes(25 * (MAX_DISKS_PER_PAGE - len(disks)))
        return self._build(AIM_DISK, bytes(data))

    def wlan_page(self, order: int, total: int, net_count: int,
                  net: WlanItem) -> bytes:
        """aim 0x60: one adapter per packet, 36-byte payload."""
        unit, up_v, down_v = encode_wlan_speed(net.up_kbps, net.down_kbps)
        ip = net.ip or (0, 0, 0, 0)
        data = bytearray((
            _u8(order),
            _u8(total),
            _u8(net_count),
            1,              # count in this packet
            1,              # online
            0x1F,           # constant used by the Windows client
            _u8(net.net_id),
            unit,
        ))
        data += struct.pack("<HH", up_v, down_v)
        data += bytes(_u8(x) for x in ip[:4])
        data += _fixed_ascii(net.name, MAX_WLAN_NAME)
        return self._build(AIM_WLAN, bytes(data))

    def mode_page(self) -> bytes:
        """aim 0x70: constant block sent by the Windows client."""
        return self._build(AIM_MODE, bytes((1, 1, 3, 3, 1, 0)))

    def info_page(self, order: int, total: int, name: str) -> bytes:
        """aim 0xA0: device / CPU / OS / serial strings (4 pages)."""
        text = str(name or "").encode("ascii", "replace")[:MAX_INFO_NAME]
        data = bytearray((_u8(order), _u8(total), 0, 0, len(text)))
        data += text.ljust(MAX_INFO_NAME, b"\x00")
        return self._build(AIM_INFO, bytes(data))

    # -- individual items -------------------------------------------------
    def system_item(self, item_id: int, usage: int, temp: int,
                    rpm: int = 0xFF) -> bytes:
        return self._build(AIM_SYSTEM_ITEM,
                           bytes((_u8(item_id), _u8(usage), _u8(temp), _u8(rpm))))

    def disk_item(self, disk: DiskItem) -> bytes:
        unit, total_v, used_v = encode_disk_size(disk.total_gb, disk.used_gb)
        data = bytes((_u8(disk.disk_id), unit))
        data += struct.pack("<HH", total_v, used_v) + bytes((_u8(disk.temp),))
        return self._build(AIM_DISK_ITEM, data)

    def wlan_speed(self, net: WlanItem) -> bytes:
        unit, up_v, down_v = encode_wlan_speed(net.up_kbps, net.down_kbps)
        data = bytes((_u8(net.net_id), unit)) + struct.pack("<HH", up_v, down_v)
        return self._build(AIM_WLAN_SPEED, data)

    def wlan_total(self, net: WlanItem) -> bytes:
        unit, value = encode_wlan_total(net.total_kb, net.total_unit)
        data = bytes((_u8(net.net_id), unit)) + struct.pack("<H", value)
        return self._build(AIM_WLAN_TOTAL, data)

    def wlan_ip(self, net: WlanItem) -> bytes:
        ip = net.ip or (0, 0, 0, 0)
        return self._build(AIM_WLAN_IP,
                           bytes((_u8(net.net_id),)) + bytes(_u8(x) for x in ip[:4]))

    def wlan_user(self, online: int = 1) -> bytes:
        return self._build(AIM_WLAN_USER, bytes((_u8(online), 0)))


# ---------------------------------------------------------------------------
# Full update sequence (mirrors FUN_1400061a0 + FUN_140006630)
# ---------------------------------------------------------------------------

def build_full_sequence(snapshot: SystemSnapshot,
                        builder: Optional[PacketBuilder] = None,
                        include_items: bool = True,
                        syscount: int = 3,
                        system_mode: str = "both",
                        layout: str = "per-page",
                        wlan_slots: int = 3) -> List[bytes]:
    """Return the same packet sequence the Windows client sends each second.

    `include_items=False` drops the 0x11 per-item refreshes, leaving only the
    pages. The pages already carry every value, so this is the switch to flip
    when a panel renders the refresh packets differently from the pages.

    `layout="per-page"` (the default) puts one card in each page. Two blocks in
    one page do not survive: the panel repaints the first block into the second
    slot, so a paired page always shows the first card twice. Per-page costs
    extra packets, which only matters because the panel drains slowly - pace the
    stream to its rate and the page count is no longer a problem.

    Different cards read different channels: the CPU card takes its temperature
    from the `0x11` item packet while another card renders the page block
    verbatim. `system_mode="both"` (the default) therefore puts the real values
    in the page blocks *and* repeats them in the item packets; "vendor" fills
    the blocks with the vendor's 0 / 0xFF / 0xFE placeholders and "legacy" keeps
    the values in the pages but sends no item packets.

    `syscount` defaults to 3 because that is the only value the Windows client
    ever writes — on both pages, with or without a discrete GPU. Sending 4 to
    advertise a DGPU card is our own invention and the panel ignores page 2
    when it sees it.
    """
    b = builder or PacketBuilder()
    packets: List[bytes] = []

    # FUN_1400061a0: aggregate pages
    packets.append(b.home(snapshot.timestamp))
    packets.append(b.time(snapshot.timestamp))

    # 0xFF is not a sentinel to the panel: a usage/fan byte of 0xFF is printed
    # as "255%" / "254%". Zero is the honest value when nothing was measured.
    # Temperature keeps 0xFF, which the panel does render as "--".
    def _num(value):
        return 0 if value in (None, 0xFF) else int(value)

    cpu_u, cpu_t, cpu_f = (_num(snapshot.cpu_usage), snapshot.cpu_temp,
                           _num(snapshot.cpu_fan))
    igpu_u, igpu_t, igpu_f = (_num(snapshot.igpu_usage), snapshot.igpu_temp,
                              _num(snapshot.igpu_fan))
    mem_u, mem_t, mem_f = (_num(snapshot.mem_usage), snapshot.mem_temp,
                           _num(snapshot.mem_fan))
    dgpu_u, dgpu_t, dgpu_f = (_num(snapshot.dgpu_usage), snapshot.dgpu_temp,
                              _num(snapshot.dgpu_fan))

    # A GPU card with neither a usage counter nor a temperature sensor cannot
    # report anything, and a permanent "0% / --" card is worse than no card.
    # CPU and Memory always exist; a GPU joins only when it can be measured.
    def _measurable(usage, temp):
        return usage != 0xFF or temp != 0xFF

    values = [(0, cpu_u, cpu_t, cpu_f, "CPU")]
    if _measurable(snapshot.igpu_usage, snapshot.igpu_temp):
        values.append((1, igpu_u, igpu_t, igpu_f, "iGPU"))
    values.append((2, mem_u, mem_t, mem_f, "Memory"))
    if snapshot.dgpu_present and _measurable(snapshot.dgpu_usage,
                                             snapshot.dgpu_temp):
        values.append((3, dgpu_u, dgpu_t, dgpu_f, "DGPU"))

    def block(card):
        """A page block for this card, honouring `system_mode`."""
        if system_mode == "vendor":
            # What the Windows client writes: no reading at all.
            return SystemItem(card[0], 0, 0xFF, 0xFE, card[4])
        return SystemItem(*card)

    # `syscount` is how many cards the panel draws, so it also caps the pages.
    order = tuple(snapshot.card_order or (0, 1, 2, 3))
    values.sort(key=lambda card: (order.index(card[0]) if card[0] in order
                                  else len(order)))
    limit = max(1, min(8, syscount))
    values = values[:limit]
    cards = [block(card) for card in values]

    if layout == "per-page":
        # One card per page. Two blocks inside one page do not survive: the
        # panel repaints the first block into the second slot, so slot 2 always
        # mirrors slot 1 no matter what we put there.
        total = len(cards)
        for order, card in enumerate(cards, 1):
            packets.append(b.system_page(order, total, total, [card]))
    else:
        total = max(1, min(4, (len(cards) + 1) // 2))
        for order in range(1, total + 1):
            chunk = cards[(order - 1) * 2: order * 2]
            while len(chunk) < 2:
                chunk.append(SystemItem(3, 0, 0xFF, 0xFE, "DGPU"))
            # syscount is the number of cards drawn; total is the page count.
            packets.append(b.system_page(order, total, len(cards), chunk))

    disk_count = len(snapshot.disks)
    disk_pages = (disk_count + 1) // 2
    for page in range(1, disk_pages + 1):
        packets.append(b.disk_page(page, disk_pages, disk_count,
                                   snapshot.disks[(page - 1) * 2: page * 2]))

    # The panel reserves three adapter slots (the Windows client caps at three)
    # and keeps whatever it last drew in the unused ones, which is why a machine
    # with a single br0 still showed a second, stale br0. Fill every slot so the
    # leftovers are overwritten with zeros.
    nets = list(snapshot.networks)
    slots = max(len(nets), min(3, wlan_slots))
    while len(nets) < slots:
        nets.append(WlanItem(len(nets), 0, 0, 0, (0, 0, 0, 0), ""))
    for index, net in enumerate(nets, 1):
        packets.append(b.wlan_page(index, slots, slots, net))

    packets.append(b.mode_page())

    info = (snapshot.device_name, snapshot.cpu_name,
            snapshot.os_name, snapshot.serial)
    for order, text in enumerate(info, 1):
        packets.append(b.info_page(order, 4, text))

    if not include_items:
        return packets

    # FUN_140006630: individual item refreshes
    packets.append(b.system_item(0, cpu_u, cpu_t, cpu_f))
    packets.append(b.system_item(1, igpu_u, igpu_t, igpu_f))
    packets.append(b.system_item(2, mem_u, mem_t, mem_f))
    if snapshot.dgpu_present:
        packets.append(b.system_item(3, dgpu_u, dgpu_t, dgpu_f))

    for disk in snapshot.disks:
        packets.append(b.disk_item(disk))

    for net in snapshot.networks:
        packets.append(b.wlan_speed(net))
        packets.append(b.wlan_total(net))
        packets.append(b.wlan_ip(net))

    packets.append(b.wlan_user(1))
    packets.append(b.time(snapshot.timestamp))
    return packets


# ---------------------------------------------------------------------------
# Self test / diagnostics
# ---------------------------------------------------------------------------

def _hexdump(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def selftest(verbose: bool = False) -> bool:
    """Validate framing, lengths and checksums against the Windows selftest."""
    ok = True
    b = PacketBuilder(0)
    ts = 0x11223344

    home = b.home(ts)
    expected_len = 13
    checks = [
        ("HomePage", home, 13, 0x0A, AIM_HOME),
        ("Time", b.time(ts), 11, 0x08, AIM_TIME),
    ]
    for name, pkt, size, length, aim in checks:
        good = (
            len(pkt) == size
            and pkt[0:2] == MAGIC_BYTES
            and pkt[3] == length
            and pkt[4] == CMD_UPDATE
            and pkt[5] == aim
            and pkt[-1] == checksum(pkt[:-1])
        )
        if verbose or not good:
            print(f"{name}: {'PASS' if good else 'FAIL'} {_hexdump(pkt)}")
        ok = ok and good

    snap = SystemSnapshot(
        timestamp=ts,
        cpu_usage=42, cpu_temp=55, mem_usage=37,
        device_name="Unraid", cpu_name="Intel Core i5",
        os_name="Unraid 7.1", serial="ABC123",
        disks=[
            DiskItem(0, 12000, 8000, 41, "disk1"),
            DiskItem(1, 500, 200, 0xFF, "cache"),
        ],
        networks=[WlanItem(0, 1200, 3400, 12345, (192, 168, 1, 10), "br0")],
    )
    packets = build_full_sequence(snap, PacketBuilder(1))
    fixed_lengths = {
        AIM_HOME: 13, AIM_TIME: 11, AIM_SYSTEM: 37, AIM_DISK: 61,
        AIM_WLAN: 43, AIM_MODE: 13, AIM_INFO: 60,
        AIM_SYSTEM_ITEM: 11, AIM_DISK_ITEM: 14, AIM_WLAN_USER: 9,
        AIM_WLAN_SPEED: 13, AIM_WLAN_TOTAL: 11, AIM_WLAN_IP: 12,
    }
    for index, pkt in enumerate(packets):
        parsed = parse_packet(pkt + b"\x00")  # parser expects a trailing crc slot
        good = (
            parsed is not None
            and pkt[0:2] == MAGIC_BYTES
            and pkt[3] == len(pkt) - 3
            and pkt[-1] == checksum(pkt[:-1])
            and len(pkt) == fixed_lengths.get(pkt[5], len(pkt))
        )
        if verbose or not good:
            print(f"packet[{index:02d}] aim=0x{pkt[5]:02X} "
                  f"{'PASS' if good else 'FAIL'} {_hexdump(pkt)}")
        ok = ok and good
    print(f"selftest: {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest(verbose="-v" in __import__("sys").argv) else 1)
