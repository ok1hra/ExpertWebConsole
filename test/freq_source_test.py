#!/usr/bin/env python3
"""
Who may retune the amplifier, as opposed to who may command it.

INTEGRATION.md section 6.2b: /hz is a state topic owned by *a* device, not by
*the* device - both 705 and OI3 publish it. An amplifier wired behind one of
them and following both ends up on the band of whichever moved last, and
nothing anywhere says so: every other setting reads healthy, the peer is in the
table, the packets arrive and are dropped in silence.

That silence is the reason this file exists. The gate has no visible behaviour
of its own - it is a frame that does not go out - so it is exactly the kind of
thing that is discovered months later with a kilowatt on the wrong band.

Offline and instant: no sockets, no simulator, no serial port.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import expert_console as E                                      # noqa: E402

passed = failed = 0


def note(cond, what):
    global passed, failed
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    passed += bool(cond)
    failed += (not cond)


class FakeHub:
    """The two Hub methods the frequency path touches."""

    def __init__(self):
        self.frames = []

    def send(self, data, rate_limited=True):
        self.frames.append(data)
        return True

    def send_key(self, code):
        self.frames.append(bytes([code]))
        return True

    def note_activity(self):
        pass


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


def bridge(allow=(), freq_from=()):
    hub = FakeHub()
    return hub, E.ExpertBridge(hub, E.StatusTap(), NullNode(), publish_on=False,
                               allow=allow, freq_from=freq_from)


def hz(khz):
    return struct.pack("<I", khz * 1000)


def tuned_khz(hub):
    """The kHz in the last CAT_232 frame the bridge sent, or None."""
    for data in reversed(hub.frames):
        if (len(data) >= 7 and data[:3] == bytes([E.SYN_PC]) * 3
                and data[4] == E.CAT_232):
            return data[5] | (data[6] << 8)
    return None


# 55 55 55 CNT op LO HI CHK - confirm the offsets before every check below
# leans on them. A reader that finds nothing would make this whole file pass by
# proving that no frame ever goes out, which is what half of it asserts.
_h, _b = bridge()
_b._want_hz = 14075000
_b._tick_freq()
note(tuned_khz(_h) == 14075, f"CAT_232 nese kHz na bajtech 5-6 (cteno {tuned_khz(_h)})")


print("\n1. bez nastaveni se chova jako dosud")
h, b = bridge()
b._on_hz("705.01", hz(14075))
b._tick_freq()
note(tuned_khz(h) == 14075, "bez allow i bez freq-from preladi kdokoli")

h, b = bridge(allow=("705.01",))
b._on_hz("OI3.02", hz(14075))
b._tick_freq()
note(tuned_khz(h) is None, "s allow listem preladi jen ten, kdo je v nem")


print("\n2. freq-from vybere jedno radio")
h, b = bridge(allow=("705.01", "OI3.02"), freq_from=("OI3.02",))
b._on_hz("OI3.02", hz(7013))
b._tick_freq()
note(tuned_khz(h) == 7013, "jmenovany zdroj preladi")

b._on_hz("705.01", hz(14075))
b._tick_freq()
note(tuned_khz(h) == 7013, "druhe radio uz ne - zustalo na 7013 kHz")
note(b._want_hz == 7013000, "a nezmenilo ani zapamatovany kmitocet")


print("\n3. obe brany jsou nezavisle")
# Tohle je navrhove rozhodnuti, ne nahoda: allow list odpovida na "kdo smi
# mackat tlacitka", freq-from na "ktere radio stoji pred zesilovacem", a v bezne
# instalaci jsou to dve ruzna zarizeni - web backend mackam, keyer dodava kmitocet.
h, b = bridge(allow=("705.01",), freq_from=("OI3.02",))
b._on_hz("OI3.02", hz(21225))
b._tick_freq()
note(tuned_khz(h) == 21225, "zdroj kmitoctu nemusi byt v allow listu")

b._on_cmd("operate", "OI3.02", b"\x01")
note("operate" not in b._want, "ale povel od nej se zahodi")
b._on_cmd("operate", "705.01", b"\x01")
note("operate" in b._want, "zatimco od 705.01 projde, presto ze neladi")


print("\n4. zahozeni /hz se ohlasi, ale jen jednou na odesilatele")
lines = []
orig, E.log = E.log, lines.append
try:
    h, b = bridge(freq_from=("OI3.02",))
    for k in (14075, 14076, 14077):
        b._on_hz("705.01", hz(k))
    b._on_hz("ANT.01", hz(14078))
finally:
    E.log = orig
note(len(lines) == 2, f"dva radky za dva odesilatele (bylo {len(lines)})")
note(any("705.01" in ln and "OI3.02" in ln for ln in lines),
     "radek rekne, kdo byl zahozen i kdo je zdroj")

lines = []
orig, E.log = E.log, lines.append
try:
    h, b = bridge(allow=("OI3.02",))
    b._on_hz("705.01", hz(14075))
finally:
    E.log = orig
note(lines and "allow" in lines[0], "bez freq-from se odvolava na allow list")


print("\n5. prefix se chova stejne v obou nastavenich")
h, b = bridge(freq_from=("OI3",))
b._on_hz("OI3.02", hz(3573))
b._tick_freq()
note(tuned_khz(h) == 3573, "prefix OI3 chyti OI3.02")
h, b = bridge(freq_from=("oi3.02",))
b._on_hz("OI3.02", hz(3573))
b._tick_freq()
note(tuned_khz(h) == 3573, "a nezalezi na velikosti pismen")


print("\n6. seznam se odmitne pri startu, ne az za provozu")


class Args:
    trxnet = True
    trxnet_id = "01"
    trxnet_type = "PA"
    trxnet_port = 5799
    trxnet_prio = ""
    trxnet_allow = "705.01"
    trxnet_no_publish = False
    trxnet_subscribe = True
    trxnet_freq_from = "OI3.02 705.01"


try:
    E.start_trxnet(FakeHub(), Args(), None)
    note(False, "dva nazvy ve freq-from musi zabit start")
except SystemExit as e:
    note("ONE peer name" in str(e), "dva nazvy ve freq-from zabijou start s vysvetlenim")

print(f"\n{passed} proslo, {failed} selhalo")
sys.exit(1 if failed else 0)
