#!/usr/bin/env python3
"""
EXPERT 1K-FA - web console (daemon + web server).

The daemon owns the serial port, drives DTR and passes bytes through. The full
protocol - the setup tree, every DISPLAY_CTX, the bar graphs - lives in
index.html and only there.

The daemon decodes the little it needs to stand on its own: the RCU_ON
watchdog, and the handful of STATUS fields TrxNet publishes. That duplication
is deliberate and bounded; test/decode_test.py checks it against the same
test/fixture.log the JavaScript decoder is tested with.

  ./expert_console.py --port /dev/ttyUSB.pa --raw-port 7373
  ./expert_console.py --simulate
  ./expert_console.py --replay /tmp/expert1k.log
  ./expert_console.py --port /dev/ttyUSB.pa --trxnet --trxnet-subscribe \
                      --trxnet-allow "705.01" --trxnet-freq-from "OI3.02"

The only dependency is pyserial, and only for --port:
  apt install python3-serial
"""

import argparse
import json
import os
import re
import socket
import struct
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
CAT_232 = 0x82         # remote tuning: 0x82 LO HI, frequency in kHz
KEY_OFF = 0x18         # klavesa OFF - jednoznacny prikaz, ne prepinac

# Keys the daemon presses on its own. The rest stay in index.html.
KEY_MODE = 0x1A        # PWR-L / PWR-H  - a toggle, not a setter
KEY_OPERATE = 0x1C     # STANDBY / OPERATE - a toggle, not a setter
KEY_TUNE = 0x34        # momentary


def frame(*data: int) -> bytes:
    """Slozi ramec PC -> PA: 0x55 x3, CNT, DATA..., CHK (suma mod 256)."""
    payload = bytes(data)
    return bytes([SYN_PC, SYN_PC, SYN_PC, len(payload)]) + payload + bytes(
        [sum(payload) & 0xFF]
    )


FRAME_OFF = frame(KEY_ON, KEY_OFF)      # 55 55 55 02 10 18 28
FRAME_RCU_ON = frame(RCU_ON)            # 55 55 55 01 80 80


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
        self._taps = []                 # in-process observers of the RX stream
        self._last_rcu = 0.0            # last RCU_ON the watchdog sent
        self.last_rx = 0.0              # time of the last byte from the PA
        self.last_activity = None       # someone used the amplifier (see below)
        self.stats = {"rx_bytes": 0, "tx_bytes": 0, "dropped_cmds": 0}

    def add_tap(self, cb):
        """
        Register an in-process observer of received bytes.

        Unlike an SSE subscriber a tap is not a client: it does not keep
        auto-shutdown at bay and does not appear in the client count. The
        status decoder feeding TrxNet hangs off this.
        """
        self._taps.append(cb)

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
        self.last_rx = time.time()
        self._publish("rx", data)
        with self._raw_lock:
            raw = self._raw_client
        if raw is not None:
            try:
                raw.sendall(data)
            except OSError:
                pass
        for tap in self._taps:
            try:
                tap(data)
            except Exception as e:                # a tap must never stop the flow
                log(f"tap error: {e}")

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

    # -- RCU session ------------------------------------------------------

    RCU_QUIET_S = 1.5

    def tick_rcu(self):
        """
        Restart the telemetry stream whenever it falls silent.

        The amplifier resets RCU to OFF on power-up, so the only way to get
        packets flowing - after a boot, after a glitch, at all - is to keep
        asking. This used to live in the browser alone; the daemon needs its
        own copy to be of any use with no page open. index.html keeps its
        watchdog as a fallback, and at 1.5 s apart the two cost 1.3 frames per
        second against a ceiling of 8.
        """
        now = time.time()
        if now - self.last_rx < self.RCU_QUIET_S:
            return
        if now - self._last_rcu < self.RCU_QUIET_S:
            return
        self._last_rcu = now
        self.send(FRAME_RCU_ON)

    # -- auto-shutdown ----------------------------------------------------

    def note_activity(self):
        """
        Someone used the amplifier from outside the browser.

        Presence deliberately does not count, only traffic: a TrxNet peer that
        merely announces itself every 30 s has no business holding a kilowatt
        up, while a transceiver sending its frequency plainly does.
        """
        self.last_activity = time.time()
        self._shutdown_fired = False

    def tick_auto_shutdown(self):
        """
        Powers the amplifier down after N minutes with no client and no use.

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
        since = self._last_client_gone
        if self.last_activity is not None:
            since = max(since, self.last_activity)
        idle = time.time() - since
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
        self.cat_freq = None                     # set by CAT_232, kHz
        self.cat_sub = None
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
        # A frequency set over CAT_232 wins until the band is moved by hand
        freq = self.BAND_FREQ[self.band] if self.cat_freq is None else self.cat_freq
        sub = self.BAND_SUB[self.band] if self.cat_sub is None else self.cat_sub
        body = [0x80, self._flags(), self.ctx]
        body += self._setup_bytes()
        body += [
            (self.band << 4) | self.input,
            sub,
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
        elif op == CAT_232 and len(body) > 2:
            self._cat_232(body[1] | (body[2] << 8))
            self._pending.append(self._ack() if self.rcu else self._status())
        else:
            self._pending.append(self._ack(0xFF))

    # RS-232 is menu index 4 in Rev. 1.0, which is the revision this simulator
    # reports. A real amplifier acts on CAT_232 only with that setting
    # (protocol p. 7), so neither does this one - the precondition is worth
    # being able to test.
    CAT_RS232 = 4

    def _cat_232(self, khz):
        if self.cat != self.CAT_RS232:
            return
        sub = sub_band_for(khz)
        if sub is None:
            return                               # no band of ours down there
        self.band = max(i for i, s in enumerate(SUB_BAND_START) if s <= sub)
        self.cat_freq, self.cat_sub = khz, sub

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
            self.cat_freq = self.cat_sub = None  # moved by hand, drop the CAT value
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
# Minimal STATUS decoder
#
# Only what the daemon itself needs. index.html decodes the whole record - the
# setup tree, every DISPLAY_CTX, the alarm texts - and stays the reference.
# The two are kept honest by test/decode_test.py and test/decode.test.js
# running against the same test/fixture.log.
# --------------------------------------------------------------------------

BANDS_M = (160, 80, 40, 30, 20, 17, 15, 12, 10, 6)


class Framer:
    """
    Splits the byte stream into frames: 0xAA x3, CNT, DATA..., CHK.

    The payload may contain 0xAA, so the marker alone proves nothing and the
    checksum decides. On a mismatch we advance a single byte and try again.
    Same rules as the framer in index.html, including the CNT bound - without
    it two stray 0xAA bytes read as CNT=170 and the framer waits forever for
    175 bytes that never come.
    """

    MAX_CNT = 64
    MAX_BUF = 4096

    def __init__(self, on_frame):
        self.buf = b""
        self.on_frame = on_frame
        self.stats = {"ok": 0, "bad": 0, "resync": 0}

    def push(self, chunk: bytes):
        self.buf += chunk
        b = self.buf
        i = 0
        while i + 5 <= len(b):
            if not (b[i] == SYN_PA and b[i + 1] == SYN_PA and b[i + 2] == SYN_PA):
                i += 1
                continue
            cnt = b[i + 3]
            if cnt > self.MAX_CNT:               # false marker
                self.stats["resync"] += 1
                i += 1
                continue
            total = 4 + cnt + 1
            if i + total > len(b):
                break                            # rest arrives later
            body = b[i + 4:i + 4 + cnt]
            if sum(body) & 0xFF == b[i + 4 + cnt]:
                self.stats["ok"] += 1
                self.on_frame(body)
                i += total
            else:
                self.stats["bad"] += 1
                self.stats["resync"] += 1
                i += 1
        rest = b[i:]
        if len(rest) > self.MAX_BUF:
            rest = rest[-128:]
        self.buf = rest


def swr_from(pf, pr):
    """
    SWR out of forward and reflected power: r = sqrt(Pr/Pf), SWR = (1+r)/(1-r).

    In OPERATE the amplifier does not send SWR - bytes 19-20 carry gain there -
    so it has to be calculated. Below a few watts the ratio is noise, and None
    means "no answer" rather than a made-up number.
    """
    if not pf > 5 or pr < 0 or pr >= pf:
        return None
    r = (pr / pf) ** 0.5
    return (1 + r) / (1 - r)


def decode_status(p: bytes):
    """
    Decode a STATUS record into the fields the daemon needs, or None.

    The status code identifies the protocol revision:
        0x80        - Rev. 1.0 (firmware 06_11_06_x)
        0xA0 / 0xA1 - Rev. 2.0 (firmware >= 07_07_07_M), bit 0 = startup mode
    ACK/NAK/UNK are one byte long and are not statuses; anything else short or
    unrecognised is not ours to interpret.
    """
    if len(p) < 30:
        return None
    rev = 1 if p[0] == 0x80 else 2 if (p[0] & 0xFE) == 0xA0 else 0
    if not rev:
        return None

    def u16(i):
        return p[i] | (p[i + 1] << 8)

    f = p[1]
    s = {
        "rev": rev,
        "flags": f,                              # raw FLAGS byte, bits 0-6 used
        "tx": bool(f & 0x04),
        "operate": bool(f & 0x02),
        "band": p[14] >> 4,
        "cat": p[18] >> 4,
        "temp": p[21],
        "fwd": u16(22) / 10.0,                   # W
        "ref": u16(24) / 10.0,                   # W
    }
    # Bytes 19-20 carry two different things, selected by FLAGS bit 1
    raw = u16(19)
    if s["operate"]:
        s["swr"] = swr_from(s["fwd"], s["ref"])
    else:
        s["swr"] = None if raw == 0 else float("inf") if raw == 9999 else raw / 100.0
    return s


# The tuner's sub-bands, from the user's manual section 19 (p. 70) - the
# protocol document gives only the index ranges, not the frequencies. 127
# entries, indices 0..126, each the CENTRAL frequency of one sub-band in kHz.
# Written out rather than generated so it can be checked against the manual
# line by line: the steps are regular per band except on 17 m and 12 m.
SUB_CENTER_KHZ = (
    1785, 1795, 1805, 1815, 1825, 1835, 1845, 1855, 1865, 1875, 1885, 1895,
    1905, 1915, 1925, 1935, 1945, 1955, 1965, 1975, 1985, 1995, 2005, 2015,
    3470, 3490, 3510, 3530, 3550, 3570, 3590, 3610, 3630, 3650, 3670, 3690,
    3710, 3730, 3750, 3770, 3790, 3810, 3830, 3850, 3870, 3890, 3910, 3930,
    3950, 3970, 3990, 4010, 4030,
    6963, 6988, 7013, 7038, 7063, 7088, 7113, 7138, 7163, 7188, 7213, 7238,
    7263, 7288, 7313, 7338,
    10075, 10125, 10175,
    13975, 14025, 14075, 14125, 14175, 14225, 14275, 14325, 14375,
    18075, 18125, 18165,
    20975, 21025, 21075, 21125, 21175, 21225, 21275, 21325, 21375, 21425,
    21475,
    24891, 24963, 25038,
    27950, 28050, 28150, 28250, 28350, 28450, 28550, 28650, 28750, 28850,
    28950, 29050, 29150, 29250, 29350, 29450, 29550, 29650, 29750,
    49750, 50250, 50750, 51250, 51750, 52250, 52750, 53250, 53750, 54250,
)

# First index of each band in SUB_CENTER_KHZ, in the band order of BANDS_M.
SUB_BAND_START = (0, 24, 53, 69, 72, 81, 84, 95, 98, 117)


def sub_band_for(khz):
    """
    The tuner's sub-band index for a frequency in kHz, or None if out of reach.

    Nearest centre rather than computed edges - that handles the two irregular
    bands (17 m steps 50 then 40, 12 m 72 then 75) with no special cases,
    because between two centres the nearer one wins by definition.

    Only the outer edges need a test: below the first centre of a band or above
    the last one there is nothing to tune, and the gaps between bands are wide.
    Answering with the nearest centre anyway would tune 60 m to the top of 80 m.
    """
    best = min(range(len(SUB_CENTER_KHZ)),
               key=lambda i: abs(SUB_CENTER_KHZ[i] - khz))
    band = max(i for i, start in enumerate(SUB_BAND_START) if start <= best)
    lo = SUB_BAND_START[band]
    hi = (SUB_BAND_START[band + 1] - 1 if band + 1 < len(SUB_BAND_START)
          else len(SUB_CENTER_KHZ) - 1)
    if best == lo and khz < SUB_CENTER_KHZ[lo]:
        step = SUB_CENTER_KHZ[lo + 1] - SUB_CENTER_KHZ[lo] if hi > lo else 0
        return None if SUB_CENTER_KHZ[lo] - khz > step / 2 else best
    if best == hi and khz > SUB_CENTER_KHZ[hi]:
        step = SUB_CENTER_KHZ[hi] - SUB_CENTER_KHZ[hi - 1] if hi > lo else 0
        return None if khz - SUB_CENTER_KHZ[hi] > step / 2 else best
    return best


class StatusTap:
    """
    Frames and decodes the PA's stream, and keeps the latest picture of it.

    Hangs off Hub.add_tap, so it sees the same bytes as the browser and needs
    nothing from it. on_status is called for every decoded record - that is
    what drives publishing, so the cadence follows the amplifier rather than a
    timer of our own.
    """

    LINK_TIMEOUT = 3.0                           # s of silence = link is down

    def __init__(self, on_status=None):
        self.on_status = on_status
        self.framer = Framer(self._frame)
        self.last = None
        self.last_at = 0.0
        self.lock = threading.Lock()

    def feed(self, data: bytes):
        self.framer.push(data)

    def _frame(self, body: bytes):
        s = decode_status(body)
        if s is None:
            return
        with self.lock:
            self.last = s
            self.last_at = time.time()
        if self.on_status:
            self.on_status(s)

    def snapshot(self):
        """
        The last status, whether it is still fresh, and when it arrived.

        The arrival time is not a nicety: a command loop that presses a toggle
        key has to know whether the amplifier has spoken *since* the keystroke,
        and a wall clock cannot tell it that.
        """
        with self.lock:
            if self.last is None:
                return None, False, 0.0
            return (self.last,
                    time.time() - self.last_at < self.LINK_TIMEOUT,
                    self.last_at)


# --------------------------------------------------------------------------
# TrxNet
#
# A Python peer for the TrxNet network used across the remoteQTH device family:
# UDP broadcast discovery plus a minimal CoAP, no broker and no router. The
# wire format follows the C++ library and the passive sniffer that ships with
# it - TrxNet/monitor/monitor.py, functions parse_discovery / parse_coap /
# build_coap / build_ack. Keep the two comparable.
#
# Nothing here knows about amplifiers; ExpertBridge below does the joining.
# --------------------------------------------------------------------------

class TrxPeer:
    __slots__ = ("name", "ip", "port", "last_seen")

    def __init__(self, name, ip, port, last_seen):
        self.name, self.ip, self.port, self.last_seen = name, ip, port, last_seen


class TrxNode:
    """One TrxNet device: discovery, subscriptions, publishing, CON retries."""

    DISC_MAGIC = 0xAA
    DISC_VERSION = 0x01
    DISC_PROBE = 0x01
    DISC_ANNOUNCE = 0x02

    COAP_VER = 1
    COAP_CON = 0
    COAP_NON = 1
    COAP_ACK = 2
    COAP_POST = 0x02
    COAP_EMPTY = 0x00
    COAP_URI_PATH = 11

    ANNOUNCE_S = 30.0            # TRXNET_ANNOUNCE_MS
    PEER_TIMEOUT_S = 95.0        # TRXNET_PEER_TIMEOUT_MS, ~3 missed keepalives
    CON_TIMEOUT_S = 2.0          # TRXNET_CON_TIMEOUT_MS
    CON_MAX_RETRIES = 3
    MAX_PEERS = 24               # as on ESP32; Python has no reason to skimp
    MAX_SEEN = 64                # dedup ring for incoming CON
    MAX_PAYLOAD = 64
    MAX_PRIO = 8
    PRIO_LEN = 4
    TICK_S = 0.25

    def __init__(self, name, port=5683, prio=(), on_peer=None, bind_port=None):
        self.name = name[:31]
        self.port = port                         # the network's port
        # Where we listen. A device binds the network port; only a second node
        # on the same machine (the test peer) needs its own, because two UDP
        # sockets sharing a port split unicast between them at the kernel's
        # discretion. The announce carries my_port, so peers reply to the right
        # place either way.
        self.bind_port = port if bind_port is None else bind_port
        self.my_port = self.bind_port
        self.prio = tuple(prio)
        self.on_peer = on_peer
        self.subs = {}
        self.peers = {}                          # name -> TrxPeer
        self.lock = threading.Lock()
        self.sock = None
        self._msg_id = 0
        self._pending = []                       # unACKed CON messages
        self._seen = deque(maxlen=self.MAX_SEEN)
        self._last_announce = 0.0
        self.stats = {"rx": 0, "tx": 0, "acked": 0, "lost": 0, "dropped": 0}

    # -- priority prefixes (INTEGRATION.md section 5) ----------------------

    @staticmethod
    def parse_prio(text):
        """Trim, upper-case, clamp each token to 4 chars and the list to 8."""
        out = []
        for tok in (text or "").split():
            out.append(tok.upper()[:TrxNode.PRIO_LEN])
            if len(out) == TrxNode.MAX_PRIO:
                break
        return tuple(out)

    def is_priority(self, name):
        return any(name.upper().startswith(p) for p in self.prio)

    # -- wire format ------------------------------------------------------

    def _build_discovery(self, pkt_type):
        enc = self.name.encode("utf-8")[:31]
        return (bytes([self.DISC_MAGIC, self.DISC_VERSION, pkt_type, len(enc)])
                + enc + bytes([self.my_port >> 8, self.my_port & 0xFF]))

    @classmethod
    def _parse_discovery(cls, data):
        if len(data) < 4 or data[0] != cls.DISC_MAGIC or data[1] != cls.DISC_VERSION:
            return None
        name_len = data[3]
        if len(data) < 4 + name_len + 2:
            return None
        name = data[4:4 + name_len].decode("utf-8", errors="replace")
        port = (data[4 + name_len] << 8) | data[4 + name_len + 1]
        return ("probe" if data[2] == cls.DISC_PROBE else "announce", name, port)

    def _build_coap(self, topic, payload, con, msg_id):
        typ = self.COAP_CON if con else self.COAP_NON
        buf = bytearray([(self.COAP_VER << 6) | (typ << 4), self.COAP_POST,
                         msg_id >> 8, msg_id & 0xFF])
        prev = 0
        for part in [p for p in topic.lstrip("/").split("/") if p]:
            seg = part.encode("utf-8")
            delta = self.COAP_URI_PATH - prev
            prev = self.COAP_URI_PATH
            d_nib = delta if delta < 13 else 13
            l_nib = len(seg) if len(seg) < 13 else 13
            buf.append((d_nib << 4) | l_nib)
            if delta >= 13:
                buf.append(delta - 13)
            if len(seg) >= 13:
                buf.append(len(seg) - 13)
            buf += seg
        if payload:
            buf.append(0xFF)
            buf += payload
        return bytes(buf)

    @classmethod
    def _parse_coap(cls, data):
        if len(data) < 4 or ((data[0] >> 6) & 0x03) != cls.COAP_VER:
            return None
        typ = (data[0] >> 4) & 0x03
        tkl = data[0] & 0x0F
        msg_id = (data[2] << 8) | data[3]
        if typ == cls.COAP_ACK:
            return ("ack", msg_id, None, None)
        if data[1] != cls.COAP_POST:
            return None
        pos = 4 + tkl
        if pos > len(data):
            return None
        parts, opt = [], 0
        while pos < len(data) and data[pos] != 0xFF:
            d, l = (data[pos] >> 4) & 0x0F, data[pos] & 0x0F
            pos += 1
            if d == 13:
                if pos >= len(data):
                    return None
                d, pos = data[pos] + 13, pos + 1
            elif d == 14:
                if pos + 1 >= len(data):
                    return None
                d, pos = ((data[pos] << 8) | data[pos + 1]) + 269, pos + 2
            if l == 13:
                if pos >= len(data):
                    return None
                l, pos = data[pos] + 13, pos + 1
            elif l == 14:
                if pos + 1 >= len(data):
                    return None
                l, pos = ((data[pos] << 8) | data[pos + 1]) + 269, pos + 2
            opt += d
            if opt == cls.COAP_URI_PATH and l > 0:
                if pos + l > len(data):
                    return None
                parts.append(data[pos:pos + l].decode("utf-8", errors="replace"))
            pos += l
        payload = data[pos + 1:] if pos < len(data) and data[pos] == 0xFF else b""
        topic = "/" + "/".join(parts) if parts else "/"
        return ("con" if typ == cls.COAP_CON else "non", msg_id, topic, payload)

    def _build_ack(self, msg_id):
        return bytes([(self.COAP_VER << 6) | (self.COAP_ACK << 4),
                      self.COAP_EMPTY, msg_id >> 8, msg_id & 0xFF])

    # -- API --------------------------------------------------------------

    def subscribe(self, path, cb):
        self.subs[path] = cb

    def publish(self, path, payload, con=False):
        """Send to every known peer. TrxNet unicasts; it is not a broadcast."""
        with self.lock:
            targets = [(p.ip, p.port) for p in self.peers.values()]
        for addr in targets:
            self._send_msg(addr, path, payload, con)

    def publish_to(self, name, path, payload, con=False):
        with self.lock:
            peer = self.peers.get(name)
            addr = (peer.ip, peer.port) if peer else None
        if addr:
            self._send_msg(addr, path, payload, con)

    def peer_list(self):
        now = time.time()
        with self.lock:
            return [{"name": p.name, "ip": p.ip, "port": p.port,
                     "ageMs": int((now - p.last_seen) * 1000),
                     "priority": self.is_priority(p.name), "self": False}
                    for p in sorted(self.peers.values(), key=lambda x: x.name)]

    def peer_count(self):
        with self.lock:
            return len(self.peers)

    # -- sending ----------------------------------------------------------

    def _send_msg(self, addr, path, payload, con):
        if len(payload) > self.MAX_PAYLOAD:
            payload = payload[:self.MAX_PAYLOAD]
        with self.lock:
            self._msg_id = (self._msg_id + 1) & 0xFFFF
            msg_id = self._msg_id
        data = self._build_coap(path, payload, con, msg_id)
        self._sendto(data, addr)
        if con:
            with self.lock:
                self._pending.append({"addr": addr, "data": data, "id": msg_id,
                                      "tries": 1, "at": time.time()})

    def _sendto(self, data, addr):
        try:
            with self.lock:
                if self.sock is None:
                    return
                self.sock.sendto(data, addr)
            self.stats["tx"] += 1
        except OSError as e:
            self.stats["dropped"] += 1
            log(f"trxnet: send to {addr[0]} failed: {e}")

    # -- the loop ---------------------------------------------------------

    def serve(self, stop, tick=None):
        """Thread body: receive, keep discovery alive, retransmit, tick."""
        import select

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", self.bind_port))
        except OSError as e:
            log(f"trxnet: cannot bind UDP {self.bind_port}: {e} - TrxNet is off")
            return
        self.my_port = sock.getsockname()[1]     # bind_port 0 = let the OS pick
        with self.lock:
            self.sock = sock
        log(f"trxnet: {self.name} on UDP {self.my_port}"
            + (f", priority {' '.join(self.prio)}" if self.prio else ""))

        self._broadcast(self.DISC_PROBE)
        while not stop.is_set():
            try:
                ready, _, _ = select.select([sock], [], [], self.TICK_S)
                if ready:
                    data, addr = sock.recvfrom(1500)
                    self._on_packet(data, addr)
            except OSError as e:
                if not stop.is_set():
                    log(f"trxnet: socket error: {e}")
                    time.sleep(0.5)
            self._housekeeping()
            if tick:
                try:
                    tick()
                except Exception as e:
                    log(f"trxnet: tick error: {e}")
        with self.lock:
            self.sock = None
        sock.close()

    def _broadcast(self, pkt_type):
        self._sendto(self._build_discovery(pkt_type), ("255.255.255.255", self.port))

    def _housekeeping(self):
        now = time.time()
        if now - self._last_announce >= self.ANNOUNCE_S:
            self._last_announce = now
            self._broadcast(self.DISC_ANNOUNCE)
        with self.lock:
            gone = [n for n, p in self.peers.items()
                    if now - p.last_seen > self.PEER_TIMEOUT_S]
            for n in gone:
                del self.peers[n]
            due = [p for p in self._pending if now - p["at"] >= self.CON_TIMEOUT_S]
            for p in due:
                if p["tries"] >= self.CON_MAX_RETRIES:
                    self._pending.remove(p)
                    self.stats["lost"] += 1
                else:
                    p["tries"] += 1
                    p["at"] = now
        for n in gone:
            log(f"trxnet: peer {n} timed out")
        for p in due:
            if p in self._pending:
                self._sendto(p["data"], p["addr"])

    # -- receiving --------------------------------------------------------

    def _on_packet(self, data, addr):
        self.stats["rx"] += 1
        if not data:
            return
        if data[0] == self.DISC_MAGIC:
            self._on_discovery(data, addr)
        elif ((data[0] >> 6) & 0x03) == self.COAP_VER:
            self._on_coap(data, addr)

    def _on_discovery(self, data, addr):
        parsed = self._parse_discovery(data)
        if not parsed:
            return
        kind, name, port = parsed
        if name == self.name:                    # our own broadcast coming back
            return
        fresh = self._touch_peer(name, addr[0], port)
        if kind == "probe":                      # answer with a unicast announce
            self._sendto(self._build_discovery(self.DISC_ANNOUNCE), (addr[0], port))
        if fresh:
            log(f"trxnet: peer {name} at {addr[0]}:{port}")
            if self.on_peer:
                self.on_peer(name)               # must not send from here

    def _touch_peer(self, name, ip, port):
        """Add or refresh a peer. Returns True when it is a new one."""
        now = time.time()
        with self.lock:
            peer = self.peers.get(name)
            if peer:
                peer.ip, peer.port, peer.last_seen = ip, port, now
                return False
            if len(self.peers) >= self.MAX_PEERS:
                # Table full: a priority newcomer evicts the stalest ordinary
                # peer, anything else is dropped (section 5 of the profile).
                if not self.is_priority(name):
                    self.stats["dropped"] += 1
                    return False
                ordinary = [p for p in self.peers.values()
                            if not self.is_priority(p.name)]
                if not ordinary:
                    self.stats["dropped"] += 1
                    return False
                del self.peers[min(ordinary, key=lambda p: p.last_seen).name]
            self.peers[name] = TrxPeer(name, ip, port, now)
            return True

    def _on_coap(self, data, addr):
        parsed = self._parse_coap(data)
        if not parsed:
            return
        kind, msg_id, topic, payload = parsed
        if kind == "ack":
            with self.lock:
                for p in list(self._pending):
                    if p["id"] == msg_id and p["addr"][0] == addr[0]:
                        self._pending.remove(p)
                        self.stats["acked"] += 1
            return
        if kind == "con":
            self._sendto(self._build_ack(msg_id), addr)
            # CON is at-least-once: it is retransmitted until ACKed, so without
            # this ring a repeated /s-tune would fire the key twice.
            key = (addr[0], msg_id)
            if key in self._seen:
                return
            self._seen.append(key)
        cb = self.subs.get(topic)
        if cb:
            name = self._name_of(addr[0])
            try:
                cb(name, payload)
            except Exception as e:
                log(f"trxnet: handler for {topic} failed: {e}")

    def _name_of(self, ip):
        with self.lock:
            for p in self.peers.values():
                if p.ip == ip:
                    return p.name
        return ip


# --------------------------------------------------------------------------
# The amplifier as a TrxNet device
# --------------------------------------------------------------------------

class _Want:
    """One outstanding command: a value, a deadline and a retry count."""

    __slots__ = ("value", "deadline", "tries", "sent_at")

    def __init__(self, value, deadline):
        self.value = value
        self.deadline = deadline
        self.tries = 0
        self.sent_at = 0.0


class ExpertBridge:
    """
    Maps the amplifier onto TrxNet topics and TrxNet commands onto keystrokes.

    Publishing is driven by the STATUS stream, so the cadence follows the
    amplifier: everything while transmitting, on change plus a heartbeat
    otherwise. Commands run a closed loop - the two mode keys are toggles, not
    setters, so a blind keystroke is as likely to switch the wrong way.
    """

    HEARTBEAT_S = 5.0            # republish an unchanged value this often
    HOLD_S = 10.0                # keep a command this long while the link is down
    # Between keystrokes. Measured on the wire: the amplifier ACKs an OPERATE
    # key in 52 ms and then goes *completely quiet* for about 1.2 s while it
    # throws the relays - no ACK, no STATUS, though the stream otherwise runs at
    # eight packets a second. The old 0.4 s therefore pressed a toggle key twice
    # more inside the window in which it could not possibly have answered, and
    # the parity of the press count decided where it ended up. Worse, it looked
    # like a success: the loop saw OPERATE arrive, called itself done, and the
    # presses already inside the amplifier undid it a second later.
    SETTLE_S = 1.5
    MAX_TRIES = 3                # three presses of a toggle; more is not better
    CAT_REFRESH_S = 1.0          # slow catch-up so the PA's display tracks
    TUNE_CONFIRM_S = 1.0

    CAT_RS232 = {1: 4, 2: 6}     # menu index of RS-232, by protocol revision

    def __init__(self, hub, tap, node, publish_on=True, subscribe_on=False,
                 allow=(), freq_from=()):
        self.hub = hub
        self.tap = tap
        self.node = node
        self.publish_on = publish_on
        self.subscribe_on = subscribe_on
        self.allow = tuple(allow)
        self.freq_from = tuple(freq_from)
        self.lock = threading.Lock()

        self._last = {}                          # topic -> (payload, time)
        self._greet = deque()                    # peers waiting for a snapshot
        self._want = {}                          # what a peer asked for
        self._want_hz = None                     # last commanded frequency, Hz
        self._sent_khz = None
        self._sent_sub = None
        self._sent_at = 0.0
        self._tune_at = 0.0
        self.cat_ok = None                       # None = not known yet
        self._cat_warned = False
        self._hz_warned = set()                  # senders logged once, see _on_hz

        tap.on_status = self._on_status
        node.on_peer = self._on_peer
        if subscribe_on:
            node.subscribe("/hz", self._on_hz)
            node.subscribe("/s-on", lambda f, d: self._on_cmd("on", f, d))
            node.subscribe("/s-operate", lambda f, d: self._on_cmd("operate", f, d))
            node.subscribe("/s-full", lambda f, d: self._on_cmd("full", f, d))
            node.subscribe("/s-tune", lambda f, d: self._on_cmd("tune", f, d))

    # -- outgoing: the amplifier's state ----------------------------------

    @staticmethod
    def _temp_c(s):
        """
        The amplifier's temperature in whole °C, whatever scale it reports in.

        Rev. 2.0 says which with FLAGS bit 7 (T_SCALE, 1 = °C); Rev. 1.0 uses
        that same bit for PA_PROT and does not say, so °C is assumed there --
        the same assumption the web console makes (index.html, flags.celsius).

        The conversion happens HERE rather than in every consumer because this
        is the only place that still knows the scale: bit 7 is masked out of
        /pa-flags before publishing, precisely because it means two different
        things depending on revision.
        """
        celsius = bool(s["flags"] & 0x80) if s["rev"] == 2 else True
        return s["temp"] if celsius else (s["temp"] - 32) * 5.0 / 9.0

    def _values(self, s):
        """The six published payloads, or None where there is no answer."""
        on = bool(self.hub.source.dtr_state())
        _, link, _ = self.tap.snapshot()
        flags = s["flags"] & 0x7F                # bit 7 means two things by rev
        if on:
            flags |= 0x100
        if link:
            flags |= 0x200
        if s["rev"] == 2:
            flags |= 0x400

        swr = s["swr"]
        if swr is None:
            swr_raw = 0                          # 0 = no answer
        elif swr == float("inf"):
            swr_raw = 0xFFFF
        else:
            swr_raw = min(0xFFFF, int(round(swr * 100)))

        band = BANDS_M[s["band"]] if s["band"] < len(BANDS_M) else 0
        return {
            "/pa-flags": struct.pack("<H", flags),
            "/fwd": struct.pack("<H", min(0xFFFF, int(round(s["fwd"] * 10)))),
            "/ref": struct.pack("<H", min(0xFFFF, int(round(s["ref"] * 10)))),
            "/swr": struct.pack("<H", swr_raw),
            "/band": struct.pack("B", band),
            # °C x 100 in an int16 -- the encoding /temp already uses on this
            # network, so one shape means one thing whatever measures it. A
            # SEPARATE topic from /temp all the same: that one is the WX node's
            # outdoor reading, and a monitor or subscriber meeting both under
            # one name would report a heatsink as the weather. Exactly the
            # /flags vs /pa-flags split, for the same reason.
            "/pa-temp": struct.pack("<h", max(-32768, min(32767,
                                              int(round(self._temp_c(s) * 100))))),
        }

    def _on_status(self, s):
        """Called for every decoded STATUS - from the serial thread."""
        self._check_cat(s)
        if not self.publish_on:
            return
        vals = self._values(s)
        now = time.time()
        # While transmitting the point is the instantaneous number, so every
        # packet goes out. Otherwise a value that has not moved is worth one
        # heartbeat. The trailing zeros after PTT drops are a change, so they
        # are published without a special case.
        live = {"/fwd", "/ref", "/swr"} if s["tx"] else set()
        for topic, payload in vals.items():
            prev = self._last.get(topic)
            if (topic in live or prev is None or prev[0] != payload
                    or now - prev[1] >= self.HEARTBEAT_S):
                self._last[topic] = (payload, now)
                self.node.publish(topic, payload)

    def _check_cat(self, s):
        """CAT_232 only has an effect with CAT set to RS-232 in the PA's menu."""
        want = self.CAT_RS232.get(s["rev"])
        self.cat_ok = s["cat"] == want
        if not self.cat_ok and not self._cat_warned:
            self._cat_warned = True
            log(f"trxnet: the PA has CAT index {s['cat']}, not RS-232 "
                f"({want}) - CAT_232 frames may have no effect")
        elif self.cat_ok:
            self._cat_warned = False

    def _on_peer(self, name):
        """A peer joined; greet it with the current state (from the UDP thread)."""
        if self.publish_on:
            self._greet.append(name)

    def _drain_greet(self):
        """One peer per tick, so a CON burst never piles up."""
        if not self._greet:
            return
        name = self._greet.popleft()
        s, link, _ = self.tap.snapshot()
        vals = self._values(s) if (s and link) else self._offline_values(s)
        for topic, payload in vals.items():
            self.node.publish_to(name, topic, payload, con=True)

    def _offline_values(self, s):
        """
        What to publish while the amplifier is not answering.

        Bits 8 and 9 of /pa-flags are the daemon's own knowledge, not the
        amplifier's - whether DTR is up, and whether anything is coming back -
        and they are precisely the two a consumer needs when no telemetry
        flows. Publishing only from the STATUS handler made "switched off"
        indistinguishable from "gone", so a panel whose ON button toggles over
        the last value it heard kept sending the opposite one, forever: with
        the amplifier off there is no STATUS, so nothing could ever correct it.
        The amplifier's own bits go out as zero and the readings as "no
        answer", which is what they are.
        """
        flags = 0x400 if s and s["rev"] == 2 else 0
        if self.hub.source.dtr_state():
            flags |= 0x100                       # bit 8, ON
        # Bit 9, LINK, stays clear - that is the whole point of this set.
        return {
            "/pa-flags": struct.pack("<H", flags),
            "/fwd": struct.pack("<H", 0),
            "/ref": struct.pack("<H", 0),
            "/swr": struct.pack("<H", 0),        # 0 = no answer
            "/band": struct.pack("B", 0),        # 0 = unknown
            # No sentinel for "unknown": every temperature an int16 can hold is
            # a temperature something could really be. What says the reading is
            # meaningless is bit 9, LINK, being clear right beside it in this
            # same snapshot -- which is the whole point of this set.
            "/pa-temp": struct.pack("<h", 0),
        }

    # -- incoming: commands -----------------------------------------------

    @staticmethod
    def _name_matches(sender, patterns):
        """
        Prefix match on the device name, case-insensitively.

        One matcher for both gates below, so "OI3" keeps meaning the same thing
        whichever setting it is written in. A prefix rather than the whole name
        because a network with a single keyer is entitled to say OI3 and be
        done; write the full OI3.02 when there are two of them.
        """
        return any(sender.upper().startswith(p.upper()) for p in patterns)

    def _allowed(self, sender):
        """Who may COMMAND the amplifier - the /s-x topics."""
        if not self.allow:
            return True
        return self._name_matches(sender, self.allow)

    def _freq_source(self, sender):
        """
        Who may RETUNE it - the /hz topic. A different question.

        INTEGRATION.md section 6.2b: a state topic is owned by *a* device, not
        by *the* device. Both 705 and OI3 publish /hz, so anything that ACTS on
        one - tunes to it, steps a rotator to it, switches an antenna on it -
        MUST be able to say which peer it takes it from. Without that, an
        amplifier wired behind one radio follows whichever of the two moved
        last, and the operator gets a kilowatt on the wrong band with nothing
        anywhere saying why.

        Deliberately NOT folded into the allow list, and deliberately not
        requiring the source to be in it: the allow list answers "who may press
        the buttons", this answers "which radio is in front of the amplifier",
        and in the ordinary installation those are two different devices - the
        web backend presses the buttons, the keyer supplies the frequency.

        Empty falls back to the allow list, which is what every install that
        predates this setting already has.
        """
        if not self.freq_from:
            return self._allowed(sender)
        return self._name_matches(sender, self.freq_from)

    def _on_hz(self, sender, data):
        if len(data) < 4:
            return
        if not self._freq_source(sender):
            # Once per sender. The other radio publishes on every turn of its
            # VFO, so logging each one would bury the line that matters - and
            # this line matters: a silently dropped /hz is precisely the
            # failure that sends somebody hunting through two devices. The cap
            # bounds a set whose keys come off the wire.
            if sender not in self._hz_warned and len(self._hz_warned) < 8:
                self._hz_warned.add(sender)
                why = (f"the frequency source is {' '.join(self.freq_from)}"
                       if self.freq_from else "not in the allow list")
                log(f"trxnet: /hz from {sender} ignored ({why})")
            return
        with self.lock:
            self._want_hz = struct.unpack_from("<I", data)[0]
        self.hub.note_activity()

    def _on_cmd(self, what, sender, data):
        if len(data) < 1:
            return
        if not self._allowed(sender):
            log(f"trxnet: /s-{what} from {sender} ignored (not in the allow list)")
            return
        value = bool(data[0])
        if what == "tune" and not value:
            return                               # TUNE is momentary, 0 is a no-op
        with self.lock:
            self._want[what] = _Want(value, time.time() + self.HOLD_S)
        self.hub.note_activity()
        log(f"trxnet: /s-{what} {int(value)} from {sender}")

    # -- the tick ---------------------------------------------------------

    def tick(self):
        """Called from the TrxNode loop, four times a second."""
        self._drain_greet()
        self._tick_offline()
        self._tick_freq()
        self._tick_cmds()

    def _tick_offline(self):
        """Keep /pa-flags going while the amplifier is silent - see above."""
        if not self.publish_on:
            return
        s, link, _ = self.tap.snapshot()
        if link:
            return                               # _on_status has it covered
        now = time.time()
        for topic, payload in self._offline_values(s).items():
            prev = self._last.get(topic)
            if (prev is None or prev[0] != payload
                    or now - prev[1] >= self.HEARTBEAT_S):
                self._last[topic] = (payload, now)
                self.node.publish(topic, payload)

    def _tick_freq(self):
        with self.lock:
            hz = self._want_hz
        if hz is None:
            return
        khz = hz // 1000
        sub = sub_band_for(khz)
        if sub is None:
            return                               # the PA has no band there
        now = time.time()
        if sub != self._sent_sub:
            pass                                 # a new sub-band tunes at once
        elif khz != self._sent_khz and now - self._sent_at >= self.CAT_REFRESH_S:
            pass                                 # slow catch-up for the display
        else:
            return
        # The real frequency goes on the wire; the table only decides when.
        if self.hub.send(frame(CAT_232, khz & 0xFF, (khz >> 8) & 0xFF)):
            self._sent_khz, self._sent_sub, self._sent_at = khz, sub, now

    def _tick_cmds(self):
        with self.lock:
            items = list(self._want.items())
        if not items:
            return
        s, link, at = self.tap.snapshot()
        now = time.time()
        for what, want in items:
            if what == "on":
                self._do_power(want.value)
                self._done(what)
                continue
            if not link:
                # Nothing to check against yet. A PA that was just switched on
                # takes about seven seconds, which is exactly the case this
                # hold is for.
                if now > want.deadline:
                    log(f"trxnet: /s-{what} dropped - no telemetry for "
                        f"{self.HOLD_S:.0f} s")
                    self._done(what)
                continue
            if what == "tune":
                self._do_tune(s, want, now, at)
                continue
            reached = s["operate"] if what == "operate" else bool(s["flags"] & 0x10)
            if reached == want.value:
                self._done(what)
                continue
            # OPERATE and PWR are toggle keys, so the one thing that must never
            # happen is a second press the amplifier has not had the chance to
            # answer. "Had the chance" is not a timer: it is a STATUS that
            # arrived at least a settle time after the keystroke went out, which
            # is the only evidence that the amplifier is talking again and still
            # disagrees. Nothing at all is sent while it is quiet - the link
            # going down is already handled above, and until then a silent
            # amplifier is a busy one.
            if at - want.sent_at < self.SETTLE_S:
                continue
            want.tries += 1
            if want.tries > self.MAX_TRIES:
                log(f"trxnet: /s-{what} gave up - the amplifier did not follow")
                self._done(what)
                continue
            if want.tries > 1:
                log(f"trxnet: /s-{what} retry {want.tries} - still "
                    f"{int(reached)}, want {int(want.value)}")
            want.sent_at = now
            self.hub.send_key(KEY_OPERATE if what == "operate" else KEY_MODE)

    def _do_power(self, on):
        src = self.hub.source
        if self.hub.dtr_mode == "pulse":
            if on:
                threading.Thread(target=src.dtr_pulse, daemon=True).start()
            else:
                self.hub.send(FRAME_OFF, rate_limited=False)
        else:
            src.set_dtr(on)

    def _do_tune(self, s, want, now, at):
        """
        TUNE is momentary: press once, then check the flag actually rose.

        The verdict waits on a STATUS newer than the keystroke for the same
        reason the toggle keys do - the amplifier falls silent for around a
        second while it acts, and a wall clock alone would call that a failure.
        """
        if not want.sent_at:
            want.sent_at = now
            self.hub.send_key(KEY_TUNE)
            return
        if s["flags"] & 0x01:                    # tuning started
            self._done("tune")
        elif at - want.sent_at >= self.TUNE_CONFIRM_S:
            log("trxnet: /s-tune had no effect - transmitting, or in STANDBY?")
            self._done("tune")

    def _done(self, what):
        with self.lock:
            self._want.pop(what, None)

    # -- diagnostics (INTEGRATION.md section 8.2) -------------------------

    def health(self):
        peers = self.node.peer_list()
        return {
            "self": {
                "name": self.node.name,
                "port": self.node.port,
                "enabled": True,
                "publishOn": self.publish_on,
                "subscribeOn": self.subscribe_on,
                # Beside subscribeOn because section 6.2b asks for the source
                # restriction to be reachable wherever subscribe_enable is, and
                # because these two are the settings that drop a packet without
                # anything else changing: every other field can read healthy
                # while one of them quietly refuses the traffic. Product
                # specific, beyond the section 8.2 core.
                "allow": list(self.allow),
                "freqFrom": list(self.freq_from),
                "peerCount": len(peers),
                "peerMax": TrxNode.MAX_PEERS,
                "tableFull": len(peers) >= TrxNode.MAX_PEERS,
            },
            "peers": peers,
            "stats": self.node.stats,
        }


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
    bridge = None                # set when TrxNet is running

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
        h = {
            "source": self.hub.source.describe(),
            "clients": self.hub.client_count(),
            "raw_client": self.hub.has_raw(),
            "auto_shutdown_min": self.hub.auto_shutdown_min,
            "dtr_mode": self.args.dtr_mode,
            "dtr": self.hub.source.dtr_state(),
            "stats": self.hub.stats,
        }
        if self.bridge is not None:
            # Shape per INTEGRATION.md section 8.2, so the monitor, NodeRed and
            # companion UIs all read one thing.
            h["trxnet"] = self.bridge.health()
            h["cat_ok"] = self.bridge.cat_ok
            if self.bridge.cat_ok is False:
                h["cat_hint"] = ("CAT_232 needs CAT set to RS-232 in the "
                                 "amplifier's menu")
        return h

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


def start_trxnet(hub, args, stop):
    """
    Bring the TrxNet node up, or return None when it is not wanted.

    The enable rule is the profile's (INTEGRATION.md section 3): the stack
    starts only with the switch on AND a NET_ID other than the reserved 0x00.
    They are separate settings so that switching TrxNet off does not lose the
    configured ID.
    """
    if not args.trxnet:
        return None
    try:
        net_id = int(args.trxnet_id, 16)
    except ValueError:
        sys.exit(f"--trxnet-id takes two hex digits, not {args.trxnet_id!r}")
    if not 0 <= net_id <= 0xFF:
        sys.exit("--trxnet-id is out of range (00-ff)")
    if net_id == 0:
        sys.exit("--trxnet-id 00 is the reserved 'disabled' value - "
                 "pick another ID, or leave out --trxnet")

    # One name, and it is worth refusing a list rather than quietly accepting
    # one: a list brings back exactly the collision this setting exists to end,
    # and it would do it silently. A typo'd single name is caught by the
    # once-per-sender line in _on_hz.
    freq_from = args.trxnet_freq_from.split()
    if len(freq_from) > 1:
        sys.exit("--trxnet-freq-from takes ONE peer name, not a list "
                 f"({args.trxnet_freq_from!r}) - two radios feeding one "
                 "amplifier's frequency is the collision this setting is for. "
                 "Name the radio that is wired to the PA.")

    name = f"{args.trxnet_type}.{net_id:02x}"
    node = TrxNode(name, args.trxnet_port, TrxNode.parse_prio(args.trxnet_prio))
    tap = StatusTap()
    hub.add_tap(tap.feed)
    bridge = ExpertBridge(hub, tap, node,
                          publish_on=not args.trxnet_no_publish,
                          subscribe_on=args.trxnet_subscribe,
                          allow=args.trxnet_allow.split(),
                          freq_from=freq_from)
    threading.Thread(target=node.serve, args=(stop, bridge.tick),
                     daemon=True).start()
    log(f"trxnet: {name}, publish {'on' if bridge.publish_on else 'off'}, "
        f"subscribe {'on' if bridge.subscribe_on else 'off'}"
        + (f", allow {' '.join(bridge.allow)}" if bridge.allow else "")
        + (f", /hz from {' '.join(bridge.freq_from)}" if bridge.freq_from else ""))
    if bridge.subscribe_on and not bridge.allow:
        log("trxnet: any device on the segment can command the amplifier "
            "- see --trxnet-allow")
    # The collision has no symptom of its own: both radios are configured, both
    # are entitled to publish, and the amplifier simply follows the one that
    # moved last. Say so at startup, where it is cheap to read.
    if bridge.subscribe_on and not bridge.freq_from and len(bridge.allow) > 1:
        log("trxnet: more than one peer may retune the amplifier and the last "
            "/hz wins - name the radio wired to it with --trxnet-freq-from")
    return bridge


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

    t = p.add_argument_group(
        "TrxNet",
        "Join the remoteQTH device network as PA.<id>. The console then works "
        "with no browser open at all.")
    t.add_argument("--trxnet", action="store_true", help="join the network")
    t.add_argument("--trxnet-id", default="01", metavar="HEX",
                   help="NET_ID as two hex digits; 00 is the reserved "
                        "'disabled' value and refuses to start")
    t.add_argument("--trxnet-type", default="PA", metavar="TYPE",
                   help="device type prefix, giving names like PA.01")
    t.add_argument("--trxnet-port", type=int, default=5683,
                   help="UDP port; every device on the network shares it")
    t.add_argument("--trxnet-subscribe", action="store_true",
                   help="act on commands from the network. Off by default: "
                        "TrxNet has no authentication, so anyone on the "
                        "segment could otherwise start a tune")
    t.add_argument("--trxnet-no-publish", action="store_true",
                   help="stay silent - announce presence but publish no state")
    t.add_argument("--trxnet-prio", default="", metavar="LIST",
                   help='space-separated name prefixes to keep when the peer '
                        'table fills, e.g. "705 OI3"')
    t.add_argument("--trxnet-allow", default="", metavar="LIST",
                   help="space-separated peer names allowed to command the "
                        "amplifier; empty means anyone. Give the whole list in "
                        "ONE argument - repeating the option keeps only the "
                        "last. The sender's name is unsigned, so this guards "
                        "against a misconfigured device, not against an "
                        "attacker")
    t.add_argument("--trxnet-freq-from", default="", metavar="NAME",
                   help="the one peer whose /hz retunes the amplifier, e.g. "
                        'OI3.02. Which radio stands in front of the amplifier '
                        "is a physical fact with a single answer, so this takes "
                        "one name, not a list. Empty falls back to "
                        "--trxnet-allow, where the last peer to publish wins")
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

    bridge = start_trxnet(hub, args, stop)

    def watchdog():
        """
        Keeps the telemetry stream alive and minds auto-shutdown.

        The RCU_ON watchdog runs whether or not TrxNet does: without it the
        daemon sees nothing unless a browser happens to be open, which is
        exactly what this whole exercise is about.
        """
        tick = 0
        while not stop.is_set():
            time.sleep(0.5)
            hub.tick_rcu()
            tick += 1
            if tick % 10 == 0:
                hub.tick_auto_shutdown()

    threading.Thread(target=watchdog, daemon=True).start()

    Handler.hub, Handler.args, Handler.bridge = hub, args, bridge
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
