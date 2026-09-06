#!/usr/bin/env python3
"""
The toggle-key command loop, against an amplifier that goes quiet.

Measured on the real 1K-FA (daemon SSE stream, one /s-operate 1 on the wire):
the amplifier ACKs the OPERATE key in 52 ms and then stops answering
altogether for about 1.2 s while it throws the relays - no ACK and no STATUS,
although the stream otherwise runs at eight packets a second. OPERATE and
PWR-L/H are toggle keys, so a loop that retries on a wall clock alone presses
them again inside that window and the parity of the press count decides where
the amplifier ends up. It even reports success: it sees OPERATE arrive, calls
itself done, and the presses already inside the amplifier undo it a second
later.

The simulator cannot show this - it flips on the byte - so the stall is
modelled here directly, which is also the only way to run it at speed.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import expert_console as E                                      # noqa: E402

passed = failed = 0


def note(cond, what):
    global passed, failed
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    passed += bool(cond)
    failed += (not cond)


class FakeAmp:
    """
    An amplifier that answers a toggle key only after it has gone quiet.

    Keeps a StatusTap honest by writing into it the way the framer would, so
    the bridge sees exactly the interface it sees in the daemon.
    """

    STALL = 1.2                              # measured; no STATUS at all
    RATE = 0.125                             # 8 STATUS packets a second

    def __init__(self, tap, operate=False, full=True):
        self.tap = tap
        self.operate = operate
        self.full = full
        self.keys = []                       # every key the bridge pressed
        self.busy_until = 0.0
        self._next = 0.0

    # -- the Hub side the bridge uses -------------------------------------

    def send_key(self, code):
        self.keys.append((time.time(), code))
        if time.time() < self.busy_until:
            return True                      # swallowed while switching
        if code == E.KEY_OPERATE:
            self.operate = not self.operate
        elif code == E.KEY_MODE:
            self.full = not self.full
        self.busy_until = time.time() + self.STALL
        return True

    def note_activity(self):
        pass

    # -- the amplifier's own stream ---------------------------------------

    def pump(self, now):
        """One tick of the STATUS stream, silent while switching."""
        if now < self.busy_until or now < self._next:
            return
        self._next = now + self.RATE
        f = 0x80
        if self.operate:
            f |= 0x02
        if self.full:
            f |= 0x10
        with self.tap.lock:
            self.tap.last = {"rev": 2, "flags": f, "tx": False,
                             "operate": bool(f & 0x02), "band": 2, "cat": 6,
                             "temp": 30, "fwd": 0.0, "ref": 0.0, "swr": None}
            self.tap.last_at = now


class NullNode:
    name = "PA.01"
    port = 5683
    stats = {}

    def subscribe(self, *a):
        pass

    def publish(self, *a, **k):
        pass

    def publish_to(self, *a, **k):
        pass

    def peer_list(self):
        return []


def run(what, value, start, seconds=8.0):
    """Drive the bridge over a virtual amplifier and return the keys pressed."""
    tap = E.StatusTap()
    amp = FakeAmp(tap, **start)
    bridge = E.ExpertBridge(amp, tap, NullNode(), publish_on=False)
    amp.pump(time.time())                    # telemetry is already flowing
    bridge._on_cmd(what, "705.01", bytes([value]))
    end = time.time() + seconds
    while time.time() < end:
        amp.pump(time.time())
        bridge._tick_cmds()
        if not bridge._want:
            break
        time.sleep(0.02)
    return amp, bridge


print("1. jeden povel = jeden stisk, kdyz zesilovac mlci 1,2 s")
amp, bridge = run("operate", 1, {"operate": False})
note(len(amp.keys) == 1, f"prave jeden stisk OPERATE (bylo {len(amp.keys)})")
note(amp.operate, "zesilovac skoncil v OPERATE")
note(not bridge._want, "smycka se uzavrela")

print("\n2. tyz povel podruhe nic neprepne")
amp, bridge = run("operate", 1, {"operate": True})
note(len(amp.keys) == 0, f"zadny stisk (bylo {len(amp.keys)})")
note(amp.operate, "OPERATE zustava")

print("\n3. PWR-L/H je tyz prepinac a chova se stejne")
amp, bridge = run("full", 0, {"full": True})
note(len(amp.keys) == 1, f"prave jeden stisk MODE (bylo {len(amp.keys)})")
note(not amp.full, "zesilovac je v HALF")

print("\n4. stisky nesmi prijit driv, nez zesilovac zase promluvi")
# Zesilovac, ktery klavesu ignoruje: smycka to musi vzdat po MAX_TRIES
# a mezi stisky nechat vic nez merenych 1,2 s ticha.
class Deaf(FakeAmp):
    def send_key(self, code):
        self.keys.append((time.time(), code))
        self.busy_until = time.time() + self.STALL
        return True


tap = E.StatusTap()
amp = Deaf(tap, operate=False)
bridge = E.ExpertBridge(amp, tap, NullNode(), publish_on=False)
amp.pump(time.time())
bridge._on_cmd("operate", "705.01", b"\x01")
end = time.time() + 12
while time.time() < end and bridge._want:
    amp.pump(time.time())
    bridge._tick_cmds()
    time.sleep(0.02)
gaps = [b[0] - a[0] for a, b in zip(amp.keys, amp.keys[1:])]
note(len(amp.keys) == E.ExpertBridge.MAX_TRIES,
     f"vzdalo to po {E.ExpertBridge.MAX_TRIES} stiscich (bylo {len(amp.keys)})")
note(all(g > FakeAmp.STALL for g in gaps),
     f"kazda mezera prekracuje 1,2 s ticha: {[round(g, 2) for g in gaps]}")
note(not bridge._want, "povel se zahodil, nezustal viset")

print("\n5. mlcici zesilovac nedostane nic - drzi se a pak zahodi")
tap = E.StatusTap()
amp = FakeAmp(tap, operate=False)
bridge = E.ExpertBridge(amp, tap, NullNode(), publish_on=False)
bridge.HOLD_S = 1.0
bridge._on_cmd("operate", "705.01", b"\x01")     # zadny pump: link nikdy nenabehl
end = time.time() + 3
while time.time() < end and bridge._want:
    bridge._tick_cmds()
    time.sleep(0.02)
note(len(amp.keys) == 0, "bez telemetrie se na klavesu nesahlo")
note(not bridge._want, "po HOLD_S se povel zahodil")

print(f"\n{passed} proslo, {failed} selhalo")
sys.exit(1 if failed else 0)
