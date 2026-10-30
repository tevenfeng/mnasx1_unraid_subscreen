#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HarborOS NAS front-panel USB screen daemon (Unraid, Python 3 stdlib only).

The panel is a USB device with VID 0x5448 / PID 0x0002 and two bulk
endpoints. This daemon talks to it through the kernel ``usbfs`` interface
(fcntl.ioctl) so no pyusb / libusb *and no ctypes / libffi* is required.

Commands:
    run       (default) drive the display forever, reconnecting on errors
    once      send one update sequence
    dump      print the packets for one snapshot without touching USB
    probe     show USB discovery / claim information
    selftest  protocol self test + live stats sanity check
"""

import argparse
import errno
import fcntl
import glob
import hashlib
import json
import mmap
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

# Import the protocol module next to this file (works when launched from
# /boot/config/plugins/subscreen via the rc script too).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import subscreen_protocol as proto  # noqa: E402
from subscreen_protocol import (  # noqa: E402
    DiskItem,
    PacketBuilder,
    SystemItem,
    SystemSnapshot,
    WlanItem,
    build_full_sequence,
    parse_packet,
)

# ---------------------------------------------------------------------------
# USB constants
# ---------------------------------------------------------------------------

USB_VID = "5448"
USB_PID = "0002"
USB_BULK_TYPE = 2
USB_DIR_IN = 0x80

USBDEVFS_CLAIMINTERFACE = 0x8004550F
USBDEVFS_RELEASEINTERFACE = 0x80045510
USBDEVFS_BULK = 0xC0185502
# _IO('U', 20): resets the port and re-enumerates the device. The only way
# back from a wedged panel short of unplugging it.
USBDEVFS_RESET = 0x5514

BULK_WRITE_TIMEOUT_MS = 5000
BULK_READ_TIMEOUT_MS = 500

IOCTL_RETRY_ERRNOS = (errno.EINTR,)
READ_TIMEOUT_ERRNOS = (errno.ETIMEDOUT, errno.EAGAIN)

# Packet lengths produced by the protocol module (used by selftest).
VALID_PACKET_LENGTHS = {9, 11, 12, 13, 14, 37, 43, 60, 61}

AIM_NAMES = {
    0x00: "Home",
    0x01: "Time",
    0x10: "System",
    0x11: "SystemItem",
    0x50: "Disk",
    0x51: "DiskItem",
    0x60: "WLAN",
    0x61: "WLANUser",
    0x62: "WLANSpeed",
    0x64: "WLANTOTAL",
    0x65: "WLANIP",
    0x70: "Mode",
    0xA0: "Info",
    0xF0: "FwInfo",
    0xF1: "GetVersion",
    0xF2: "FwUpdate",
}

PAGE_NAMES = {
    0x00: "home",
    0x10: "system",
    0x50: "disks",
    0x60: "network",
    0xA0: "info",
}


class UsbError(OSError):
    """Generic USB transport error (an OSError, like the ioctl layer)."""


class UsbNotFound(UsbError):
    """No matching USB device in sysfs."""


# ---------------------------------------------------------------------------
# usbfs transport (fcntl.ioctl, no ctypes / libffi)
# ---------------------------------------------------------------------------

# The kernel struct usbdevfs_bulktransfer is
#     u32 ep; u32 len; u32 timeout; void *data;
# so its size depends on the pointer width. fcntl.ioctl() hands the struct to
# the kernel verbatim, which means the trailing "data" field must already hold
# a valid address; see UsbIoRegion for how that address is obtained without
# ctypes.
_POINTER_SIZE = struct.calcsize("P")
_BULK_HEADER = struct.calcsize("<III")
_BULK_PADDING = (-_BULK_HEADER) % _POINTER_SIZE
_BULK_DATA_OFFSET = _BULK_HEADER + _BULK_PADDING
BULK_REQUEST_SIZE = _BULK_DATA_OFFSET + _POINTER_SIZE


def _mapping_address(stat, min_size):
    """Return the address of a writable mapping backed by ``stat``'s file.

    Python only exposes a buffer's contents, never its address, and ctypes is
    unavailable on some hosts (Unraid ships a _ctypes that needs a missing
    libffi.so.7). /proc/self/maps is read instead: device + inode identify the
    mapping even after the file name has been unlinked.
    """
    device = "%02x:%02x" % (os.major(stat.st_dev), os.minor(stat.st_dev))
    inode = str(stat.st_ino)
    try:
        with open("/proc/self/maps", "r", encoding="ascii",
                  errors="replace") as handle:
            for line in handle:
                fields = line.split(None, 5)
                if len(fields) < 5:
                    continue
                if fields[3] != device or fields[4] != inode:
                    continue
                if "w" not in fields[1]:
                    continue
                first, _, last = fields[0].partition("-")
                try:
                    start, end = int(first, 16), int(last, 16)
                except ValueError:
                    continue
                if end - start >= min_size:
                    return start
    except OSError as exc:
        raise UsbError("cannot read /proc/self/maps: %s" % exc) from exc
    raise UsbError("cannot locate the usb i/o buffer mapping")


class UsbIoRegion:
    """A small scratch page whose address the kernel can be handed.

    usbfs transfers need a struct that *contains* a pointer, so knowing one
    buffer address is unavoidable. This maps a page of a tmpfs file, records
    the address from /proc/self/maps, then unlinks the file; the payload slots
    below it are what gets passed back and forth.
    """

    SIZE = 4096
    SLOT_SIZE = 512
    TX_SLOT = 0          # writer thread
    RX_SLOT = 2048       # reader thread

    def __init__(self):
        self.fd = None
        self.region = None
        self.base = None
        path = None
        error = None
        for directory in ("/dev/shm", None):
            try:
                self.fd, path = tempfile.mkstemp(prefix="subscreen-usb-",
                                                 dir=directory)
                break
            except OSError as exc:
                error = exc
        if self.fd is None:
            raise UsbError("cannot create a usb i/o buffer: %s" % error)
        try:
            os.ftruncate(self.fd, self.SIZE)
            self.region = mmap.mmap(self.fd, self.SIZE, mmap.MAP_PRIVATE,
                                    mmap.PROT_READ | mmap.PROT_WRITE)
            self.base = _mapping_address(os.fstat(self.fd), self.SIZE)
        except Exception:
            self.close()
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise
        # The name was only needed to obtain an identified mapping; the
        # mapping itself survives the unlink and nothing is left on disk.
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    def close(self):
        if self.region is not None:
            try:
                self.region.close()
            except (OSError, ValueError):
                pass
            self.region = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self.base = None


def _read_text(path, default=""):
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            return handle.read(4096).strip()
    except OSError:
        return default


def _parse_hex(text, default=0):
    try:
        return int(str(text).strip(), 16)
    except (TypeError, ValueError):
        return default


def _parse_dec(text, default=0):
    try:
        return int(str(text).strip(), 10)
    except (TypeError, ValueError):
        return default


def _driver_name(iface_dir):
    link = os.path.join(iface_dir, "driver")
    if not os.path.islink(link):
        return ""
    try:
        return os.path.basename(os.path.realpath(link))
    except OSError:
        return ""


class UsbTransport:
    """Bulk endpoint access to one panel through /dev/bus/usb (usbfs)."""

    def __init__(self, vid=USB_VID, pid=USB_PID):
        self.vid = vid
        self.pid = pid
        self.info = None
        self._fd = None
        self._io = None
        self._claimed = False
        self._unbound = None  # (driver, interface sysfs name) to rebind

    # -- discovery --------------------------------------------------------
    def discover(self):
        """Scan sysfs for the panel. Returns a dict, raises UsbNotFound."""
        for marker in sorted(glob.glob("/sys/bus/usb/devices/*/idVendor")):
            devdir = os.path.dirname(marker)
            if _read_text(marker).lower() != self.vid.lower():
                continue
            if _read_text(os.path.join(devdir, "idProduct")).lower() != self.pid.lower():
                continue
            real = os.path.realpath(devdir)
            busnum = _parse_dec(_read_text(os.path.join(real, "busnum")))
            devnum = _parse_dec(_read_text(os.path.join(real, "devnum")))
            if not busnum or not devnum:
                continue

            interfaces = []
            iface_dirs = []
            seen_dirs = set()
            for pattern in (os.path.join(real, os.path.basename(real) + ":*.*"),
                            devdir + ":*.*"):
                for iface_dir in glob.glob(pattern):
                    key = os.path.realpath(iface_dir)
                    if key in seen_dirs:
                        continue
                    seen_dirs.add(key)
                    iface_dirs.append(iface_dir)
            for iface_dir in sorted(iface_dirs):
                if not os.path.isdir(iface_dir):
                    continue
                number = _parse_hex(
                    _read_text(os.path.join(iface_dir, "bInterfaceNumber")), -1)
                if number < 0:
                    continue
                out_eps, in_eps = [], []
                for ep_dir in sorted(glob.glob(os.path.join(iface_dir, "ep_*"))):
                    address = _parse_hex(
                        _read_text(os.path.join(ep_dir, "bEndpointAddress")))
                    attrs = _parse_hex(
                        _read_text(os.path.join(ep_dir, "bmAttributes")))
                    if (attrs & 0x03) != USB_BULK_TYPE or not address:
                        continue
                    if address & USB_DIR_IN:
                        in_eps.append(address)
                    else:
                        out_eps.append(address)
                interfaces.append({
                    "dir": iface_dir,
                    "number": number,
                    "out": out_eps,
                    "in": in_eps,
                    "driver": _driver_name(iface_dir),
                })

            # Prefer interface 0, and among those the first with both bulk
            # directions; fall back to an OUT-only interface if necessary.
            interfaces.sort(key=lambda item: (item["number"] != 0, item["number"]))
            chosen = None
            for item in interfaces:
                if item["out"] and item["in"]:
                    chosen = item
                    break
            if chosen is None:
                for item in interfaces:
                    if item["out"]:
                        chosen = item
                        break
            if chosen is None and interfaces:
                chosen = interfaces[0]
            if chosen is None:
                # Device present but not configured yet (no interface dirs).
                chosen = {"dir": None, "number": 0, "out": [], "in": [],
                          "driver": ""}

            out_ep = 0x02 if 0x02 in chosen["out"] else (
                chosen["out"][0] if chosen["out"] else None)
            in_ep = 0x81 if 0x81 in chosen["in"] else (
                chosen["in"][0] if chosen["in"] else None)

            return {
                "sysfs": real,
                "busnum": busnum,
                "devnum": devnum,
                "node": "/dev/bus/usb/%03d/%03d" % (busnum, devnum),
                "iface_path": chosen["dir"],
                "interface": chosen["number"],
                "out_ep": out_ep,
                "in_ep": in_ep,
                "driver": chosen["driver"],
                "interfaces": interfaces,
            }
        raise UsbNotFound("USB device %s:%s not found in sysfs" % (self.vid, self.pid))

    # -- open / close -----------------------------------------------------
    def open(self, info=None):
        """Open the device node, unbind a kernel driver and claim interface."""
        if self._fd is not None:
            return self
        info = info or self.info or self.discover()
        try:
            fd = os.open(info["node"],
                         os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            raise UsbError("cannot open %s: %s (run as root?)"
                           % (info["node"], exc)) from exc
        self._fd = fd
        self.info = info
        try:
            self._io = UsbIoRegion()
            self._unbind_kernel_driver(info)
            self._claim(info["interface"])
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        """Release the interface, close the fd and rebind any driver."""
        fd = self._fd
        if fd is not None:
            try:
                if self._claimed:
                    try:
                        self._release_interface(self.info["interface"])
                    except (OSError, UsbError):
                        pass
            finally:
                # Clear the fd only after the release ioctl has used it.
                self._fd = None
                self._claimed = False
                try:
                    os.close(fd)
                except OSError:
                    pass
        else:
            self._claimed = False
        if self._io is not None:
            self._io.close()
            self._io = None
        self._rebind_kernel_driver()

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- descriptors ------------------------------------------------------
    def describe(self):
        info = self.info or self.discover()
        return {
            "sysfs": info["sysfs"],
            "busnum": info["busnum"],
            "devnum": info["devnum"],
            "node": info["node"],
            "interface": info["interface"],
            "out_ep": info["out_ep"],
            "in_ep": info["in_ep"],
            "driver": info["driver"],
            "claimed": self._claimed,
            "io_buffer": self._io.base if self._io is not None else None,
        }

    @property
    def claimed(self):
        return self._claimed

    # -- kernel driver handling ------------------------------------------
    def _unbind_kernel_driver(self, info):
        iface_dir = info.get("iface_path")
        driver = info.get("driver") or ""
        if not iface_dir or not driver or driver == "usbfs":
            return
        sysfs_name = os.path.basename(iface_dir.rstrip("/"))
        unbind = "/sys/bus/usb/drivers/%s/unbind" % driver
        if not os.path.exists(unbind):
            return
        try:
            with open(unbind, "w", encoding="ascii") as handle:
                handle.write(sysfs_name)
        except OSError as exc:
            raise UsbError("cannot unbind %s from %s: %s"
                           % (sysfs_name, driver, exc)) from exc
        self._unbound = (driver, sysfs_name)
        _log("unbound kernel driver %s from %s" % (driver, sysfs_name), verbose_only=True)

    def _rebind_kernel_driver(self):
        if not self._unbound:
            return
        driver, sysfs_name = self._unbound
        self._unbound = None
        bind = "/sys/bus/usb/drivers/%s/bind" % driver
        try:
            with open(bind, "w", encoding="ascii") as handle:
                handle.write(sysfs_name)
            _log("rebound kernel driver %s to %s" % (driver, sysfs_name),
                 verbose_only=True)
        except OSError:
            # The device may have been unplugged; nothing left to rebind.
            pass

    # -- ioctls -----------------------------------------------------------
    def _ioctl(self, request, arg):
        if self._fd is None:
            raise UsbError("USB device is closed")
        while True:
            try:
                return fcntl.ioctl(self._fd, request, arg)
            except OSError as exc:
                if exc.errno in IOCTL_RETRY_ERRNOS:
                    continue
                raise

    def _claim(self, interface):
        # usbfs reads the interface number through a user pointer.
        number = bytearray(struct.pack("<I", int(interface)))
        try:
            self._ioctl(USBDEVFS_CLAIMINTERFACE, number)
        except OSError as exc:
            raise UsbError("cannot claim interface %s: %s"
                           % (interface, exc)) from exc
        self._claimed = True

    def _release_interface(self, interface):
        self._ioctl(USBDEVFS_RELEASEINTERFACE,
                    bytearray(struct.pack("<I", int(interface))))

    # -- transfers --------------------------------------------------------
    def _bulk(self, endpoint, payload, timeout_ms, slot):
        """One usbfs bulk transfer through the shared scratch page."""
        if self._fd is None:
            raise UsbError("USB device is closed")
        if self._io is None:
            raise UsbError("usb i/o buffer is not mapped")
        size = len(payload)
        if size > UsbIoRegion.SLOT_SIZE:
            raise UsbError("bulk transfer too large: %d bytes" % size)
        if payload:
            self._io.region[slot:slot + size] = payload
        request = bytearray(
            struct.pack("<III", int(endpoint), size, int(timeout_ms))
            + b"\x00" * _BULK_PADDING
            + struct.pack("P", self._io.base + slot)
        )
        return self._ioctl(USBDEVFS_BULK, request)

    def write(self, data):
        if self._fd is None:
            raise UsbError("USB device is closed")
        if self.info is None or self.info.get("out_ep") is None:
            raise UsbError("no bulk OUT endpoint on this interface")
        payload = bytes(data)
        if not payload:
            return 0
        try:
            rc = self._bulk(self.info["out_ep"], payload,
                            BULK_WRITE_TIMEOUT_MS, UsbIoRegion.TX_SLOT)
        except OSError as exc:
            raise UsbError("bulk write failed: %s" % exc) from exc
        if rc != len(payload):
            raise UsbError("short bulk write: %d of %d bytes"
                           % (rc, len(payload)))
        return rc

    def read(self, maxlen=64):
        """Return up to maxlen bytes; b"" on timeout."""
        if self._fd is None:
            raise UsbError("USB device is closed")
        if self.info is None or self.info.get("in_ep") is None:
            raise UsbError("no bulk IN endpoint on this interface")
        try:
            rc = self._bulk(self.info["in_ep"], bytes(maxlen),
                            BULK_READ_TIMEOUT_MS, UsbIoRegion.RX_SLOT)
        except OSError as exc:
            if exc.errno in READ_TIMEOUT_ERRNOS:
                return b""
            raise UsbError("bulk read failed: %s" % exc) from exc
        if rc <= 0:
            return b""
        return bytes(self._io.region[UsbIoRegion.RX_SLOT:
                                     UsbIoRegion.RX_SLOT + rc])


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _log(message, verbose_only=False):
    if verbose_only and not _VERBOSE:
        return
    sys.stderr.write("[%s] subscreen: %s\n"
                     % (time.strftime("%H:%M:%S"), message))
    sys.stderr.flush()


_VERBOSE = False


def _clamp(value, low, high):
    return max(low, min(high, int(value)))


def _sanitize_ascii(text, fallback=""):
    """Return a printable, single-line ASCII string."""
    value = str(text or "").replace("\x00", " ")
    value = value.encode("ascii", "replace").decode("ascii")
    value = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in value)
    value = " ".join(value.split())
    return value or fallback


def _read_first_line(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def _format_temp(value):
    return "N/A" if value in (0xFF, 0xFE) else "%dC" % value


def _format_percent(value):
    return "--" if value == 0xFF else "%d%%" % value


def _format_ip(ip):
    return ".".join(str(_clamp(part, 0, 255)) for part in ip)


# ---------------------------------------------------------------------------
# Statistics collection
# ---------------------------------------------------------------------------

KB = 1024
MB = 1024 * 1024
GB = 1024 * MB

# The panel renders 0xFF in the usage/temp/fan bytes as "--". 0xFE is NOT a
# sentinel: it is printed verbatim ("254%"), which is what the panel showed
# before the fan field switched to 0xFF.
NO_VALUE = 0xFF
# The panel prints any fan byte verbatim with a "%" suffix: 0xFF shows as
# "255%", so there is no "no data" sentinel for this cell. Zero is the least
# misleading value we can send when no sensor exists.
FAN_UNKNOWN = 0


CARD_NAMES = {"cpu": 0, "igpu": 1, "mem": 2, "memory": 2, "dgpu": 3}


def _apply_card_order(snapshot, args):
    """Move cards between slots without touching their ids or values."""
    spec = getattr(args, "card_order", None)
    if not spec:
        return snapshot
    cards = []
    for token in str(spec).replace(" ", "").split(","):
        if not token:
            continue
        if token not in CARD_NAMES:
            raise SystemExit("unknown card %r (use cpu/igpu/mem/dgpu)"
                             % token)
        card = CARD_NAMES[token]
        if card in cards:
            raise SystemExit("card %r listed twice" % token)
        cards.append(card)
    if not cards:
        return snapshot
    missing = [card for card in (0, 1, 2, 3) if card not in cards]
    snapshot.card_order = tuple(cards + missing)
    return snapshot


def _card_count(snapshot, args):
    if getattr(args, "syscount", None) is not None:
        return max(1, min(8, args.syscount))
    return 4 if snapshot.dgpu_present else 3


def _sequence(snapshot, builder, args):
    """Build one frame with the CLI's system-page switches applied."""
    _apply_card_order(snapshot, args)
    syscount = getattr(args, "syscount", None)
    if syscount is None:
        syscount = 4 if snapshot.dgpu_present else 3
    return build_full_sequence(snapshot, builder,
                               include_items=getattr(args, "item_refresh", True),
                               syscount=syscount,
                               system_mode=getattr(args, "system_mode", "both"),
                               layout=getattr(args, "layout", "per-page"),
                               wlan_slots=getattr(args, "wlan_slots", 3))
DRM_ROOT = "/sys/class/drm"
HWMON_ROOT = "/sys/class/hwmon"
GPU_TTL = 1.0
# Seconds between packets while running: the panel drains ~3 packets a second.
RUN_PACE = 1.5
# Interface counters are sampled once a second in their own thread so a frame
# pacing of 20+ seconds cannot average a burst into a fake headline rate.
NET_SAMPLE_INTERVAL = 1.0
NET_RATE_ALPHA = 0.3
NET_RATE_TTL = 4.0
# Pages that never change between frames. Re-sending them costs the panel a
# third of its bandwidth for nothing, so `run` sends them only when it starts
# (and every LEAN_FULL_EVERY passes, to survive a panel-side reset).
STATIC_AIMS = frozenset({0x00, 0x65, 0x70, 0xA0})
# Disk pages change once a minute at most (temperatures), so they ride a slower
# cadence than the live counters.
SLOW_AIMS = frozenset({0x50, 0x51})
LEAN_FULL_EVERY = 12
LEAN_SLOW_EVERY = 4


def _lean_frame(packets, full, slow=False):
    """Trim a frame to what actually changes.

    `full` sends everything, `slow` adds the disk pages, otherwise only the
    live counters (time, system pages/items, network) go out.
    """
    if full:
        return packets
    skip = STATIC_AIMS if slow else STATIC_AIMS | SLOW_AIMS
    return [packet for packet in packets if packet[5] not in skip]
# nvidia-smi spawns a process, so it is refreshed on its own slower clock.
NVIDIA_TTL = 5.0
FAN_TTL = 2.0


@dataclass
class GpuReading:
    """One GPU as the panel sees it: 0-100 usage, Celsius, fan percent."""

    kind: str = "none"          # "igpu" / "dgpu"
    vendor: str = ""
    usage: int = NO_VALUE
    temp: int = NO_VALUE
    fan: int = NO_VALUE
    source: str = "none"


def _read_int(path):
    try:
        return int(_read_text(path).strip())
    except (TypeError, ValueError):
        return None


def _hwmon_paths(device):
    base = os.path.join(device, "hwmon")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.realpath(os.path.join(base, name)) for name in names]


def _fan_percent(hwmon):
    """Fan drive level as a percentage, which is what the panel labels.

    pwm1 is the control value (0-255) and maps straight to percent. Some
    boards leave it at 0 while the EC still spins the fan, so a spinning
    fan1_input wins when pwm1 reads zero; fan1_max turns rpm into percent,
    and without it rpm/100 is the only honest proxy.
    """
    pwm = _read_int(os.path.join(hwmon, "pwm1"))
    rpm = _read_int(os.path.join(hwmon, "fan1_input"))
    if pwm:
        return _clamp(round(pwm * 100 / 255), 1, 100)
    if rpm and rpm > 0:
        top = _read_int(os.path.join(hwmon, "fan1_max"))
        if top and top > 0:
            return _clamp(round(rpm * 100 / top), 1, 100)
        return _clamp(rpm // 100, 1, 255)
    if pwm == 0 and not rpm:
        return 0
    return NO_VALUE


def _gpu_temp(device, card=None):
    nodes = _hwmon_paths(device)
    if card:
        for extra in _sibling_hwmons(card, device):
            if extra not in nodes:
                nodes.append(extra)
    for hwmon in nodes:
        for probe in ("temp1_input", "temp2_input", "temp1_crit"):
            milli = _read_int(os.path.join(hwmon, probe))
            if milli is not None and probe != "temp1_crit":
                return _clamp(milli // 1000, 0, 120)
    return NO_VALUE


def _gpu_fan(device, card=None):
    nodes = _hwmon_paths(device)
    if card:
        for extra in _sibling_hwmons(card, device):
            if extra not in nodes:
                nodes.append(extra)
    for hwmon in nodes:
        if os.path.exists(os.path.join(hwmon, "pwm1")) or \
                os.path.exists(os.path.join(hwmon, "fan1_input")):
            return _fan_percent(hwmon)
    return NO_VALUE


FREQ_CURRENT_NAMES = ("gt_cur_freq_mhz", "gt_act_freq_mhz", "rps_cur_freq_mhz",
                      "act_freq", "cur_freq", "gt_cur_freq")
FREQ_MIN_NAMES = ("gt_min_freq_mhz", "rps_min_freq_mhz", "min_freq")
FREQ_MAX_NAMES = ("gt_max_freq_mhz", "rps_max_freq_mhz", "max_freq",
                  "rps_boost_freq_mhz", "boost_freq")


def _walk_freq_nodes(root, max_depth=4):
    """Yield (directory, {basename: path}) for every dir holding freq files.

    i915 moved its knobs over the kernel history (gt_cur_freq_mhz on older
    chips, gt/gt0/rps_cur_freq_mhz on Gen12+, tile0/gt0/freq0/ on xe), so the
    paths are discovered instead of hardcoded.
    """
    root = os.path.realpath(root)
    base_depth = root.rstrip("/").count("/")
    for current, dirs, files in os.walk(root):
        if current.rstrip("/").count("/") - base_depth >= max_depth:
            dirs[:] = []
        wanted = {name: os.path.join(current, name) for name in files}
        if any(name in wanted for name in FREQ_CURRENT_NAMES):
            yield current, wanted
        dirs[:] = [d for d in dirs if d not in ("hwmon", "drm", "power")]


def _find_freq_triplet(card, device):
    """(current, minimum, maximum) in MHz from the first usable node."""
    for root in (device, card):
        try:
            nodes = list(_walk_freq_nodes(root))
        except OSError:
            continue
        for _directory, files in nodes:
            current = None
            for name in FREQ_CURRENT_NAMES:
                if name in files:
                    current = _read_int(files[name])
                    break
            if current is None:
                continue
            low = high = None
            for name in FREQ_MIN_NAMES:
                if name in files:
                    low = _read_int(files[name])
                    if low is not None:
                        break
            for name in FREQ_MAX_NAMES:
                if name in files:
                    high = _read_int(files[name])
                    if high is not None:
                        break
            if high and high > (low or 0):
                return current, low or 0, high
    return None


def _sibling_hwmons(card, device, max_depth=4):
    """Every hwmon node under the card, at any nesting depth."""
    found = []
    for root in (device, card):
        for current, dirs, _files in os.walk(os.path.realpath(root)):
            if len(current.split(os.sep)) - len(os.path.realpath(root).split(os.sep)) >= max_depth:
                dirs[:] = []
            for name in list(dirs):
                if name.startswith("hwmon"):
                    found.append(os.path.realpath(os.path.join(current, name)))
    return sorted(set(found))


def _gpu_usage(device, card):
    """Utilization in percent from whatever the driver exposes.

    amdgpu publishes gpu_busy_percent directly. Intel has no busy counter in
    sysfs, so the current GT frequency is scaled between the minimum and the
    maximum: at idle the GPU sits on its minimum frequency, which maps to 0%.
    A plain cur/max ratio would report ~20% on an idle iGPU and is useless.
    """
    # amdgpu publishes one counter per engine. A NAS spends its GPU time in the
    # video engine (VCN), which gpu_busy_percent does not reflect, so the
    # busiest counter wins.
    busiest = None
    busiest_name = ""
    for name in ("gpu_busy_percent", "vcn_busy_percent", "gfx_busy_percent",
                 "jpeg_busy_percent", "gpu_busy", "busy_percent", "load"):
        value = _read_int(os.path.join(device, name))
        if value is None:
            continue
        if busiest is None or value > busiest:
            busiest, busiest_name = value, name
    if busiest is not None:
        return _clamp(busiest, 0, 100), "busy:" + busiest_name

    triplet = _find_freq_triplet(card, device)
    if triplet is not None:
        current, low, high = triplet
        percent = round((current - low) * 100 / (high - low))
        return _clamp(percent, 0, 100), "freq(%d/%d-%d)" % (current, low, high)
    return NO_VALUE, "no-counter"


def _scan_gpus():
    """Return (igpu, dgpu) readings from /sys/class/drm.

    Vendor decides the slot: Intel and AMD APUs feed the iGPU card, NVIDIA and
    discrete AMD cards feed the DGPU card. An AMD card is treated as an APU
    when its VRAM is tiny (shared memory) or when it is the only GPU present.
    """
    igpu = None
    dgpu = None
    try:
        cards = sorted(os.listdir(DRM_ROOT))
    except OSError:
        return None, None
    for name in cards:
        if not re.match(r"^card\d+$", name):
            continue
        card = os.path.join(DRM_ROOT, name)
        device = os.path.join(card, "device")
        vendor_id = _parse_hex(_read_text(os.path.join(device, "vendor")))
        if not vendor_id:
            continue
        dev_class = _parse_hex(_read_text(os.path.join(device, "class")))
        if dev_class and (dev_class >> 16) != 0x03:
            continue    # not a display controller
        reading = GpuReading(
            vendor="%04x" % vendor_id,
            usage=_gpu_usage(device, card)[0],
            temp=_gpu_temp(device, card),
            fan=_gpu_fan(device, card),
            source=_gpu_usage(device, card)[1],
        )
        if vendor_id == 0x8086:
            if igpu is None:
                reading.kind = "igpu"
                igpu = reading
            continue
        if vendor_id == 0x10DE:
            if dgpu is None:
                reading.kind = "dgpu"
                dgpu = reading
            continue
        vram = _read_int(os.path.join(device, "mem_info_vram_total")) or 0
        if vendor_id != 0x1002:
            continue    # not a GPU we know how to read
        if igpu is None and (not vram or vram < 1024 ** 3):
            reading.kind = "igpu"
            igpu = reading
        elif dgpu is None:
            reading.kind = "dgpu"
            dgpu = reading
    return igpu, dgpu
# The panel reserved eight disk slots in the vendor client; more are allowed
# by the protocol but unverified on hardware.
MAX_DISKS = 8


def _read_proc_stat():
    """Return (total_jiffies, idle_jiffies) for the aggregate cpu line."""
    try:
        with open("/proc/stat", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("cpu "):
                    fields = [int(part) for part in line.split()[1:]]
                    if len(fields) < 4:
                        return None
                    # Only the first eight fields are real CPU time; guest
                    # time is already included in user/nice.
                    fields = fields[:8]
                    total = sum(fields)
                    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
                    return total, idle
    except (OSError, ValueError):
        return None
    return None


def _read_meminfo():
    values = {}
    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                parts = rest.split()
                if parts:
                    try:
                        values[key.strip()] = int(parts[0])
                    except ValueError:
                        pass
    except OSError:
        pass
    return values


def _unescape_mount(text):
    return (text.replace("\\040", " ")
                .replace("\\011", "\t")
                .replace("\\012", "\n")
                .replace("\\134", "\\"))


def _read_mounts():
    """Map mount point -> source device (decoded from /proc/mounts)."""
    mounts = {}
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 2:
                    continue
                source = _unescape_mount(parts[0])
                target = _unescape_mount(parts[1])
                mounts.setdefault(target, source)
    except OSError:
        pass
    return mounts


def _read_net_dev():
    """Map interface -> (rx_bytes, tx_bytes) from /proc/net/dev."""
    counters = {}
    try:
        with open("/proc/net/dev", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                name = name.strip()
                fields = rest.split()
                if len(fields) < 16:
                    continue
                try:
                    counters[name] = (int(fields[0]), int(fields[8]))
                except ValueError:
                    continue
    except OSError:
        pass
    return counters


def _read_millideg(path):
    """Read a hwmon/thermal millidegree file, return sane Celsius or None."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            raw = handle.read(64).strip()
        celsius = int(round(int(raw) / 1000.0))
    except (OSError, ValueError):
        return None
    if 0 <= celsius <= 120:
        return celsius
    return None


# --- disk temperatures -----------------------------------------------------
# Unraid does not load drivetemp, so SATA/NVMe drives usually expose no
# hwmon node and smartctl is the only source. Querying it wakes nothing
# (-n standby) but costs a few dozen milliseconds, so results are cached and
# only a couple of devices are refreshed per collect() call.
SMARTCTL_CANDIDATES = ("/usr/sbin/smartctl", "/usr/bin/smartctl",
                       "/sbin/smartctl", "/bin/smartctl")
SMART_DEVICE_TYPES = ("sat", "scsi", "nvme")
SMART_TEMP_TTL = 120.0       # seconds a real reading stays valid
SMART_FAIL_TTL = 30.0        # retry sooner after a miss: the disk may wake up
SMART_TEMP_BUDGET = 2        # refreshes per collect() pass, not first reads

_smartctl_binary = None      # resolved once; "" when the host has no smartctl
_smart_temp_cache = {}       # device -> (monotonic, celsius or None, reason)
_smart_dev_type = {}         # device -> the -d value that produced a reading


def _find_smartctl():
    global _smartctl_binary
    if _smartctl_binary is None:
        found = shutil.which("smartctl") or ""
        if not found:
            for candidate in SMARTCTL_CANDIDATES:
                if os.access(candidate, os.X_OK):
                    found = candidate
                    break
        _smartctl_binary = found
    return _smartctl_binary or None


def _sane_temp(value):
    try:
        celsius = int(value)
    except (TypeError, ValueError):
        return None
    return celsius if 1 <= celsius <= 125 else None


def _parse_smart_temp(payload):
    """Current temperature from smartctl JSON output, else from its text."""
    payload = (payload or "").strip()
    if not payload:
        return None
    if payload.startswith("{"):
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        if isinstance(data, dict):
            block = data.get("temperature")
            if isinstance(block, dict) and block.get("current") is not None:
                return _sane_temp(block["current"])
            nvme = data.get("nvme_smart_health_information_log")
            if isinstance(nvme, dict):
                return _sane_temp(nvme.get("temperature"))
    for pattern in (
        r"Temperature[^\n]*?(\d{1,3})\s*\(Min/Max",   # ATA attribute table
        r"Temperature:\s*(\d{1,3})\s*Celsius",        # NVMe / SCSI
    ):
        match = re.search(pattern, payload)
        if match:
            return _sane_temp(match.group(1))
    return None


def _smart_summary(result):
    """One-line reason from a smartctl result (JSON messages or plain text)."""
    payload = (result.stdout or "").strip()
    if payload.startswith("{"):
        try:
            data = json.loads(payload)
        except ValueError:
            data = {}
        if isinstance(data, dict):
            mode = (data.get("power_mode") or {}).get("name", "")
            for message in data.get("messages") or []:
                if isinstance(message, dict) and message.get("string"):
                    return message["string"]
            if mode:
                return "power_mode=" + mode
        return ""
    lines = payload.splitlines()
    return lines[0] if lines else ""


def _run_smartctl(dev, device_type=None, use_json=True):
    """Run smartctl for one device; None when the host has no smartctl."""
    binary = _find_smartctl()
    if not binary:
        return None
    command = [binary, "-A", "-n", "standby"]
    if device_type:
        command += ["-d", device_type]
    if use_json:
        command.append("--json=c")
    command.append("/dev/" + dev)
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5, text=True)
    except Exception as exc:  # noqa: BLE001 - never break the loop
        _log("smartctl %s failed: %s" % (dev, exc), verbose_only=True)
        return None


def _smart_power_mode(result):
    """'SLEEP'/'STANDBY'/... reported by smartctl, or '' when unknown."""
    payload = (result.stdout or "").strip()
    if payload.startswith("{"):
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        if isinstance(data, dict):
            mode = (data.get("power_mode") or {}).get("name", "")
            if mode:
                return mode.upper()
            for message in data.get("messages") or []:
                text = (message.get("string") or "").upper() \
                    if isinstance(message, dict) else ""
                if "SLEEP MODE" in text or "STANDBY MODE" in text:
                    return "SLEEP"
        return ""
    upper = payload.upper()
    if "SLEEP MODE" in upper or "STANDBY MODE" in upper:
        return "SLEEP"
    return ""


def _smart_says_standby(result):
    """True only when the disk really is spun down.

    Never scan the raw payload for the word "standby": smartctl 7.5 echoes
    its own argv into the JSON, and that argv contains the literal argument
    "-n standby". Only the parsed power mode / message text is trustworthy.
    """
    mode = _smart_power_mode(result)
    if mode:
        return mode in ("SLEEP", "STANDBY", "LOW POWER IDLE")
    # Older smartctl / plain text: exit bit 1 means "device open failed or
    # in standby", which only counts when no reading came out of it.
    if (getattr(result, "returncode", 0) or 0) & 0x02:
        return _parse_smart_temp(getattr(result, "stdout", "")) is None
    return False


def _query_smart_temp(dev):
    """Return (celsius or None, reason).

    Bare invocations fail for common Unraid setups (USB bridges need ``-d
    sat``, virtio/scsi disks need ``-d scsi``), so every device type is tried
    once and the one that works is remembered.
    """
    known = _smart_dev_type.get(dev, "__unknown__")
    if known == "__unknown__":
        candidates = [None] + list(SMART_DEVICE_TYPES)
    else:
        # A cached type can go stale when the disk is replaced; keep the
        # preferred one first but still fall back to a full probe.
        candidates = [known] + [other for other in [None] + list(SMART_DEVICE_TYPES)
                                if other != known]

    last_exit = None
    for device_type in candidates:
        result = _run_smartctl(dev, device_type)
        if result is None:
            return None, "no-smartctl"
        if result.returncode & 0x01:
            # Bit 0: the command line did not parse (e.g. --json on 6.4).
            result = _run_smartctl(dev, device_type, use_json=False)
            if result is None:
                return None, "no-smartctl"
        last_exit = result.returncode
        if _smart_says_standby(result):
            return None, "standby:" + dev
        value = _parse_smart_temp(result.stdout)
        if value is not None:
            if device_type is not None:
                _smart_dev_type[dev] = device_type
            return value, ("smartctl:" + dev if device_type is None
                           else "smartctl:%s(-d %s)" % (dev, device_type))
        _log("smartctl %s (%s) exit=%d: no temperature"
             % (dev, device_type or "default", result.returncode),
             verbose_only=True)
    return None, "no-temp:%s(exit=%s)" % (dev, last_exit)


UNRAID_DISKS_INI = "/var/local/emhttp/disks.ini"


def _unraid_disk_table():
    """Parse /var/local/emhttp/disks.ini into {name: {key: value}}.

    This is Unraid's own device state, the same data the web UI and the
    Unassigned Devices plugin display: ``device`` maps a name to /dev/sdX and
    ``temp`` holds the temperature emhttpd last read. Unraid's md driver does
    not expose /sys/block/mdN/slaves the way stock md does, so this table is
    the only reliable route from /mnt/disk1 to a physical disk.
    """
    table = {}
    current = None
    try:
        with open(UNRAID_DISKS_INI, "r", encoding="utf-8",
                  errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith('["') and line.endswith('"]'):
                    current = line[2:-2]
                    table.setdefault(current, {})
                    continue
                if current is None or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                table[current][key.strip()] = value.strip().strip('"')
    except OSError:
        return {}
    return table


def _unraid_disk_map():
    """Map Unraid disk names ('disk1', 'cache', 'flash') to /dev nodes."""
    mapping = {}
    for name, fields in _unraid_disk_table().items():
        device = fields.get("device", "")
        if device and device not in ("", "device"):
            mapping[name] = "/dev/" + device
            mapping[device] = "/dev/" + device
    return mapping


# sdb1, nvme0n1p1, mmcblk0p1 are partitions of a physical disk: smartctl
# talks to the whole device better (and the -d type is a property of the
# bridge, not of a partition).
PARTITION_RE = re.compile(
    r"^(?:sd[a-z]+|vd[a-z]+|hd[a-z]+)\d+$"
    r"|^nvme\d+n\d+p\d+$"
    r"|^mmcblk\d+p\d+$")


def _is_partition(name):
    return bool(PARTITION_RE.match(name))


def _related_block_devices(name):
    """The device, its parent and any md slaves underneath it."""
    ordered = []

    def add(candidate):
        if candidate and candidate not in ordered:
            ordered.append(candidate)

    add(name)
    real = os.path.realpath("/sys/class/block/" + name)
    if os.path.isdir(real):
        parent = os.path.basename(os.path.dirname(real))
        if os.path.isdir("/sys/class/block/" + parent):
            add(parent)
    for candidate in list(ordered):
        try:
            slaves = sorted(os.listdir("/sys/class/block/%s/slaves" % candidate))
        except OSError:
            continue
        for slave in slaves:
            add(slave)
    return ordered


UNION_MOUNTS = ("/mnt/user", "/mnt/user0")
# Remote shares (SMB/NFS), plugins and the root share are not local disks:
# statvfs on them is slow or meaningless and they have no temperature.
SKIP_MOUNT_PREFIXES = ("/mnt/user/", "/mnt/remotes/", "/mnt/addons/",
                       "/mnt/rootshare/")


def _disk_label(path):
    """Short name shown on the panel for a mount point.

    Unassigned Devices mount under /mnt/disks/<label>, so the label is the
    interesting part; everything else uses its last path component.
    """
    clean = os.path.normpath(path)
    parts = [part for part in clean.split(os.sep) if part]
    if "disks" in parts:
        index = parts.index("disks")
        if index + 1 < len(parts):
            return parts[index + 1]
    return os.path.basename(clean) or clean


def _disk_priority(path, table):
    """Sort key: array disks, pools, other mounts, unassigned devices.

    Argument order decides which disks land in the panel's two-slot DiskPage
    and which survive the MAX_DISKS cap, so it must not depend on the order
    in which discovery happened to find them.
    """
    name = _disk_label(path)
    if re.match(r"^disk\d+$", name):
        return (0, int(name[4:]), name)
    pools = {key for key in table
             if not re.match(r"^disk\d+$", key) and key != "flash"}
    if name in pools:
        return (1, 0, name)
    if path.startswith("/mnt/disks/"):
        return (3, 0, name)
    return (2, 0, name)


def _default_disk_paths():
    """Array disks, then pools, then every other mounted local block device.

    Unassigned Devices mounts live under /mnt/disks/<label> with a plain
    /dev/sdX source, so they are reported exactly like array disks; extra
    pools also show up here as /mnt/<pool>.

    The boot flash is skipped: it is a USB stick whose bridge exposes no
    temperature and whose capacity says nothing about the array. Pass
    --disk /boot explicitly to show it anyway.
    """
    candidates = []
    try:
        entries = os.listdir("/mnt")
    except OSError:
        entries = []
    for name in entries:
        path = "/mnt/" + name
        if re.match(r"^disk\d+$", name) and os.path.isdir(path):
            candidates.append(path)
    if os.path.isdir("/mnt/cache"):
        candidates.append("/mnt/cache")

    for mount, source in _read_mounts().items():
        if not source.startswith("/dev/"):
            continue
        if mount == "/boot" or not mount.startswith("/mnt/"):
            continue
        if mount in UNION_MOUNTS or mount.startswith(SKIP_MOUNT_PREFIXES):
            continue
        if mount in candidates or not os.path.isdir(mount):
            continue
        candidates.append(mount)

    unique = list(dict.fromkeys(candidates))
    table = _unraid_disk_table()
    return sorted(unique, key=lambda path: _disk_priority(path, table))


def _iface_has_ipv4(name):
    """True when the interface currently owns an IPv4 address."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        return _ipv4_address(sock, name) != (0, 0, 0, 0)
    except OSError:
        return False
    finally:
        sock.close()


def _iface_up(name):
    return _read_text("/sys/class/net/%s/operstate" % name) in ("up", "unknown")


def _default_ifaces():
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        return []
    excluded_prefixes = ("docker", "veth", "virbr", "br-", "tap", "tun",
                         "tailscale", "shim", "dummy", "ifb")
    names = [name for name in names
             if name != "lo" and not name.startswith(excluded_prefixes)]

    def priority(name):
        # The Windows collector only keeps three adapters. Prefer the Unraid
        # bridge/bond, then physical Ethernet/Wi-Fi, then anything else.
        if name == "br0":
            return 0
        if name == "bond0":
            return 1
        if name.startswith(("eth", "en", "wl")):
            return 2
        return 3

    names.sort(key=lambda name: (priority(name), name))

    # Unraid bridges its uplink: br0 owns the address while eth0/bond0 are
    # members without one, and macvlan shims mirror the bridge. Sending those
    # fills the panel with empty adapters, so prefer interfaces that actually
    # carry an address and fall back to "up" ones when nothing does.
    addressed = [name for name in names if _iface_has_ipv4(name)]
    if addressed:
        return addressed[:3]
    return [name for name in names if _iface_up(name)][:3] or names[:3]


def _unique(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _ipv4_address(sock, name):
    """SIOCGIFADDR lookup; (0, 0, 0, 0) when the interface has no IPv4."""
    try:
        request = struct.pack("256s", name.encode("ascii", "replace")[:15])
        result = fcntl.ioctl(sock.fileno(), 0x8915, request)  # SIOCGIFADDR
        return tuple(result[20:24])
    except OSError:
        return (0, 0, 0, 0)


class StatsCollector:
    """Collects SystemSnapshot values from /proc, /sys and nvidia-smi."""

    def __init__(self, disk_paths=None, ifaces=None, wlan_total_unit=None):
        self.wlan_total_unit = wlan_total_unit
        if disk_paths:
            self.disk_paths = _unique(disk_paths)
        else:
            self.disk_paths = _unique(_default_disk_paths())
        if ifaces:
            self.ifaces = _unique(ifaces)
            self._ifaces_explicit = True
        else:
            self.ifaces = _unique(_default_ifaces())
            self._ifaces_explicit = False
        # /boot/config/go runs before Unraid's bridge has an address, so the
        # automatic interface list is re-evaluated while running.
        self._ifaces_next = time.monotonic() + 30.0

        self._prev_cpu = None
        self._prev_net = {}
        self._last_collect = None
        self._net_rate = {}
        self._net_rate_at = {}
        self._net_sample_at = None
        self._net_sampler = None

        # Static strings are read once; they do not change at runtime.
        self._cpu_name = None
        self._os_name = None
        self._serial = None
        self._device_name = None

        self._nvidia_smi = None
        self._nvidia_checked = False
        self._nvidia_next = 0.0
        self._nvidia_cache = None
        self.no_gpu = False
        self._gpu_cache = (None, None)
        self._gpu_next = 0.0
        self._fan_cache = NO_VALUE
        self._fan_next = 0.0

        self._smart_budget = SMART_TEMP_BUDGET
        self.temp_sources = {}

    # -- public -----------------------------------------------------------
    def _refresh_ifaces(self, now):
        """Re-evaluate the automatic adapter list every 30 seconds."""
        if self._ifaces_explicit or now < self._ifaces_next:
            return
        self._ifaces_next = now + 30.0
        self.ifaces = _unique(_default_ifaces())

    def warmup(self, window=1.2):
        """Seed the counters so the first collect() reports real rates.

        CPU usage and interface throughput are differences between two
        samples, so a single-shot run (once/dump/selftest) would otherwise
        report 0% for everything. The default window is just over one second so
        the throughput sampler has produced a real sample by the time the
        snapshot is taken.
        """
        self._prev_cpu = _read_proc_stat()
        self._prev_net = _read_net_dev()
        self._last_collect = time.monotonic()
        self._net_sample_at = time.monotonic()
        self._start_net_sampler()
        if window > 0:
            time.sleep(window)

    def _start_net_sampler(self):
        """Sample interface counters every second, independently of frames.

        Frame pacing can be 20+ seconds apart, and averaging one burst over
        that window turns a short spike into a headline like "27MB/s" that the
        user never sees anywhere else. The sampler keeps a one-second rate with
        a light smoothing filter, so the panel shows what the link is doing now.
        """
        if getattr(self, "_net_sampler", None) is not None:
            return
        thread = threading.Thread(target=self._net_sampler_loop,
                                  name="subscreen-net", daemon=True)
        self._net_sampler = thread
        thread.start()

    def _net_sampler_loop(self):
        previous = _read_net_dev()
        stamp = time.monotonic()
        while True:
            time.sleep(NET_SAMPLE_INTERVAL)
            now = time.monotonic()
            counters = _read_net_dev()
            elapsed = now - stamp
            if elapsed <= 0:
                continue
            for name, (rx, tx) in counters.items():
                old = previous.get(name)
                if old is None:
                    continue
                up = max(0, tx - old[1]) / elapsed / 1024
                down = max(0, rx - old[0]) / elapsed / 1024
                prev = self._net_rate.get(name)
                if prev is None:
                    self._net_rate[name] = (up, down)
                else:
                    self._net_rate[name] = (
                        prev[0] + (up - prev[0]) * NET_RATE_ALPHA,
                        prev[1] + (down - prev[1]) * NET_RATE_ALPHA,
                    )
                self._net_rate_at[name] = now
            previous = counters
            stamp = now

    def _net_rate_for(self, name):
        """Smoothed (up_kbps, down_kbps), or None when the sampler is stale."""
        stamp = self._net_rate_at.get(name)
        if stamp is None or time.monotonic() - stamp > NET_RATE_TTL:
            return None
        up, down = self._net_rate[name]
        return int(round(up)), int(round(down))

    def collect(self):
        now = time.monotonic()
        self._refresh_ifaces(now)
        elapsed = 0.0 if self._last_collect is None else now - self._last_collect
        self._last_collect = now

        snapshot = SystemSnapshot(
            timestamp=proto.panel_timestamp(),
            cpu_usage=self._cpu_usage(),
            cpu_temp=self._cpu_temp(),
            mem_usage=self._mem_usage(),
            device_name=self._device_name_value(),
            cpu_name=self._cpu_name_value(),
            os_name=self._os_name_value(),
            serial=self._serial_value(),
            disks=self._disks(),
            networks=self._networks(elapsed),
        )
        fan = self._cpu_fan(now)
        snapshot.cpu_fan = FAN_UNKNOWN if fan == NO_VALUE else fan
        igpu, dgpu = self._gpus(now) if not getattr(self, "no_gpu", False) \
            else (None, None)
        if igpu is not None:
            snapshot.igpu_usage = igpu.usage
            # An Intel iGPU shares the CPU die, so the package temperature is
            # the only real reading available for it.
            snapshot.igpu_temp = igpu.temp if igpu.temp != NO_VALUE else snapshot.cpu_temp
            snapshot.igpu_fan = (igpu.fan if igpu.fan != NO_VALUE
                                 else snapshot.cpu_fan)
        if dgpu is not None:
            snapshot.dgpu_present = True
            snapshot.dgpu_usage = dgpu.usage
            snapshot.dgpu_temp = dgpu.temp
            snapshot.dgpu_fan = (dgpu.fan if dgpu.fan != NO_VALUE
                                 else FAN_UNKNOWN)
        return snapshot

    # -- CPU / memory -----------------------------------------------------
    def _cpu_usage(self):
        stats = _read_proc_stat()
        if stats is None:
            return 0
        previous = self._prev_cpu
        self._prev_cpu = stats
        if previous is None:
            return 0
        delta_total = stats[0] - previous[0]
        delta_idle = stats[1] - previous[1]
        if delta_total <= 0:
            return 0
        used = (delta_total - delta_idle) * 100.0 / delta_total
        return _clamp(round(used), 0, 100)

    def _cpu_temp(self):
        values = []
        for name_path in glob.glob("/sys/class/hwmon/hwmon*/name"):
            name = _read_text(name_path).lower()
            if not any(token in name for token in ("coretemp", "k10temp", "zenpower")):
                continue
            value = _read_millideg(os.path.join(os.path.dirname(name_path),
                                                "temp1_input"))
            if value is not None:
                values.append(value)
        if values:
            return max(values)
        for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            value = _read_millideg(zone)
            if value is not None:
                values.append(value)
        return max(values) if values else 0xFF

    def _mem_usage(self):
        values = _read_meminfo()
        total = values.get("MemTotal", 0)
        if total <= 0:
            return 0
        available = values.get("MemAvailable")
        if available is None:
            available = (values.get("MemFree", 0) + values.get("Buffers", 0)
                         + values.get("Cached", 0))
        return _clamp(round((total - available) * 100.0 / total), 0, 100)

    # -- disks ------------------------------------------------------------
    def _disks(self):
        mounts = _read_mounts()
        unraid = _unraid_disk_table()
        self._smart_budget = SMART_TEMP_BUDGET
        self.temp_sources = {}
        disks = []
        for path in self.disk_paths:
            if len(disks) >= MAX_DISKS:
                _log("only the first %d disks are reported, %r ignored"
                     % (MAX_DISKS, path), verbose_only=True)
                break
            try:
                stat = os.statvfs(path)
            except OSError:
                _log("disk %s is not mountable, skipping" % path, verbose_only=True)
                continue
            block_size = stat.f_frsize or stat.f_bsize
            total_bytes = stat.f_blocks * block_size
            free_bytes = stat.f_bfree * block_size
            # The vendor client stores GiB (it shifts bytes right by 30 before
            # handing them to the same encoder), so the panel's base unit is GB.
            total_gb = total_bytes // GB
            used_gb = max(0, total_bytes - free_bytes) // GB
            name = _disk_label(path)
            source = mounts.get(os.path.normpath(path), "")
            temp, temp_source = self._disk_temp_detail(source, name, unraid)
            self.temp_sources[name] = (temp, temp_source)
            disks.append(DiskItem(len(disks), total_gb, used_gb, temp, name))
        return disks

    def _block_temp_values(self, dev):
        """Return sensor values (C) for one block device and its parent."""
        values = []
        candidates = [dev]
        real = os.path.realpath("/sys/class/block/" + dev)
        if os.path.isdir(real):
            parent = os.path.basename(os.path.dirname(real))
            if parent and parent != dev and os.path.isdir("/sys/class/block/" + parent):
                candidates.append(parent)
        for name in candidates:
            for pattern in (
                "/sys/class/block/%s/device/hwmon/hwmon*/temp1_input" % name,
                "/sys/class/block/%s/hwmon*/temp1_input" % name,
            ):
                for path in glob.glob(pattern):
                    value = _read_millideg(path)
                    if value is not None:
                        values.append(value)
        return values

    def _smart_temp(self, dev):
        """Cached (celsius, reason).

        A device with no cached value is always probed, so a single-shot
        ``once``/``dump``/``selftest`` reports every disk; the per-pass budget
        only throttles *refreshes* of values that are already known.
        """
        now = time.monotonic()
        cached = _smart_temp_cache.get(dev)
        if cached is not None:
            ttl = SMART_TEMP_TTL if cached[1] is not None else SMART_FAIL_TTL
            if now - cached[0] < ttl:
                return cached[1], cached[2]
            if self._smart_budget <= 0:
                return cached[1], cached[2]
            self._smart_budget -= 1
        value, reason = _query_smart_temp(dev)
        _smart_temp_cache[dev] = (now, value, reason)
        return value, reason

    def _disk_temp_detail(self, source, name="", table=None):
        """Return (celsius or 0xFF, how the value was obtained)."""
        if not source or not source.startswith("/dev/"):
            return 0xFF, "no-device"
        base = os.path.basename(source)
        if not base:
            return 0xFF, "no-device"

        # Unraid keeps its own per-disk state; the boot flash is "flash" there
        # while its mount point is /boot.
        if table is None:
            table = _unraid_disk_table()
        fields = table.get(name) or {}
        if not fields and name == "boot":
            fields = table.get("flash") or {}

        names = _related_block_devices(base)
        device = fields.get("device", "")
        if device and device not in names:
            names.append(device)
        for candidate in list(names):
            try:
                for slave in sorted(os.listdir(
                        "/sys/class/block/%s/slaves" % candidate)):
                    if slave not in names:
                        names.append(slave)
            except OSError:
                continue

        for candidate in names:
            values = self._block_temp_values(candidate)
            if values:
                return max(values), "hwmon:" + candidate

        # smartctl reads the current value; prefer it over the temperature
        # emhttpd last cached in disks.ini (which can be minutes old).
        reason = "no-sensor"
        standby_reason = ""
        physical = [name for name in names
                    if name.startswith(("sd", "nvme", "hd", "vd", "mmcblk"))]
        physical.sort(key=_is_partition)   # whole disks first, partitions after
        for candidate in physical:
            value, why = self._smart_temp(candidate)
            if value is not None:
                return value, why
            if why.startswith("standby") and not standby_reason:
                # Report the whole disk, not its last partition.
                standby_reason = why
            if not why.startswith("standby"):
                reason = why
        if standby_reason:
            reason = standby_reason

        # Fallback: emhttpd already read this disk for the web UI, and
        # Unassigned Devices displays the same value. It is free, works while
        # a disk sleeps, and keeps the panel in step with the web UI.
        value = _sane_temp(fields.get("temp", ""))
        if value is not None:
            asleep = fields.get("spundown", "0") == "1" or reason.startswith("standby")
            return value, "unraid:%s%s" % (name, "(sleep)" if asleep else "")

        if not _find_smartctl():
            return 0xFF, "no-smartctl"
        return 0xFF, reason

    # -- networks ---------------------------------------------------------
    def _networks(self, elapsed):
        counters = _read_net_dev()
        sock = None
        if self.ifaces:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            except OSError:
                sock = None
        networks = []
        try:
            for name in self.ifaces:
                if name not in counters:
                    continue
                rx_bytes, tx_bytes = counters[name]
                sampled = self._net_rate_for(name)
                previous = self._prev_net.get(name)
                if sampled is not None:
                    up_kbps, down_kbps = sampled
                elif previous is None or elapsed <= 0:
                    up_kbps = down_kbps = 0
                else:
                    rx_delta = max(0, rx_bytes - previous[0])
                    tx_delta = max(0, tx_bytes - previous[1])
                    down_kbps = int(rx_delta / elapsed) // 1024
                    up_kbps = int(tx_delta / elapsed) // 1024
                self._prev_net[name] = (rx_bytes, tx_bytes)
                ip = _ipv4_address(sock, name) if sock is not None else (0, 0, 0, 0)
                networks.append(WlanItem(
                    net_id=len(networks),
                    up_kbps=up_kbps,
                    down_kbps=down_kbps,
                    total_kb=(rx_bytes + tx_bytes) // KB,
                    ip=ip,
                    name=name,
                    total_unit=self.wlan_total_unit,
                ))
        finally:
            if sock is not None:
                sock.close()
        return networks

    # -- fans / GPUs ------------------------------------------------------
    def _cpu_fan(self, now):
        """Fan drive level in percent for the CPU card, cached for two seconds.

        Every hwmon node is considered: pwm1 is the control value the panel
        labels, and a spinning fan1_input wins when the board leaves pwm1 at
        zero because the EC does the regulation.
        """
        if now < self._fan_next:
            return self._fan_cache
        self._fan_next = now + FAN_TTL
        value = NO_VALUE
        try:
            entries = sorted(os.listdir(HWMON_ROOT))
        except OSError:
            entries = []
        for name in entries:
            path = os.path.join(HWMON_ROOT, name)
            if not (os.path.exists(os.path.join(path, "pwm1")) or
                    os.path.exists(os.path.join(path, "fan1_input"))):
                continue
            candidate = _fan_percent(path)
            if candidate != NO_VALUE:
                value = candidate
                break
        self._fan_cache = value
        return value

    def _gpus(self, now):
        """(igpu, dgpu) readings, refreshed at most once a second.

        sysfs covers Intel/AMD; NVIDIA only answers through nvidia-smi, so it
        is probed only when a /dev/nvidiaX node exists.
        """
        if now < self._gpu_next:
            return self._gpu_cache
        self._gpu_next = now + GPU_TTL
        igpu, dgpu = _scan_gpus()
        # sysfs knows an NVIDIA card exists but exposes no counters for it, so
        # nvidia-smi is the only source of usage/temperature/fan. It is asked
        # whenever it exists, not only when sysfs came up empty.
        if now >= self._nvidia_next:
            self._nvidia_next = now + NVIDIA_TTL
            nvidia = self._nvidia_gpu(now)
            if nvidia is not None:
                dgpu = nvidia
                self._nvidia_cache = nvidia
            elif self._nvidia_cache is not None:
                dgpu = self._nvidia_cache
        elif self._nvidia_cache is not None:
            dgpu = self._nvidia_cache
        self._gpu_cache = (igpu, dgpu)
        return self._gpu_cache

    def _nvidia_gpu(self, now=None):
        if not self._nvidia_checked:
            self._nvidia_checked = True
            self._nvidia_smi = shutil.which("nvidia-smi") or ""
        if not self._nvidia_smi:
            return None
        cmd = [self._nvidia_smi,
               "--query-gpu=utilization.gpu,temperature.gpu,fan.speed",
               "--format=csv,noheader,nounits"]
        try:
            result = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=3, text=True)
            if result.returncode != 0:
                raise OSError("nvidia-smi exited %d" % result.returncode)
            line = result.stdout.strip().splitlines()[0]
            fields = [part.strip() for part in line.split(",")]
            if len(fields) < 3:
                raise ValueError("unexpected nvidia-smi output")
        except Exception as exc:  # noqa: BLE001 - never break the loop
            _log("nvidia-smi query failed: %s" % exc, verbose_only=True)
            # Spawning a failing process every second is pointless; back off.
            stamp = time.time() if now is None else now
            self._gpu_next = max(self._gpu_next, stamp + 60)
            return None
        # nvidia-smi prints "[N/A]" for fields the card does not expose.
        usage = _parse_int(fields[0], NO_VALUE)
        temp = _parse_int(fields[1], NO_VALUE)
        fan = _parse_int(fields[2], NO_VALUE)
        return GpuReading(
            kind="dgpu", vendor="10de",
            usage=_clamp(usage, 0, 100) if usage not in (NO_VALUE, 0xFE) else NO_VALUE,
            temp=_clamp(temp, 0, 120) if temp not in (NO_VALUE, 0xFE) else NO_VALUE,
            fan=_clamp(fan, 0, 100) if fan not in (NO_VALUE, 0xFE) else NO_VALUE,
            source="nvidia-smi",
        )

    # -- cached info strings ---------------------------------------------
    def _device_name_value(self):
        if self._device_name is None:
            try:
                hostname = socket.gethostname()
            except OSError:
                hostname = ""
            self._device_name = _sanitize_ascii(hostname, "Unraid")
        return self._device_name

    def _cpu_name_value(self):
        if self._cpu_name is None:
            name = ""
            try:
                with open("/proc/cpuinfo", "r", encoding="utf-8",
                          errors="replace") as handle:
                    for line in handle:
                        if line.lower().startswith("model name"):
                            name = line.split(":", 1)[1] if ":" in line else ""
                            break
            except OSError:
                pass
            self._cpu_name = _sanitize_ascii(name, "CPU")
        return self._cpu_name

    def _os_name_value(self):
        if self._os_name is None:
            text = ""
            try:
                with open("/etc/unraid-version", "r", encoding="utf-8",
                          errors="replace") as handle:
                    text = handle.read(4096)
            except OSError:
                pass
            match = re.search(r"version\s*=\s*[\"']([^\"']+)[\"']", text)
            if match:
                self._os_name = _sanitize_ascii("Unraid " + match.group(1), "Unraid")
            else:
                self._os_name = "Unraid"
        return self._os_name

    def _serial_value(self):
        if self._serial is None:
            raw = _read_text("/sys/class/dmi/id/product_serial")
            self._serial = _sanitize_ascii(raw, "Unknown")
        return self._serial


def _parse_int(text, default):
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Panel -> host event handling (reader thread)
# ---------------------------------------------------------------------------

def _extract_frames(buffer):
    """Split a byte stream into complete protocol frames."""
    frames = []
    while True:
        start = buffer.find(proto.MAGIC_BYTES)
        if start < 0:
            # Keep a trailing 0xA5 that may be the start of the next magic.
            buffer = b"\xA5" if buffer.endswith(b"\xA5") else b""
            break
        if start:
            buffer = buffer[start:]
        if len(buffer) < 4:
            break
        length = buffer[3]
        if length < 4 or length + 2 > 258:
            buffer = buffer[1:]
            continue
        # Panel -> host frames may or may not carry the trailing checksum:
        # the Windows client only ever reads the first 6 bytes and ignores
        # anything after the payload. Consuming length+2 (magic..data) works
        # for both variants; a CRC byte, if present, is skipped as noise.
        total = length + 2
        if len(buffer) < total:
            break
        frames.append(buffer[:total])
        buffer = buffer[total:]
    return frames, buffer


def _handle_hibernate(args, state):
    if not args.allow_hibernate:
        _log("panel: hibernate request ignored (start with --allow-hibernate "
             "to enable)")
        return
    if state.get("hibernate_issued"):
        return
    state["hibernate_issued"] = True
    _log("panel: hibernate requested - running shutdown -h now")
    try:
        subprocess.Popen(
            ["shutdown", "-h", "now"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        _log("panel: shutdown command failed: %s" % exc)


def _handle_incoming(info, args, state):
    command = info["command"]
    aim = info["aim"]
    data = info["data"]
    crc_note = "" if info.get("crc_ok") in (True, None) else " [bad crc]"

    if command == proto.CMD_EVENT:
        if aim in proto.EVENT_PAGES:
            _log("panel: page select 0x%02X (%s)%s"
                 % (aim, PAGE_NAMES.get(aim, "unknown"), crc_note))
        elif aim == proto.EVENT_MUTE:
            _log("panel: mute button%s" % crc_note)
        elif aim == proto.EVENT_PERFORMANCE:
            _log("panel: performance profile requested%s" % crc_note)
        elif aim == proto.EVENT_BALANCED:
            _log("panel: balanced profile requested%s" % crc_note)
        elif aim in (proto.EVENT_HIBERNATE, proto.EVENT_HIBERNATE_2):
            _log("panel: hibernate event%s" % crc_note)
            _handle_hibernate(args, state)
        else:
            _log("panel: unknown event aim=0x%02X data=%s%s"
                 % (aim, _hex(data), crc_note))
    elif command == proto.CMD_OTA:
        _log("panel: OTA/firmware event aim=0x%02X data=%s%s"
             % (aim, _hex(data), crc_note))
    elif command == proto.CMD_UPDATE and not data:
        # A six-byte status frame: A5 5A <panel seq> 04 02 <page>. It carries
        # its own running counter (not an echo of ours) and appears a few times
        # a second while the panel is being fed data, stopping when the link
        # goes quiet. Nothing to reply to; log it only when asked.
        _log("panel: status page=0x%02X seq=%d%s"
             % (aim, info.get("sequence", -1), crc_note), verbose_only=True)
    else:
        # Unknown panel frames are logged whole: without the raw bytes there is
        # no way to tell a handshake request from a mis-framed fragment.
        _log("panel: unexpected command=0x%02X aim=0x%02X len=%d data=%s%s "
             "raw=%s"
             % (command, aim, info.get("length", 0), _hex(data), crc_note,
                _hex(info.get("raw", b""))))


def _hex(data):
    return " ".join("%02X" % byte for byte in data) or "-"


def _reader_loop(transport, stop_event, args, state):
    """Read and decode panel events. Never sends anything."""
    buffer = b""
    while not stop_event.is_set():
        try:
            chunk = transport.read(64)
        except (OSError, UsbError) as exc:
            if not stop_event.is_set():
                _log("reader stopped: %s" % exc)
            return
        if not chunk:
            continue
        buffer += chunk
        if len(buffer) > 4096:
            buffer = buffer[-1024:]
        frames, buffer = _extract_frames(buffer)
        for frame in frames:
            try:
                info = parse_packet(frame)
                if info is not None:
                    info["raw"] = frame
                    _handle_incoming(info, args, state)
                else:
                    _log("panel: unparsable frame raw=%s" % _hex(frame))
            except Exception as exc:  # noqa: BLE001 - reader must not die
                _log("reader: event handling failed: %s" % exc)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _mock_packet_line(packet):
    return "seq=%d aim=0x%02X len=%d hex=%s" % (
        packet[2], packet[5], len(packet), _hex(packet))


def _dump_packet_line(packet):
    return "seq=%d aim=0x%02X (%-10s) len=%d hex=%s" % (
        packet[2], packet[5],
        AIM_NAMES.get(packet[5], "Unknown"), len(packet), _hex(packet))


def _snapshot_line(snapshot):
    return ("cpu=%d%% %s mem=%d%% disks=%d nets=%d igpu=%s dgpu=%s"
            % (snapshot.cpu_usage, _format_temp(snapshot.cpu_temp),
               snapshot.mem_usage, len(snapshot.disks),
               len(snapshot.networks),
               _format_percent(snapshot.igpu_usage),
               "yes" if snapshot.dgpu_present else "no"))


CARD_LABELS = {0: "CPU", 1: "iGPU", 2: "Memory", 3: "DGPU"}
CARD_FIELDS = {
    0: ("cpu_usage", "cpu_temp", "cpu_fan"),
    1: ("igpu_usage", "igpu_temp", "igpu_fan"),
    2: ("mem_usage", "mem_temp", "mem_fan"),
    3: ("dgpu_usage", "dgpu_temp", "dgpu_fan"),
}


def _present_cards(snapshot):
    """Card ids that will be drawn, GPU cards only when they can be read."""
    cards = [0, 2]
    if (snapshot.igpu_usage != NO_VALUE or snapshot.igpu_temp != NO_VALUE):
        cards.insert(1, 1)
    if snapshot.dgpu_present and (snapshot.dgpu_usage != NO_VALUE or
                                  snapshot.dgpu_temp != NO_VALUE):
        cards.append(3)
    order = tuple(snapshot.card_order or (0, 1, 2, 3))
    cards.sort(key=lambda card: (order.index(card) if card in order
                                 else len(order)))
    return cards


def _sys_lines(snapshot):
    """The SystemPage cards exactly as they go out on the wire, slot by slot."""
    lines = []
    for slot, card in enumerate(_present_cards(snapshot)):
        usage, temp, fan = CARD_FIELDS[card]
        lines.append("  sys[%d] %-7s usage=%-4s temp=%-4s fan=%s"
                     % (slot, CARD_LABELS[card],
                        _format_percent(getattr(snapshot, usage)),
                        _format_temp(getattr(snapshot, temp)),
                        _format_percent(getattr(snapshot, fan))))
    return lines


def _report_lines(snapshot, collector=None, summary=True):
    """Every value that goes out on the wire, in panel-card order.

    `once` prints this so the panel can be checked line by line; `dump` and
    `selftest` share it so the three commands can never drift apart.
    """
    lines = []
    if summary:
        lines.append("snapshot: %s" % _snapshot_line(snapshot))
    lines.append(_time_line(snapshot))
    lines.extend(_sys_lines(snapshot))
    if snapshot.disks:
        for disk in snapshot.disks:
            lines.append("  disk[%d] %-10s total=%dGB used=%dGB temp=%s"
                         % (disk.disk_id, disk.name, disk.total_gb,
                            disk.used_gb, _format_temp(disk.temp)))
    else:
        lines.append("  disk: none")
    if collector is not None:
        lines.extend(_temp_lines(collector))
    if snapshot.networks:
        for net in snapshot.networks:
            source = ""
            if collector is not None and hasattr(collector, "_net_rate_for"):
                sample = collector._net_rate_for(net.name)
                if sample is not None:
                    source = " (1s sample)"
            lines.append(
                "  net[%d] %-12s up=%dKB/s down=%dKB/s total=%dMB ip=%s%s"
                % (net.net_id, net.name, net.up_kbps, net.down_kbps,
                   net.total_kb // 1024, _format_ip(net.ip), source))
    else:
        lines.append("  net: none")
    return lines


def _time_line(snapshot):
    """Show the wire timestamp next to what the panel will render from it."""
    wire = snapshot.timestamp
    return "time: now %s | panel shows %s (wire=%d)" % (
        proto.local_time_text(),
        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(wire)),
        wire,
    )


def _temp_lines(collector):
    """Per-disk temperature and where it came from (diagnostics)."""
    return ["  temp %-12s %s via %s" % (name, _format_temp(value), source)
            for name, (value, source) in sorted(collector.temp_sources.items())]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

TOTAL_UNIT_CHOICES = {"auto": None, "kb": 0, "mb": 1, "gb": 2, "tb": 3}


def _total_unit(args):
    """Map --wlan-total-unit to the encoder's unit nibble (None = auto)."""
    return TOTAL_UNIT_CHOICES.get(getattr(args, "wlan_total_unit", "auto"))


def _make_collector(args):
    collector = _build_collector(args)
    collector.no_gpu = bool(getattr(args, "no_gpu", False))
    return collector


def _build_collector(args):
    return StatsCollector(disk_paths=args.disks, ifaces=args.ifaces,
                          wlan_total_unit=_total_unit(args))


def _install_signal_handlers(stop_event):
    def handler(signum, _frame):
        _log("signal %d received, stopping" % signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def cmd_run(args):
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    collector = _make_collector(args)
    collector.warmup()
    builder = PacketBuilder(args.sequence)

    if args.mock:
        _log("running in mock mode (no USB); Ctrl-C to stop")
        try:
            while not stop_event.is_set():
                snapshot = collector.collect()
                for packet in _sequence(snapshot, builder, args):
                    print(_mock_packet_line(packet), flush=True)
                stop_event.wait(args.interval)
        except KeyboardInterrupt:
            pass
        _log("stopped")
        return 0

    state = {"hibernate_issued": False}
    last_error = None
    last_error_log = 0.0

    while not stop_event.is_set():
        transport = UsbTransport()
        try:
            transport.open()
        except (UsbError, OSError) as exc:
            now = time.monotonic()
            message = str(exc)
            if message != last_error or now - last_error_log >= 30.0:
                _log("connect failed: %s (retrying every 2s)" % message)
                last_error = message
                last_error_log = now
            stop_event.wait(2.0)
            continue

        last_error = None
        info = transport.describe()
        _log("connected: %s bus=%03d dev=%03d iface=%d out=0x%02X in=0x%02X"
             % (info["node"], info["busnum"], info["devnum"],
                info["interface"],
                info["out_ep"] or 0, info["in_ep"] or 0))

        reader_stop = threading.Event()
        reader = threading.Thread(
            target=_reader_loop,
            args=(transport, reader_stop, args, state),
            name="subscreen-reader",
            daemon=True,
        )
        reader.start()

        try:
            passes = 0
            while not stop_event.is_set():
                try:
                    snapshot = collector.collect()
                    packets = _sequence(snapshot, builder, args)
                except Exception as exc:  # noqa: BLE001 - keep the service alive
                    _log("stats/build failed: %s" % exc)
                    stop_event.wait(args.interval)
                    continue
                if getattr(args, "lean", True):
                    full = passes % LEAN_FULL_EVERY == 0
                    slow = full or passes % LEAN_SLOW_EVERY == 0
                    packets = _lean_frame(packets, full, slow)
                passes += 1
                try:
                    _write_frame(transport, packets, _run_pace(args),
                                 progress=True)
                except (UsbError, OSError) as exc:
                    _log("write failed: %s (resetting the port, then "
                         "reconnecting)" % exc)
                    _reset_port(transport)
                    break
                _log("sent %d packets (%s)" % (len(packets), _snapshot_line(snapshot)),
                     verbose_only=True)
                stop_event.wait(args.interval)
        finally:
            reader_stop.set()
            reader.join(timeout=2.0)
            transport.close()

        if not stop_event.is_set():
            stop_event.wait(2.0)

    _log("stopped")
    return 0


def _reset_port(transport):
    """Best-effort USBDEVFS_RESET so a wedged panel comes back."""
    fd = getattr(transport, "_fd", None)
    if fd is None:
        return False
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        _log("panel: port reset")
        return True
    except OSError as exc:
        _log("panel: port reset failed: %s" % exc)
        return False


def _run_pace(args):
    """Seconds between packets for `run`.

    Measured on hardware: a 31-packet burst takes the panel 5-10 s to render,
    and anything faster than 1.5 s per packet leaves it with a backlog it never
    clears - the screen freezes and stops taking input. 1.5 s is the verified
    safe value; a lean frame (static pages omitted) needs less, so `--pace` can
    be lowered once a build is confirmed stable.
    """
    pace = getattr(args, "pace", None)
    if pace is None:
        pace = max(args.packet_delay, RUN_PACE)
    return max(0.0, pace)


def _write_frame(transport, packets, delay, progress=False):
    """Write one frame, pacing the packets like the vendor client does.

    The Windows client trickles 20-25 packets per second (~45 ms apart). The
    panel's MCU cannot take a 31-packet burst: it keeps the first few and drops
    the rest, which leaves most cards showing the values from an older frame.
    """
    started = time.monotonic()
    for index, packet in enumerate(packets):
        transport.write(packet)
        if delay > 0:
            time.sleep(delay)
        if progress and index + 1 == len(packets):
            _log("frame: %d packets in %.1fs"
                 % (len(packets), time.monotonic() - started),
                 verbose_only=True)


def _send_frames(transport, snapshot, args, frames):
    """Push the full sequence `frames` times, rebuilding each pass.

    One frame is often not enough: a panel that has just switched pages can
    drop the first one, which is how a cell keeps showing the value from an
    earlier run while its neighbours are fresh.
    """
    builder = PacketBuilder(args.sequence)
    count = 0
    for index in range(max(1, frames)):
        packets = _sequence(snapshot, builder, args)
        _write_frame(transport, packets, args.packet_delay)
        count = len(packets)
        if index + 1 < frames:
            time.sleep(min(0.05, max(0.0, args.interval)) + 0.2)
    return count


def cmd_once(args):
    collector = _make_collector(args)
    collector.warmup()
    snapshot = collector.collect()

    if args.mock:
        packets = _sequence(snapshot, PacketBuilder(args.sequence), args)
        for line in _report_lines(snapshot, collector):
            print(line)
        print("cards: %d (system-mode %s)  packets: %d (mock)"
              % (_card_count(snapshot, args), args.system_mode,
                 len(packets)))
        for packet in packets:
            print(_mock_packet_line(packet), flush=True)
        return 0

    transport = UsbTransport()
    try:
        transport.open()
    except (UsbError, OSError) as exc:
        _log("cannot open USB device: %s" % exc)
        return 1
    try:
        sent = _send_frames(transport, snapshot, args, args.repeat)
    except (UsbError, OSError) as exc:
        _log("send failed: %s" % exc)
        return 1
    finally:
        transport.close()

    # Print what was just pushed, so the panel can be compared line by line.
    for line in _report_lines(snapshot, collector):
        print(line)
    print("cards: %d (system-mode %s)  packets: %d x %d frames"
          % (_card_count(snapshot, args), args.system_mode, sent,
             max(1, args.repeat)))
    _log("sent %d packets x%d (%s)"
         % (sent, max(1, args.repeat), _snapshot_line(snapshot)))
    return 0


def _uevent_value(device, key):
    for line in _read_text(os.path.join(device, "uevent")).splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def cmd_gpus(_args):
    """Everything the GPU scan can see, so a bad reading is diagnosable."""
    print("drm root: %s" % DRM_ROOT)
    try:
        cards = sorted(os.listdir(DRM_ROOT))
    except OSError as exc:
        cards = []
        print("  cannot list: %s" % exc)
    for name in cards:
        if not re.match(r"^card\d+$", name):
            continue
        path = os.path.join(DRM_ROOT, name)
        device = os.path.join(path, "device")
        vendor = _read_text(os.path.join(device, "vendor"))
        if not vendor:
            continue
        usage, source = _gpu_usage(device, path)
        print("  %-8s vendor=%s device=%s class=%s driver=%s"
              % (name, vendor,
                 (_read_text(os.path.join(device, "device")) or "?").strip(),
                 (_read_text(os.path.join(device, "class")) or "?").strip(),
                 _uevent_value(device, "DRIVER") or "?"))
        print("           usage=%s (%s) temp=%s fan=%s vram=%s"
              % (_format_percent(usage), source,
                 _format_temp(_gpu_temp(device, path)),
                 _format_percent(_gpu_fan(device, path)),
                 _read_text(os.path.join(device,
                                         "mem_info_vram_total")) or "-"))
        triplet = _find_freq_triplet(path, device)
        print("           freq triplet: %s" % (triplet or "none found",))
        nodes = sorted(set(_hwmon_paths(device)) | set(_sibling_hwmons(device, path)))
        if not nodes:
            print("           hwmon: none")
        for node in nodes:
            try:
                files = sorted(os.listdir(node))
            except OSError:
                files = []
            readings = []
            for probe in ("temp1_input", "temp2_input", "pwm1", "fan1_input",
                          "fan1_max", "power1_average"):
                value = _read_int(os.path.join(node, probe))
                if value is not None:
                    readings.append("%s=%s" % (probe, value))
            print("           hwmon %s: %s"
                  % (os.path.basename(node), ", ".join(readings) or
                     "(%d files, no temp/pwm/fan)" % len(files)))
        names = [n for n in os.listdir(device) if "freq" in n or "busy" in n]
        if names:
            print("           device freq/busy files: %s" % ", ".join(sorted(names)))
    igpu, dgpu = _scan_gpus()
    print("scan: igpu=%s dgpu=%s"
          % (igpu.vendor if igpu else "-", dgpu.vendor if dgpu else "-"))

    smi = shutil.which("nvidia-smi") or ""
    print("nvidia: /dev/nvidia0=%s nvidia-smi=%s"
          % ("yes" if os.path.exists("/dev/nvidia0") else "no",
             smi or "(missing)"))
    for info in sorted(glob.glob("/proc/driver/nvidia/gpus/*/information")):
        print("  %s: %s" % (info, _read_text(info).strip()[:200]))
    if smi:
        try:
            result = subprocess.run(
                [smi, "--query-gpu=name,utilization.gpu,temperature.gpu,"
                      "fan.speed,memory.used,memory.total",
                 "--format=csv"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=5, text=True)
            print("  exit=%d output=%r"
                  % (result.returncode, result.stdout.strip()[:400]))
        except Exception as exc:  # noqa: BLE001
            print("  query failed: %s" % exc)
    return 0


VARIANTS = (
    # name, syscount, mem usage/temp/fan, delivery, note
    ("vendor", 3, (31, 41, 51), "together",
     "vendor-exact: syscount=3, both pages back to back"),
    ("syscount4", 4, (33, 43, 53), "together",
     "syscount=4, both pages back to back (what we ship today)"),
    ("packed", 2, (35, 45, 55), "packed",
     "single page: CPU+Memory in one 0x10 packet"),
    ("split", 3, (37, 47, 57), "split",
     "page1 in one frame, page2 alone 1.5s later"),
    ("items", 3, (39, 49, 59), "items",
     "pages first, then the 0x11 per-item packets alone"),
)


def _variant_frames(builder, spec):
    """Return [(packets, wait_after)] for one layout hypothesis."""
    name, syscount, (mem_u, mem_t, mem_f), delivery, _note = spec
    ts = proto.panel_timestamp()
    head = [builder.home(ts), builder.time(ts)]
    cpu = SystemItem(0, 11, 12, 13, "CPU")
    igpu = SystemItem(1, 21, 22, 23, "iGPU")
    mem = SystemItem(2, mem_u, mem_t, mem_f, "Memory")
    dgpu = SystemItem(3, 32, 42, 52, "DGPU")

    if delivery == "packed":
        return [(head + [builder.system_page(1, 1, syscount, [cpu, mem])], 0.0)]

    page1 = builder.system_page(1, 2, syscount, [cpu, igpu])
    page2 = builder.system_page(2, 2, syscount,
                               [mem, dgpu] if syscount > 3 else [mem])
    if delivery == "together":
        return [(head + [page1, page2], 0.0)]
    if delivery == "split":
        return [(head + [page1], 1.5), (head + [page2], 0.0)]
    # "items": pages first, then the per-item refreshes on their own
    items = [builder.system_item(0, 11, 12, 13),
             builder.system_item(1, 21, 22, 23),
             builder.system_item(2, mem_u, mem_t, mem_f),
             builder.system_item(3, 32, 42, 52)]
    return [(head + [page1, page2], 1.5), (items, 0.0)]


def _run_variants(args):
    """Cycle the layout hypotheses, each with its own memory number."""
    print("variants: %d hypotheses x %d frames, %.1fs apart"
          % (len(VARIANTS), max(1, args.repeat), args.interval))
    for name, _sc, (u, t, f), _d, note in VARIANTS:
        print("  %-10s mem=%d/%d/%d  %s" % (name, u, t, f, note))
    print("decode: the number left on the Memory card names the winning "
          "variant (31 vendor / 33 syscount4 / 35 packed / 37 split / "
          "39 items); 89 means none landed")

    transport = None
    if not args.mock:
        transport = UsbTransport()
        try:
            transport.open()
        except (UsbError, OSError) as exc:
            _log("cannot open USB device: %s" % exc)
            return 1
    try:
        for index, spec in enumerate(VARIANTS):
            builder = PacketBuilder(args.sequence)
            for packets, wait in _variant_frames(builder, spec):
                for _ in range(max(1, args.repeat)):
                    if transport is None:
                        for packet in packets:
                            print(_mock_packet_line(packet), flush=True)
                    else:
                        _write_frame(transport, packets, args.packet_delay)
                if wait:
                    time.sleep(wait)
            if index + 1 < len(VARIANTS):
                time.sleep(max(0.1, args.interval))
    except (UsbError, OSError) as exc:
        _log("send failed: %s" % exc)
        return 1
    finally:
        if transport is not None:
            transport.close()
    print("done: photograph the panel now")
    return 0


ONLY_CHOICES = ("all", "page1", "page2", "items", "home")


def _filter_packets(packets, only):
    """Keep just the packets named by `--only`, for bisecting the panel.

    Packet layout: A5 5A seq len cmd aim data...  so packet[5] is the aim and
    packet[6] is the first payload byte (the page order).
    """
    if only == "all":
        return packets
    if only == "page1":
        return [p for p in packets if p[5] == 0x10 and p[6] == 1]
    if only == "page2":
        return [p for p in packets if p[5] == 0x10 and p[6] == 2]
    if only == "items":
        return [p for p in packets if p[5] == 0x11]
    if only == "home":
        return [p for p in packets if p[5] in (0x00, 0x01)]
    return packets


def _pattern_snapshot():
    """Every cell gets a unique number so the panel maps bytes to cells 1:1.

    CPU 11/12/13, iGPU 21/22/23, Memory 31/32/33, DGPU 41/42/43,
    disk 111GB/222GB/33C, nic up 44 / down 55 / total 66MB.
    """
    return proto.SystemSnapshot(
        timestamp=proto.panel_timestamp(),
        cpu_usage=11, cpu_temp=12, cpu_fan=13,
        igpu_usage=21, igpu_temp=22, igpu_fan=23,
        mem_usage=31, mem_temp=32, mem_fan=33,
        dgpu_present=True, dgpu_usage=41, dgpu_temp=42, dgpu_fan=43,
        device_name="PATRN", cpu_name="CELLS", os_name="11-43", serial="1:1",
        disks=[proto.DiskItem(0, 111, 222, 33, "DISK")],
        networks=[proto.WlanItem(0, 44, 55, 66 * 1024, (1, 2, 3, 4), "NET")],
    )


def cmd_reset(_args):
    """Reset the panel's USB port, which clears a wedged panel state."""
    transport = UsbTransport()
    try:
        info = transport.discover()
    except (UsbError, OSError) as exc:
        _log("cannot find the panel: %s" % exc)
        return 1
    print("resetting %s (bus=%03d dev=%03d)"
          % (info["node"], info["busnum"], info["devnum"]))
    try:
        fd = os.open(info["node"], os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        _log("cannot open %s: %s (run as root?)" % (info["node"], exc))
        return 1
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    except OSError as exc:
        _log("USBDEVFS_RESET failed: %s" % exc)
        return 1
    finally:
        os.close(fd)
    print("reset ok: the panel should re-enumerate within a few seconds")
    return 0


def cmd_listen(args):
    """Open the device, send nothing, and dump whatever the panel says."""
    seconds = max(1.0, args.seconds)
    print("listen: %.1fs, sending nothing" % seconds)
    transport = UsbTransport()
    try:
        transport.open()
    except (UsbError, OSError) as exc:
        _log("cannot open USB device: %s" % exc)
        return 1
    buffer = b""
    frames = []
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            chunk = transport.read(64)
            if not chunk:
                continue
            buffer += chunk
            if len(buffer) > 4096:
                buffer = buffer[-1024:]
            parsed, buffer = _extract_frames(buffer)
            for frame in parsed:
                frames.append(frame)
                print("  %s raw=%s" % (time.strftime("%H:%M:%S"), _hex(frame)),
                      flush=True)
    except (UsbError, OSError) as exc:
        _log("read failed: %s" % exc)
    finally:
        transport.close()

    if not frames:
        print("no frames: the panel only replies when we send something "
              "(these are acknowledgements, not a poll)")
        return 0
    print("%d frames in %.1fs = %.1f/s" % (len(frames), seconds,
                                           len(frames) / seconds))
    sequences = [frame[2] for frame in frames]
    print("seq first=%d last=%d" % (sequences[0], sequences[-1]))
    aims = {}
    for frame in frames:
        key = (frame[4], frame[5])
        aims[key] = aims.get(key, 0) + 1
    for (cmd, aim), count in sorted(aims.items()):
        print("  cmd=0x%02X aim=0x%02X x%d" % (cmd, aim, count))
    return 0


def cmd_pattern(args):
    """Push a fixed test pattern so the panel can be read back cell by cell."""
    if args.variants:
        return _run_variants(args)
    snapshot = _pattern_snapshot()
    print("pattern: sending the same frame %d times, %.2fs apart"
          % (max(1, args.repeat), args.interval))
    for line in _report_lines(snapshot):
        print(line)
    print("only: %s" % args.only)
    if args.mock:
        packets = _filter_packets(
            _sequence(snapshot, PacketBuilder(args.sequence), args),
            args.only)
        for packet in packets:
            print(_mock_packet_line(packet), flush=True)
        return 0

    transport = UsbTransport()
    try:
        transport.open()
    except (UsbError, OSError) as exc:
        _log("cannot open USB device: %s" % exc)
        return 1
    try:
        for index in range(max(1, args.repeat)):
            packets = _filter_packets(
                _sequence(snapshot, PacketBuilder(args.sequence), args),
                args.only)
            _write_frame(transport, packets, args.packet_delay)
            if index + 1 < max(1, args.repeat):
                time.sleep(max(0.05, args.interval))
    except (UsbError, OSError) as exc:
        _log("send failed: %s" % exc)
        return 1
    finally:
        transport.close()
    print("sent %d frames" % max(1, args.repeat))
    return 0


def cmd_dump(args):
    collector = _make_collector(args)
    collector.warmup()
    snapshot = collector.collect()
    packets = _sequence(snapshot, PacketBuilder(args.sequence), args)

    for line in _report_lines(snapshot, collector):
        print(line)
    for packet in packets:
        print(_dump_packet_line(packet))
    print("packets: %d" % len(packets))
    return 0


def cmd_probe(_args):
    transport = UsbTransport()
    try:
        info = transport.discover()
    except UsbNotFound as exc:
        print("subscreen: no panel found: %s" % exc)
        return 1
    except UsbError as exc:
        print("subscreen: USB discovery failed: %s" % exc)
        return 1

    print("subscreen: found panel")
    print("  sysfs:     %s" % info["sysfs"])
    print("  device:    bus %03d device %03d (%s)"
          % (info["busnum"], info["devnum"], info["node"]))
    print("  interface: %d (%s)"
          % (info["interface"], info["iface_path"] or "n/a"))
    print("  endpoints: OUT %s IN %s"
          % ("0x%02X" % info["out_ep"] if info["out_ep"] else "none",
             "0x%02X" % info["in_ep"] if info["in_ep"] else "none"))
    print("  driver:    %s" % (info["driver"] or "none"))

    claimed = False
    buffer_address = None
    error = None
    try:
        transport.open(info)
        claimed = transport.claimed
        buffer_address = transport.describe().get("io_buffer")
    except (UsbError, OSError) as exc:
        error = exc
    finally:
        transport.close()
    print("  claim:     %s" % ("OK" if claimed else "FAILED"))
    print("  i/o buffer: %s"
          % ("0x%016X" % buffer_address if buffer_address else "n/a"))
    if error is not None:
        print("  error:     %s" % error)
    return 0


def cmd_selftest(args):
    print(_version_line())
    ok = proto.selftest(verbose=True)

    collector = _make_collector(args)
    collector.warmup()
    try:
        snapshot = collector.collect()
    except Exception as exc:  # noqa: BLE001 - report, do not traceback
        print("stats: FAIL (%s)" % exc)
        return 1

    print("stats: cpu=%d%% temp=%s mem=%d%%"
          % (snapshot.cpu_usage, _format_temp(snapshot.cpu_temp),
             snapshot.mem_usage))
    for line in _report_lines(snapshot, collector, summary=False):
        print(line)

    packets = _sequence(snapshot, PacketBuilder(args.sequence), args)
    sequence_ok = True
    for index, packet in enumerate(packets):
        parsed = parse_packet(packet)
        good = (
            parsed is not None
            and parsed.get("crc_ok") is True
            and len(packet) in VALID_PACKET_LENGTHS
            and packet[3] == len(packet) - 3
            and packet[0:2] == proto.MAGIC_BYTES
        )
        if not good:
            print("packet[%02d] aim=0x%02X FAIL len=%d %s"
                  % (index, packet[5], len(packet), _hex(packet)))
            sequence_ok = False
    print("sequence: %s (%d packets)" % ("PASS" if sequence_ok else "FAIL",
                                         len(packets)))
    ok = ok and sequence_ok
    print("overall: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Drive a HarborOS NAS front-panel USB screen "
                    "(VID 5448 / PID 0002).")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "once", "dump", "probe", "selftest",
                                 "temps", "version", "pattern", "gpus",
                                 "listen", "reset"],
                        help="action to perform (default: run)")
    parser.add_argument("--mock", action="store_true",
                        help="do not open USB; print packets instead")
    parser.add_argument("--interval", type=float, default=1.0,
                        metavar="SECONDS", help="update interval (default 1.0)")
    parser.add_argument("--disk", action="append", dest="disks", metavar="PATH",
                        help="disk mount point to report (repeatable, "
                             "replaces defaults)")
    parser.add_argument("--iface", action="append", dest="ifaces", metavar="NAME",
                        help="network interface to report (repeatable, "
                             "replaces defaults)")
    parser.add_argument("--allow-hibernate", action="store_true",
                        help="honour panel hibernate buttons (runs shutdown -h now)")
    parser.add_argument("--wlan-total-unit", default="auto",
                        choices=sorted(TOTAL_UNIT_CHOICES),
                        help="unit for the NIC traffic total (default: auto; "
                             "pin it when checking what the panel honours)")
    parser.add_argument("--no-item-refresh", dest="item_refresh",
                        action="store_false",
                        help="send only the page packets, drop the 0x11 "
                             "per-item refreshes")
    parser.add_argument("--variants", action="store_true",
                        help="pattern only: cycle three SystemPage layout "
                             "hypotheses with distinct numbers")
    parser.add_argument("--system-mode", default="both",
                        choices=("both", "vendor", "legacy"),
                        help="vendor: pages only declare cards and the 0x11 "
                             "items carry every value (what the Windows client "
                             "does); legacy: values inside the page blocks")
    parser.add_argument("--seconds", type=float, default=6.0,
                        help="listen: how long to read without sending")
    parser.add_argument("--card-order", default=None, metavar="LIST",
                        help="SystemPage slot order, e.g. cpu,mem,igpu,dgpu "
                             "(names: cpu igpu mem dgpu; default "
                             "cpu,igpu,mem,dgpu)")
    parser.add_argument("--layout", default="per-page",
                        choices=("per-page", "paired"),
                        help="per-page: one card per page (default; the only "
                             "shape that gives every slot its own values); "
                             "paired: the vendor's two blocks per page, where "
                             "the second slot always mirrors the first")
    parser.add_argument("--no-gpu", action="store_true",
                        help="skip GPU probing entirely (no nvidia-smi, no GPU "
                             "cards)")
    parser.add_argument("--safe", action="store_true",
                        help="vendor-exact output: paired layout, placeholder "
                             "page blocks, 3 cards, 0.1s packet gap, 3s interval")
    parser.add_argument("--syscount", type=int, default=None, metavar="N",
                        help="number of system cards to advertise "
                             "(default: 4 with a discrete GPU, else 3; the "
                             "Windows client always sends 3)")
    parser.add_argument("--only", default="all", choices=ONLY_CHOICES,
                        help="pattern only: send just this subset of packets "
                             "(bisect what the panel accepts)")
    parser.add_argument("--wlan-slots", type=int, default=3, metavar="N",
                        help="adapter slots to fill (default 3, the panel's "
                             "reserved count; unused slots are zeroed so a "
                             "stale duplicate cannot survive)")
    parser.add_argument("--no-lean", dest="lean", action="store_false",
                        help="run: re-send the static pages (home, mode, info, "
                             "wlan user) on every frame instead of every 12th")
    parser.add_argument("--pace", type=float, default=None, metavar="SECONDS",
                        help="run: seconds between packets (default 0.3, which "
                             "matches the panel's own drain rate; a faster "
                             "burst builds a backlog the screen never clears)")
    parser.add_argument("--packet-delay", type=float, default=0.03,
                        metavar="SECONDS",
                        help="pause between packets (default 0.03; the panel "
                             "drops packets when they arrive back to back)")
    parser.add_argument("--repeat", type=int, default=3, metavar="N",
                        help="how many identical frames to push for "
                             "once/pattern (default 3; the panel may ignore "
                             "the first frame after a page change)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="verbose logging")
    parser.add_argument("--sequence", type=int, default=0, metavar="N",
                        help="initial packet sequence number 0-255")
    return parser


def _file_signature(path):
    """Short content hash + mtime, so a deployed file can be identified."""
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()[:12]
        stamp = time.strftime("%Y-%m-%d %H:%M",
                              time.localtime(os.path.getmtime(path)))
    except OSError:
        return "unreadable"
    return "%s @%s" % (digest, stamp)


def _version_line():
    return "version: daemon %s | protocol %s | python %s" % (
        _file_signature(os.path.abspath(__file__)),
        _file_signature(os.path.abspath(getattr(proto, "__file__", ""))),
        sys.version.split()[0])


def cmd_version(args):
    """Print the identity of the files actually being executed."""
    print(_version_line())
    print("daemon  : %s" % os.path.abspath(__file__))
    print("protocol: %s" % os.path.abspath(getattr(proto, "__file__", "?")))
    print("disks.ini: %s (%s)" % (UNRAID_DISKS_INI,
                                  "readable" if _unraid_disk_table()
                                  else "not readable"))
    print("smartctl : %s" % (_find_smartctl() or "not found"))
    return 0


def cmd_temps(args):
    """Explain, disk by disk, where the temperature comes from.

    For every mount it prints the block devices behind it, what hwmon and
    smartctl report, the value Unraid cached in disks.ini, and the verdict the
    daemon ends up using.
    """
    collector = _make_collector(args)
    mounts = _read_mounts()
    table = _unraid_disk_table()
    binary = _find_smartctl()
    print("smartctl: %s" % (binary or "not found"))
    print("disks.ini: %s (%d entries)" % (UNRAID_DISKS_INI, len(table))
          if table else "disks.ini: %s (not readable)" % UNRAID_DISKS_INI)
    for path in collector.disk_paths:
        name = _disk_label(path)
        source = mounts.get(os.path.normpath(path), "")
        fields = table.get(name) or (table.get("flash", {}) if name == "boot" else {})
        print("\n%s -> %s" % (path, source or "(not a block device, e.g. zfs/tmpfs)"))
        if fields:
            print("  disks.ini : device=%s temp=%s spundown=%s  (fallback only)"
                  % (fields.get("device", "?"), fields.get("temp", "?"),
                     fields.get("spundown", "0")))
        if not source.startswith("/dev/"):
            continue
        names = _related_block_devices(os.path.basename(source))
        device = fields.get("device", "")
        if device and device not in names:
            names.append(device)
        if not names:
            print("  no block device found behind this mount")
        answered = {}
        for candidate in sorted(names, key=_is_partition):
            parent = next((other for other in names
                           if other != candidate and candidate.startswith(other)), "")
            hwmon = collector._block_temp_values(candidate)
            physical = candidate.startswith(("sd", "nvme", "hd", "vd", "mmcblk"))
            if hwmon:
                print("  %-10s hwmon=%dC" % (candidate, max(hwmon)))
                continue
            if not physical:
                print("  %-10s not a physical disk (hwmon: none)" % candidate)
                continue
            if parent and parent in answered:
                print("  %-10s skipped, %s already answered" % (candidate, parent))
                continue
            if not binary:
                print("  %-10s hwmon: none, smartctl not installed" % candidate)
                continue
            for device_type in [None] + list(SMART_DEVICE_TYPES):
                result = _run_smartctl(candidate, device_type)
                if result is None:
                    print("  %-10s smartctl: not runnable" % candidate)
                    break
                parsed = _parse_smart_temp(result.stdout)
                label = "-d %s" % device_type if device_type else "default"
                note = _smart_summary(result)
                print("  %-10s smartctl %-8s exit=%d temp=%-4s%s"
                      % (candidate, label, result.returncode,
                         _format_temp(parsed) if parsed is not None else "none",
                         ("  [" + note + "]") if note else ""))
                if parsed is not None:
                    answered[candidate] = parsed
                    break
    print("\n== verdict (what the daemon sends) ==")
    snapshot = collector.collect()
    for disk in snapshot.disks:
        value, origin = collector.temp_sources.get(disk.name, (disk.temp, "?"))
        print("  %-13s %6s via %s"
              % (disk.name, _format_temp(value), origin))
    return 0


def main(argv=None):
    global _VERBOSE
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be greater than 0")
    if not 0 <= args.sequence <= 255:
        parser.error("--sequence must be between 0 and 255")
    _VERBOSE = bool(args.verbose)
    if args.safe:
        # The conservative profile: byte-for-byte what the Windows client
        # sends, and the only shape the panel has never locked up on.
        args.layout = "paired"
        args.system_mode = "vendor"
        args.syscount = 3
        args.packet_delay = max(args.packet_delay, 0.1)
        # Pace, not interval: `run` streams continuously and the packet gap is
        # what keeps the panel from building a backlog.
        args.interval = min(args.interval, 1.0)

    if args.command == "run":
        return cmd_run(args)
    if args.command == "once":
        return cmd_once(args)
    if args.command == "dump":
        return cmd_dump(args)
    if args.command == "pattern":
        return cmd_pattern(args)
    if args.command == "gpus":
        return cmd_gpus(args)
    if args.command == "listen":
        return cmd_listen(args)
    if args.command == "reset":
        return cmd_reset(args)
    if args.command == "probe":
        return cmd_probe(args)
    if args.command == "selftest":
        return cmd_selftest(args)
    if args.command == "temps":
        return cmd_temps(args)
    if args.command == "version":
        return cmd_version(args)
    parser.error("unknown command: %s" % args.command)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
