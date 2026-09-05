#!/usr/bin/env python3
"""
EXPERT 1K-FA - web console (daemon + web server).

The daemon owns the serial port, drives DTR and passes bytes through. Protocol
knowledge lives in index.html - deliberately not here, apart from the OFF frame
used by auto-shutdown.

  ./expert_console.py --port /dev/ttyUSB.pa --raw-port 7373
  ./expert_console.py --simulate
  ./expert_console.py --replay /tmp/expert1k.log

The only dependency is pyserial, and only for --port:
  apt install python3-serial
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Protocol frames. The daemon knows only this much - everything else is in
# index.html.
# --------------------------------------------------------------------------

SYN_PC = 0x55          # PC -> PA
SYN_PA = 0xAA          # PA -> PC
KEY_ON = 0x10
RCU_ON = 0x80
RCU_OFF = 0x81
KEY_OFF = 0x18         # klavesa OFF - jednoznacny prikaz, ne prepinac


def frame(*data: int) -> bytes:
    """Slozi ramec PC -> PA: 0x55 x3, CNT, DATA..., CHK (suma mod 256)."""
    payload = bytes(data)
    return bytes([SYN_PC, SYN_PC, SYN_PC, len(payload)]) + payload + bytes(
        [sum(payload) & 0xFF]
    )


FRAME_OFF = frame(KEY_ON, KEY_OFF)      # 55 55 55 02 10 18 28


# --------------------------------------------------------------------------
# Hub - rozbocovac mezi zdrojem bajtu a vsemi klienty
# --------------------------------------------------------------------------

class Hub:
    """
    The single place every byte flows through.

    Reads from the source and fans out to the SSE clients and the raw TCP
    client. Writes from both directions are serialised, and the ceiling of
    8 commands per second is enforced here (manual p. 8).
    """

    MAX_CMD_PER_SEC = 8

    def __init__(self, source, auto_shutdown_min=0, dtr_mode="level"):
        self.source = source
        self.auto_shutdown_min = auto_shutdown_min
        self.dtr_mode = dtr_mode

        self._subs = []                 # SSE fronty
        self._subs_lock = threading.Lock()
        self._raw_client = None
        self._raw_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._sent = deque(maxlen=self.MAX_CMD_PER_SEC)
        self._last_client_gone = None
        self._shutdown_fired = False
        self.stats = {"rx_bytes": 0, "tx_bytes": 0, "dropped_cmds": 0}

    # -- odber ------------------------------------------------------------

    def subscribe(self):
        q = deque(maxlen=256)
        ev = threading.Event()
        sub = (q, ev)
        with self._subs_lock:
            self._subs.append(sub)
            self._last_client_gone = None
        return sub

    def unsubscribe(self, sub):
        with self._subs_lock:
            if sub in self._subs:
                self._subs.remove(sub)
            if not self._subs:
                self._last_client_gone = time.time()

    def client_count(self):
        with self._subs_lock:
            return len(self._subs)

    def _publish(self, kind, data: bytes):
        line = (kind, data.hex())
        with self._subs_lock:
            subs = list(self._subs)
        for q, ev in subs:
            q.append(line)
            ev.set()

    # -- smer PA -> klienti ----------------------------------------------

    def on_serial_data(self, data: bytes):
        """
        Called by the source for every chunk of received data.

        Reading happens even when nobody is listening - otherwise the OS buffer
        overflows after RCU_ON, because the amplifier keeps streaming.
        """
        self.stats["rx_bytes"] += len(data)
        self._publish("rx", data)
        with self._raw_lock:
            raw = self._raw_client
        if raw is not None:
            try:
                raw.sendall(data)
            except OSError:
                pass

    # -- smer klienti -> PA ----------------------------------------------

    def send(self, data: bytes, rate_limited=True) -> bool:
        """Write to the serial port. Returns False if the rate limit dropped it."""
        if rate_limited:
            now = time.time()
            if len(self._sent) == self._sent.maxlen and now - self._sent[0] < 1.0:
                self.stats["dropped_cmds"] += 1
                return False
            self._sent.append(now)
        with self._write_lock:
            self.source.write(data)
        self.stats["tx_bytes"] += len(data)
        self._publish("tx", data)
        return True

    def send_key(self, code: int) -> bool:
        return self.send(frame(KEY_ON, code))

    # -- raw TCP klient ---------------------------------------------------

    def attach_raw(self, conn) -> bool:
        with self._raw_lock:
            if self._raw_client is not None:
                return False
            self._raw_client = conn
            return True

    def detach_raw(self, conn):
        with self._raw_lock:
            if self._raw_client is conn:
                self._raw_client = None

    def has_raw(self):
        with self._raw_lock:
            return self._raw_client is not None

    # -- auto-shutdown ----------------------------------------------------

    def tick_auto_shutdown(self):
        """
        Powers the amplifier down after N minutes with no client connected.

        In level mode by dropping DTR, otherwise with the OFF command - which is
        unambiguous, unlike OPERATE which toggles, so the daemon needs no
        understanding of packet contents to use it.
        """
        if not self.auto_shutdown_min:
            return
        if self.client_count() or self.has_raw():
            self._shutdown_fired = False
            return
        if self._last_client_gone is None or self._shutdown_fired:
            return
        idle = time.time() - self._last_client_gone
        if idle >= self.auto_shutdown_min * 60:
            log(f"auto-shutdown: {self.auto_shutdown_min} min with no client")
            if self.dtr_mode == "level":
                self.source.set_dtr(False)
            else:
                self.send(FRAME_OFF, rate_limited=False)
            self._shutdown_fired = True


# --------------------------------------------------------------------------
# Zdroje bajtu
# --------------------------------------------------------------------------

class SerialSource:
    """The real serial port. The only source that can drive DTR."""

    def __init__(self, path, baud=9600, dtr_pulse_ms=1000, dtr_on_start=False):
        try:
            import serial
        except ImportError:
            sys.exit("pyserial is missing. Install it: apt install python3-serial")
        self.path = path
        self.baud = baud
        self.dtr_pulse_ms = dtr_pulse_ms
        # exclusive=True sets TIOCEXCL. Without it pyserial opens the port even
        # when ser2net or socat already holds it - both then read from the same
        # device, incoming bytes are split between them at random, and it looks
        # like "writes go out, nothing comes back". A loud error beats silent
        # interleaving.
        try:
            self.ser = serial.Serial(path, baud, timeout=0, exclusive=True)
        except serial.SerialException as e:
            sys.exit(f"Cannot open {path}: {e}\n"
                     f"Held by another process? Try:  sudo fuser -v {path}\n"
                     f"                               systemctl status ser2net")
        # DTR is a LEVEL switch, not an ignition edge. Protocol Rev. 2.0 p. 4
        # says so outright: turning on is "simply raising it at a voltage level
        # greater than +5 Vdc" and takes 3 to 4.5 s; turning off means "this
        # control line has to be reset in its OFF state" and takes about 1 s,
        # because the state is sampled for at least 500 ms. Measured 6.7 s to
        # the first reply, which fits once the RCU handshake is included.
        self.ser.dtr = bool(dtr_on_start)

    def describe(self):
        return f"{self.path} @ {self.baud} 8N1"

    def line_states(self):
        """Modem line states - for diagnosing a link that carries nothing."""
        try:
            return (f"DTR={int(self.ser.dtr)} RTS={int(self.ser.rts)} "
                    f"CTS={int(self.ser.cts)} DSR={int(self.ser.dsr)} "
                    f"CD={int(self.ser.cd)}")
        except (OSError, AttributeError) as e:
            return f"(cannot read: {e})"

    def read_loop(self, hub, stop):
        while not stop.is_set():
            try:
                n = self.ser.in_waiting
                data = self.ser.read(n if n else 1)
            except OSError as e:
                log(f"serial read error: {e}")
                time.sleep(0.5)
                continue
            if data:
                hub.on_serial_data(data)
            else:
                time.sleep(0.01)

    def write(self, data):
        self.ser.write(data)

    def set_dtr(self, on):
        """
        Switch the amplifier on or off.

        After power-up the PA takes 3 to 4.5 s to come up (Rev. 2.0 p. 4;
        measured 6.7 s to the first reply) and resets RCU to OFF in the process
        - data only resumes once the browser watchdog sends RCU_ON again.
        """
        self.ser.dtr = bool(on)
        log(f"DTR -> {'HIGH (on)' if on else 'LOW (off)'}"
            f"  ({self.line_states()})")

    def dtr_state(self):
        try:
            return bool(self.ser.dtr)
        except OSError:
            return False

    def dtr_pulse(self):
        """Ignition pulse - only for --dtr-mode pulse."""
        self.ser.dtr = True
        time.sleep(self.dtr_pulse_ms / 1000.0)
        self.ser.dtr = False
        log(f"DTR pulse {self.dtr_pulse_ms} ms  ({self.line_states()})")


class ReplaySource:
    """
    Replays a capture from `socat -x -v` (or from --record).

    socat's format:
        > 2025/09/04 21:25:00.123456  length=6 from=0 to=5
         55 55 55 01 80 80
    `<` marks the PA -> PC direction, `>` is PC -> PA. `<` is what gets replayed.
    """

    HDR = re.compile(
        r"^\s*([<>])\s+(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)?"
    )
    HEXLINE = re.compile(r"^\s*(?:[0-9a-fA-F]{2}\s+)*[0-9a-fA-F]{2}\s*$")

    def __init__(self, path, direction="<", speed=1.0, loop=True):
        self.path = path
        self.direction = direction
        self.speed = speed
        self.loop = loop
        self.records = self._parse(path, direction)
        if not self.records:
            sys.exit(f"{path} holds no data in direction '{direction}'.")
        log(f"replay: {len(self.records)} records from direction '{direction}'")

    @classmethod
    def _parse(cls, path, want_dir):
        records, cur_dir, cur_ts, buf = [], None, None, []

        def flush():
            if cur_dir == want_dir and buf:
                records.append((cur_ts, bytes(buf)))

        with open(path, "r", errors="replace") as fh:
            for line in fh:
                m = cls.HDR.match(line)
                if m and "length=" in line:
                    flush()
                    buf = []
                    cur_dir = m.group(1)
                    cur_ts = cls._ts(m.group(2))
                    continue
                # Strip socat's ASCII column - only the clean hex part is used
                hexpart = line.split("  ")[0] if "  " in line else line
                if cls.HEXLINE.match(hexpart):
                    buf.extend(int(b, 16) for b in hexpart.split())
            flush()
        return records

    @staticmethod
    def _ts(s):
        if not s:
            return None
        try:
            date, clock = s.split()
            h, m, sec = clock.split(":")
            return int(h) * 3600 + int(m) * 60 + float(sec)
        except ValueError:
            return None

    def describe(self):
        return f"replay {self.path} ({len(self.records)} records)"

    def read_loop(self, hub, stop):
        while not stop.is_set():
            prev = None
            for ts, data in self.records:
                if stop.is_set():
                    return
                if prev is not None and ts is not None:
                    delay = (ts - prev) / self.speed
                    if 0 < delay < 5:
                        time.sleep(delay)
                    else:
                        time.sleep(0.125)
                else:
                    time.sleep(0.125)
                prev = ts if ts is not None else prev
                hub.on_serial_data(data)
            if not self.loop:
                return

    def write(self, data):
        pass                                    # zaznam se necha na pokoji

    def set_dtr(self, on):
        log(f"replay: DTR {'HIGH' if on else 'LOW'} ignored")

    def dtr_state(self):
        return False

    def dtr_pulse(self):
        log("replay: DTR pulse ignored")


class SimulateSource:
    """
    A synthetic amplifier. Answers commands and streams STATUS packets, so the
    GUI including the setup tree can be worked on without hardware.
    """

    BANDS = 10
    # SETUP OPTIONS -> where SET leads from a given item (DISPLAY_CTX)
    SETUP_ENTER = {0: 0x08, 1: 0x09, 2: 0x0D, 3: 0x0E}
    MENU_ITEMS = {0x07: 7, 0x08: 11, 0x09: 6, 0x0A: 15, 0x0B: 2, 0x0C: 4}

    def __init__(self, scenario="normal"):
        self.scenario = scenario
        self.rcu = False
        self.powered = True
        self.operate = False
        self.full = True
        self.tx = False
        self.tune = False
        self.ctx = 0x00
        self.item = 0
        self.band = 2                            # 40 m
        self.input = 0
        self.antenna = 0
        self.cat = 1                             # ICOM
        self.backlight = 200
        self.t0 = time.time()
        self._pending = deque()

    def describe(self):
        return f"simulation ({self.scenario})"

    # -- generovani paketu ------------------------------------------------

    def _flags(self):
        f = 0
        if self.operate:
            f |= 0x02
        if self.tx:
            f |= 0x04
        if self.full:
            f |= 0x10
        if self.tune:
            f |= 0x01
        f |= 0x40                                # BEEP zapnuty
        if self.scenario == "alarm":
            f |= 0x08 | 0x80                     # ALARM + PA_PROT
        return f

    def _measures(self):
        """Smoothly varying readings, so the bar graphs visibly live."""
        import math
        t = time.time() - self.t0
        swing = (math.sin(t * 0.7) + 1) / 2       # 0..1

        if not self.operate:
            drive = int(swing * 40 * 10) if self.tx else 0
            swr = int((1.1 + swing * 0.6) * 100) if self.tx else 0
            return dict(pa=drive, pr=0, va=0, ia=0, swrgain=swr, temp=32)

        cap = 1200.0 if self.full else 600.0
        pa = int(swing * cap * 10) if self.tx else 0
        pr = int(swing * 90 * 10) if self.tx else 0
        va = int((43.5 - swing * 3.0) * 10)
        ia = int(swing * 38 * 10) if self.tx else 5
        gain = int((15.8 + swing * 1.2) * 10)
        temp = 38 + int(swing * 12)
        if self.scenario == "hot":
            temp = 88 + int(swing * 4)
        return dict(pa=pa, pr=pr, va=va, ia=ia, swrgain=gain, temp=temp)

    def _setup_bytes(self):
        s = [0] * 11
        if self.ctx == 0x03:                     # CAT info
            s[0] = self.cat
            s[1] = 0
            s[2] = 3
            s[6], s[7], s[8] = 0x29, 0x11, 0x06  # BCD 29_11_06
            s[9] = ord("B")
        elif self.ctx == 0x05:                   # antenna vs band
            s[0] = 0x00
            for i in range(1, 11):
                s[i] = ((i - 1) % 4) << 4 | (i - 1)
        elif self.ctx == 0x08:                   # SET ANTENNA
            s[1] = self.item
            for i in range(2, 7):
                s[i] = 0x00
        elif self.ctx in (0x07, 0x09, 0x0A, 0x0B, 0x0C):
            s[1] = self.item
        elif self.ctx == 0x0D:                   # MANUAL TUNE
            s[1] = 63                            # L = 6.3 uH
            s[2], s[3] = 0x4D, 0x01              # C = 10 bitu
        elif self.ctx == 0x0E:                   # BACKLIGHT
            s[1] = self.backlight
        elif self.ctx == 0x1D:                   # ALARM HISTORY
            s[0] = (1 << 4) | 2
            s[1], s[2] = 0x11, 0x97
        return s

    # band centre in kHz, so the frequency matches the reported band
    BAND_FREQ = [1840, 3650, 7100, 10120, 14150, 18100,
                 21200, 24930, 28400, 50150]
    BAND_SUB = [12, 40, 60, 70, 75, 82, 88, 96, 100, 120]

    def _status(self):
        m = self._measures()
        freq = self.BAND_FREQ[self.band]
        body = [0x80, self._flags(), self.ctx]
        body += self._setup_bytes()
        body += [
            (self.band << 4) | self.input,
            self.BAND_SUB[self.band],
            freq & 0xFF, (freq >> 8) & 0xFF,
            (self.cat << 4) | self.antenna,
            m["swrgain"] & 0xFF, (m["swrgain"] >> 8) & 0xFF,
            m["temp"],
            m["pa"] & 0xFF, (m["pa"] >> 8) & 0xFF,
            m["pr"] & 0xFF, (m["pr"] >> 8) & 0xFF,
            m["va"] & 0xFF, (m["va"] >> 8) & 0xFF,
            m["ia"] & 0xFF, (m["ia"] >> 8) & 0xFF,
        ]
        return (bytes([SYN_PA] * 3) + bytes([len(body)]) + bytes(body)
                + bytes([sum(body) & 0xFF]))

    @staticmethod
    def _ack(code=0x06):
        return bytes([SYN_PA] * 3 + [0x01, code, code])

    # -- responding to commands -------------------------------------------

    def write(self, data):
        i = 0
        while i < len(data) - 3:
            if data[i] == SYN_PC and data[i + 1] == SYN_PC and data[i + 2] == SYN_PC:
                cnt = data[i + 3]
                body = data[i + 4:i + 4 + cnt]
                self._command(body)
                i += 4 + cnt + 1
            else:
                i += 1

    def _command(self, body):
        if not body:
            return
        op = body[0]
        if op == RCU_ON:
            self.rcu = True
            self._pending.append(self._ack())
        elif op == RCU_OFF:
            self.rcu = False
            self._pending.append(self._status())
        elif op == KEY_ON and len(body) > 1:
            self._key(body[1])
            self._pending.append(self._ack() if self.rcu else self._status())
        else:
            self._pending.append(self._ack(0xFF))

    def _key(self, code):
        in_setup = 0x07 <= self.ctx <= 0x0E
        if code == KEY_OFF:
            self.powered = False
            self.rcu = False                     # po vypnuti se RCU resetuje
            self.ctx = 0x1E
        elif code == 0x1C:                       # OPERATE - prepinac
            self.operate = not self.operate
            self.ctx = 0x01 if self.operate else 0x00
        elif code == 0x1A:                       # MODE FULL/HALF
            self.full = not self.full
        elif code == 0x1B:                       # DISPLAY
            self.ctx = 0x02 if self.ctx == 0x01 else 0x01
        elif code == 0x2F:                       # SET
            if not in_setup:
                self.ctx, self.item = 0x07, 0
            elif self.ctx == 0x07:
                if self.item in self.SETUP_ENTER:
                    self.ctx, self.item = self.SETUP_ENTER[self.item], 0
                elif self.item == 6:             # QUIT
                    self.ctx, self.item = 0x00, 0
            elif self.ctx == 0x09 and self.item in (1, 3):
                self.ctx = 0x0B if self.item == 1 else 0x0A
                self.item = 0
            else:
                self.ctx, self.item = 0x07, 0
        elif code in (0x2D, 0x2E):               # sipky
            if in_setup:
                n = self.MENU_ITEMS.get(self.ctx, 1)
                self.item = (self.item + (1 if code == 0x2E else -1)) % n
            elif self.ctx == 0x0E:
                self.backlight = max(0, min(255, self.backlight
                                            + (8 if code == 0x2E else -8)))
        elif code in (0x29, 0x2A):               # BAND -/+
            self.band = (self.band + (1 if code == 0x2A else -1)) % self.BANDS
        elif code == 0x2B:                       # ANT
            self.antenna = (self.antenna + 1) % 5
        elif code == 0x2C:                       # CAT
            self.cat = (self.cat + 1) % 6
        elif code == 0x28:                       # IN
            self.input ^= 1
        elif code == 0x34:                       # TUNE
            self.tune = True
            threading.Timer(2.0, lambda: setattr(self, "tune", False)).start()

    def read_loop(self, hub, stop):
        tick = 0
        while not stop.is_set():
            time.sleep(0.125)                    # 8 paketu/s
            while self._pending:
                hub.on_serial_data(self._pending.popleft())
            if self.rcu and self.powered:
                tick += 1
                # an occasional short TX, to keep the bar graphs moving
                self.tx = self.operate and (tick % 40) < 16
                hub.on_serial_data(self._status())

    def set_dtr(self, on):
        self.powered = bool(on)
        if on:
            self.operate = False
            self.ctx = 0x00
        else:
            self.rcu = False                     # po vypnuti se RCU resetuje
        log(f"simulation: DTR {'HIGH -> on' if on else 'LOW -> off'}")

    def dtr_state(self):
        return self.powered

    def dtr_pulse(self):
        self.set_dtr(True)


# --------------------------------------------------------------------------
# Zapis zaznamu (--record) ve formatu, ktery zvladne --replay
# --------------------------------------------------------------------------

class Recorder:
    def __init__(self, path):
        self.fh = open(path, "w")
        self.lock = threading.Lock()

    def write(self, direction, data):
        ts = time.strftime("%Y/%m/%d %H:%M:%S") + f".{int(time.time() % 1 * 1e6):06d}"
        with self.lock:
            self.fh.write(f"{direction} {ts}  length={len(data)}\n")
            for i in range(0, len(data), 16):
                self.fh.write(" " + " ".join(f"{b:02x}" for b in data[i:i + 16]) + "\n")
            self.fh.flush()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))

# The manifest lets the console run as a standalone application - no address
# bar, no tabs. Browsers only offer installation over HTTPS or from localhost;
# over plain http across the network, --app= mode or full screen remain.
MANIFEST = json.dumps({
    "name": "EXPERT 1K-FA",
    "short_name": "1K-FA",
    "start_url": ".",
    "scope": ".",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#12151a",
    "theme_color": "#12151a",
    "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml",
               "purpose": "any maskable"}],
}, indent=2).encode()

ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
    '<rect width="192" height="192" rx="34" fill="#12151a"/>'
    '<rect x="22" y="52" width="148" height="16" rx="4" fill="#3fa96e"/>'
    '<rect x="22" y="80" width="104" height="16" rx="4" fill="#cfa54e"/>'
    '<rect x="22" y="108" width="132" height="16" rx="4" fill="#8e9aab"/>'
    '<text x="96" y="164" font-family="monospace" font-size="34" font-weight="700"'
    ' fill="#4fd6e8" text-anchor="middle">1K-FA</text>'
    '</svg>').encode()

# Filled in by --bundle; None means the page is read from index.html on disk.
# The script never rewrites itself - a bundle is an output, not a source.
BUNDLED_HTML = None


def load_html():
    """
    The page comes from index.html next to the script, so editing it means
    edit and F5. A bundle produced by --bundle carries the page inside itself
    and needs no companion file.
    """
    if BUNDLED_HTML is not None:
        return BUNDLED_HTML
    path = os.path.join(HERE, "index.html")
    if not os.path.exists(path):
        sys.exit(f"index.html not found next to {os.path.basename(__file__)}.\n"
                 f"Expected at: {path}\n"
                 f"A standalone copy can be made with:  --bundle FILE")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub = None
    args = None

    def log_message(self, *a):
        pass                                     # bez sumu do konzole

    # -- pomocne ----------------------------------------------------------

    def _send(self, code, ctype, body: bytes, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routy ------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            self._send(200, "text/html; charset=utf-8", load_html().encode())
        elif path == "/stream":
            self._stream()
        elif path == "/health":
            self._json(self._health())
        elif path == "/manifest.webmanifest":
            self._send(200, "application/manifest+json", MANIFEST)
        elif path == "/icon.svg":
            self._send(200, "image/svg+xml", ICON_SVG)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/key":
            code = self._body().get("code")
            if not isinstance(code, int) or not 0 <= code <= 255:
                return self._json({"error": "code must be 0-255"}, 400)
            ok = self.hub.send_key(code)
            self._json({"sent": ok, "rate_limited": not ok})
        elif path == "/power":
            body = self._body()
            src = self.hub.source
            if "dtr" in body:                      # urovnove rizeni
                src.set_dtr(bool(body["dtr"]))
                self._json({"dtr": src.dtr_state()})
            elif body.get("dtr_pulse"):            # zapalovaci pulz
                threading.Thread(target=src.dtr_pulse, daemon=True).start()
                self._json({"pulsing": True})
            else:
                self._json({"error": "expected dtr: true/false"}, 400)
        elif path == "/raw":
            hexs = self._body().get("hex", "")
            try:
                data = bytes.fromhex(hexs)
            except ValueError:
                return self._json({"error": "malformed hex"}, 400)
            ok = self.hub.send(data)
            self._json({"sent": ok, "rate_limited": not ok})
        else:
            self._json({"error": "not found"}, 404)

    def _health(self):
        return {
            "source": self.hub.source.describe(),
            "clients": self.hub.client_count(),
            "raw_client": self.hub.has_raw(),
            "auto_shutdown_min": self.hub.auto_shutdown_min,
            "dtr_mode": self.args.dtr_mode,
            "dtr": self.hub.source.dtr_state(),
            "stats": self.hub.stats,
        }

    def _stream(self):
        """SSE: every chunk from the serial port goes out as an rx / tx event."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")     # kvuli proxy
        self.end_headers()

        sub = self.hub.subscribe()
        q, ev = sub
        try:
            self._sse("hello", json.dumps(self._health()))
            last_ping = time.time()
            while True:
                ev.wait(timeout=1.0)
                ev.clear()
                while q:
                    kind, hexdata = q.popleft()
                    self._sse(kind, hexdata)
                if time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(sub)

    def _sse(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
        self.wfile.flush()


# --------------------------------------------------------------------------
# Raw TCP - so expert-console.sh keeps working with the original application
# --------------------------------------------------------------------------

def raw_server(hub, port, listen, stop):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen, port))
    srv.listen(1)
    srv.settimeout(1.0)
    log(f"raw TCP on {listen}:{port} (DTR is not asserted)")

    while not stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        if not hub.attach_raw(conn):
            log(f"raw: refused {addr} - a client is already connected")
            conn.close()
            continue
        log(f"raw: connected {addr}")
        threading.Thread(target=_raw_client, args=(hub, conn, addr),
                         daemon=True).start()
    srv.close()


def _raw_client(hub, conn, addr):
    try:
        conn.settimeout(1.0)
        while True:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            # No rate limit - the original application paces itself
            hub.send(data, rate_limited=False)
    except OSError:
        pass
    finally:
        hub.detach_raw(conn)
        conn.close()
        log(f"raw: disconnected {addr}")


# --------------------------------------------------------------------------

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def do_bundle(dest):
    """
    Write a standalone copy with index.html folded in.

    The bundle is a build artifact: one file to drop on any machine and run.
    The script deliberately does not rewrite itself - that kept two copies of
    the page around, which then drifted apart whenever the step was forgotten.
    """
    if BUNDLED_HTML is not None:
        sys.exit("this file is already a bundle; run --bundle from the source.")
    html_path = os.path.join(HERE, "index.html")
    if not os.path.exists(html_path):
        sys.exit(f"index.html not found at {html_path}")
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        src = fh.read()

    # Anchored to the start of a line: the same text also appears indented
    # inside this very function, and a plain substring search finds both.
    marker = re.compile(r"^BUNDLED_HTML = None$", re.M)
    if len(marker.findall(src)) != 1:
        sys.exit("cannot locate the BUNDLED_HTML assignment in the source")
    # A normal (non-raw) string: doubled backslashes and escaped triple quotes
    # come back as the original characters when loaded. In a raw string they
    # would stay as written.
    safe = html.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    out = marker.sub(lambda _: 'BUNDLED_HTML = """' + safe + '"""', src, count=1)

    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(out)
    os.chmod(dest, 0o755)
    print(f"{dest}: {len(out) // 1024} K, with {len(html) // 1024} K of index.html "
          f"folded in - runs on its own")


def main():
    p = argparse.ArgumentParser(description="EXPERT 1K-FA - web console")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--port", help="serial port, e.g. /dev/ttyUSB.pa")
    src.add_argument("--simulate", action="store_true", help="synthetic amplifier, no hardware needed")
    src.add_argument("--replay", metavar="FILE", help="replay a socat -x -v capture")
    p.add_argument("--baud", type=int, default=9600,
                   help="serial speed; the 1K-FA does not use anything else")
    p.add_argument("--listen", default="127.0.0.1",
                   help="bind address for both HTTP and the raw port; use "
                        "0.0.0.0 to reach the console from the network")
    p.add_argument("--http-port", type=int, default=8080,
                   help="port the console is served on")
    p.add_argument("--raw-port", type=int, default=0,
                   help="raw TCP port for the original application, e.g. 7373")
    p.add_argument("--auto-shutdown", type=int, default=0, metavar="MIN",
                   help="power down after MIN minutes with no client (0 = disabled)")
    p.add_argument("--dtr-pulse-ms", type=int, default=1000,
                   help="ignition pulse length (the manual says 200, but some "
                        "adapters need more)")
    p.add_argument("--dtr-mode", choices=["level", "pulse"], default="level",
                   help="level: DTR high = on, low = off (per protocol "
                        "Rev. 2.0). pulse: ignition pulse per Rev. 1.0 - try "
                        "this if level does not work")
    p.add_argument("--dtr-on-start", action="store_true",
                   help="power the amplifier up as the daemon starts")
    p.add_argument("--record", metavar="FILE", help="write traffic to a file")
    p.add_argument("--replay-speed", type=float, default=1.0,
                   help="replay rate; 0.5 runs a capture at half speed")
    p.add_argument("--scenario", default="normal",
                   choices=["normal", "alarm", "hot"], help="only for --simulate")
    p.add_argument("--bundle", metavar="FILE",
                   help="write a standalone copy with index.html folded in "
                        "and exit; the bundle needs no companion files")
    args = p.parse_args()

    if args.bundle:
        return do_bundle(args.bundle)

    if args.port:
        source = SerialSource(args.port, args.baud, args.dtr_pulse_ms,
                              args.dtr_on_start)
    elif args.replay:
        source = ReplaySource(args.replay, speed=args.replay_speed)
    else:
        source = SimulateSource(args.scenario)
        if not args.simulate:
            log("Neither --port nor --replay given, starting the simulator.")

    hub = Hub(source, args.auto_shutdown, args.dtr_mode)

    if args.record:
        rec = Recorder(args.record)
        orig_rx, orig_tx = hub.on_serial_data, hub.send

        def rx(data):
            rec.write("<", data)
            orig_rx(data)

        def tx(data, rate_limited=True):
            ok = orig_tx(data, rate_limited)
            if ok:
                rec.write(">", data)
            return ok

        hub.on_serial_data, hub.send = rx, tx
        log(f"recording to {args.record}")

    stop = threading.Event()
    threading.Thread(target=source.read_loop, args=(hub, stop), daemon=True).start()

    if args.raw_port:
        threading.Thread(target=raw_server,
                         args=(hub, args.raw_port, args.listen, stop),
                         daemon=True).start()

    def watchdog():
        while not stop.is_set():
            time.sleep(5)
            hub.tick_auto_shutdown()

    threading.Thread(target=watchdog, daemon=True).start()

    Handler.hub, Handler.args = hub, args
    httpd = ThreadingHTTPServer((args.listen, args.http_port), Handler)
    httpd.daemon_threads = True

    load_html()          # fail now, not on the first page request
    log(f"source: {source.describe()}")
    if hasattr(source, "line_states"):
        log(f"lines: {source.line_states()}")
    if args.port:
        log(f"DTR mode: {args.dtr_mode}"
            + ("  (high = on, low = off)"
               if args.dtr_mode == "level" else ""))
        if args.dtr_mode == "level" and not args.dtr_on_start:
            log("amplifier is off - switch it on with the ON button in the console")
    log(f"console: http://{args.listen}:{args.http_port}/")
    if args.auto_shutdown:
        log(f"auto-shutdown after {args.auto_shutdown} min with no client")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        stop.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
