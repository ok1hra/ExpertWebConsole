#!/usr/bin/env python3
"""
End-to-end over TrxNet: a peer joins, commands the simulated amplifier and
reads its telemetry back.

  ./expert_console.py --simulate --trxnet --trxnet-subscribe \
      --trxnet-port 5799 --http-port 8099 &
  python3 test/trxnet_e2e.py

Port 5799 rather than 5683 on purpose: on a machine that sits on the real
network, a test announcing itself as PA.01 would be picked up by the actual
fleet and the IC-705 would start publishing to it.
"""
import os
import socket
import struct
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import expert_console as E                                      # noqa: E402

PORT = 5799
PA = "PA.01"
passed = failed = 0
seen = {}                                   # topic -> payload
seen_lock = threading.Lock()


def note(cond, what):
    global passed, failed
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    passed += bool(cond)
    failed += (not cond)


def wait(pred, what, t=6.0):
    """Poll until the amplifier's own telemetry says the thing happened."""
    global passed, failed
    end = time.time() + t
    while time.time() < end:
        with seen_lock:
            snap = dict(seen)
        if pred(snap):
            print(f"  ok   {what}")
            passed += 1
            return True
        time.sleep(0.05)
    print(f"  FAIL {what}   (topicy: { {k: v.hex() for k, v in snap.items()} })")
    failed += 1
    return False


def flags(snap):
    return struct.unpack("<H", snap["/pa-flags"])[0] if "/pa-flags" in snap else 0


def u16(snap, topic):
    return struct.unpack("<H", snap[topic])[0] if topic in snap else None


# -- a peer of our own, built from the same class the daemon uses -------------

# bind_port 0: an ephemeral port of our own, so unicast to 5799
# reaches the daemon rather than being split between two sockets
peer = E.TrxNode("TST.02", PORT, bind_port=0)
for topic in ("/pa-flags", "/fwd", "/ref", "/swr", "/band"):
    def handler(_from, data, topic=topic):
        with seen_lock:
            seen[topic] = data
    peer.subscribe(topic, handler)

stop = threading.Event()
threading.Thread(target=peer.serve, args=(stop,), daemon=True).start()
time.sleep(1.5)                              # discovery + first status packets

try:
    print("1. discovery")
    note(any(p["name"] == PA for p in peer.peer_list()),
         f"{PA} se ohlasil a je v tabulce")
    note(peer.peer_count() == 1, "prave jeden peer")

    print("\n2. telemetrie chodi bez otevreneho prohlizece")
    wait(lambda s: len(s) >= 5, "vsech pet topicu doslo")
    with seen_lock:
        snap = dict(seen)
    note(len(snap.get("/pa-flags", b"")) == 2, "/pa-flags jsou dva bajty")
    note(len(snap.get("/band", b"")) == 1, "/band je jeden bajt")
    note(flags(snap) & 0x200, "bit 9 LINK - STATUS pakety tecou")
    note(flags(snap) & 0x100, "bit 8 ON - simulator je zapnuty")
    note(not flags(snap) & 0x80, "bit 7 je vzdy nula")
    note(snap["/band"][0] == 40, "simulator startuje na 40 m")

    print("\n3. /s-operate - klavesa 0x1C je prepinac, musi konvergovat")
    peer.publish_to(PA, "/s-operate", b"\x01")
    wait(lambda s: flags(s) & 0x02, "OPERATE zapnuto (bit 1)")
    peer.publish_to(PA, "/s-operate", b"\x01")       # podruhe totez
    time.sleep(1.5)
    with seen_lock:
        snap = dict(seen)
    note(flags(snap) & 0x02, "tyz povel podruhe nic neprepnul - idempotence")
    peer.publish_to(PA, "/s-operate", b"\x00")
    wait(lambda s: not flags(s) & 0x02, "STANDBY zpet")

    print("\n4. /s-full - PWR-L / PWR-H")
    peer.publish_to(PA, "/s-full", b"\x00")
    wait(lambda s: not flags(s) & 0x10, "HALF (PWR-L)")
    peer.publish_to(PA, "/s-full", b"\x01")
    wait(lambda s: flags(s) & 0x10, "FULL (PWR-H)")

    print("\n5. /hz -> CAT_232 -> zmena pasma")
    # PA jede v Rev 1.0, kde je RS-232 v menu CAT index 4; simulator na CAT_232
    # bez nej nereaguje, stejne jako skutecny zesilovac (protokol str. 7).
    for _ in range(3):                       # CAT: ICOM(1) -> 2 -> 3 -> RS-232(4)
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8099/key", b'{"code":44}',
            {"Content-Type": "application/json"}), timeout=3).read()
        time.sleep(0.3)
    peer.publish_to(PA, "/hz", struct.pack("<I", 14250000))
    wait(lambda s: s.get("/band", b"\x00")[0] == 20, "14.250 MHz -> 20 m")
    peer.publish_to(PA, "/hz", struct.pack("<I", 3650000))
    wait(lambda s: s.get("/band", b"\x00")[0] == 80, "3.650 MHz -> 80 m")
    peer.publish_to(PA, "/hz", struct.pack("<I", 5357000))
    time.sleep(1.5)
    with seen_lock:
        snap = dict(seen)
    note(snap["/band"][0] == 80, "60 m PA nema - zustava na 80 m")

    print("\n6. okamzity vykon pri TX")
    peer.publish_to(PA, "/s-operate", b"\x01")
    wait(lambda s: flags(s) & 0x02, "OPERATE kvuli vykonu")
    wait(lambda s: flags(s) & 0x04, "simulator zacal vysilat (bit 2 TX)", t=12)
    wait(lambda s: (u16(s, "/fwd") or 0) > 0, "/fwd nese vykon")
    wait(lambda s: (u16(s, "/swr") or 0) > 100, "/swr dopocitane z FW a REV")
    wait(lambda s: not flags(s) & 0x04, "TX skoncilo", t=12)
    wait(lambda s: u16(s, "/fwd") == 0, "po TX prisla jeste nulova hodnota")

    print("\n7. /s-tune")
    peer.publish_to(PA, "/s-tune", b"\x01")
    wait(lambda s: flags(s) & 0x01, "TUNE se rozbehl (bit 0)")

    print("\n8. uvitaci snimek novemu peerovi")
    fresh = E.TrxNode("TST.03", PORT, bind_port=0)
    got = {}
    for topic in ("/pa-flags", "/fwd", "/ref", "/swr", "/band"):
        def h(_from, data, topic=topic):
            got[topic] = data
        fresh.subscribe(topic, h)
    stop2 = threading.Event()
    threading.Thread(target=fresh.serve, args=(stop2,), daemon=True).start()
    end = time.time() + 5
    while time.time() < end and len(got) < 5:
        time.sleep(0.05)
    note(len(got) == 5, f"novy peer dostal snimek hned po pripojeni ({len(got)}/5)")
    stop2.set()

    print("\n9. vypnuty zesilovac se pozna od ztraceneho")
    # Bity 8 a 9 zna demon sam ze sebe, ne ze zesilovace. Kdyz se publikovalo
    # jen ze STATUS handleru, vypnuty zesilovac neposilal vubec nic - a paletka,
    # jejiz tlacitko ON je prepinac nad naposledy slysenou hodnotou, pak trvale
    # posilala tu opacnou: bez STATUSu ji nemelo co opravit.
    def power(on):
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8099/power",
            b'{"dtr": true}' if on else b'{"dtr": false}',
            {"Content-Type": "application/json"}), timeout=3).read()

    power(False)
    with seen_lock:
        seen.pop("/pa-flags", None)
    wait(lambda s: "/pa-flags" in s, "/pa-flags chodi dal i s vypnutym PA", t=8)
    with seen_lock:
        snap = dict(seen)
    note(not flags(snap) & 0x100, "bit 8 ON je nula - je opravdu vypnuty")
    note(not flags(snap) & 0x200, "bit 9 LINK je nula - nic se nevraci")
    note(u16(snap, "/fwd") == 0, "/fwd je nula, ne posledni slysena hodnota")
    power(True)
    wait(lambda s: flags(s) & 0x300 == 0x300, "po zapnuti se ON i LINK vratily",
         t=8)

    print("\n10. allowlist - kdo smi ovladat zesilovac")
    # Bez site: staci sama rozhodovaci funkce mostu.
    guarded = E.ExpertBridge(None, E.StatusTap(), E.TrxNode("PA.09", PORT),
                             subscribe_on=False, allow=("705.01", "OI3"))
    note(guarded._allowed("705.01"), "705.01 je na seznamu")
    note(guarded._allowed("OI3.ff"), "OI3 sedi prefixove na OI3.ff")
    note(not guarded._allowed("DIN.01"), "DIN.01 na seznamu neni")
    note(not guarded._allowed("192.168.1.9"), "neznamy peer zname jen podle IP")
    open_bridge = E.ExpertBridge(None, E.StatusTap(), E.TrxNode("PA.09", PORT),
                                 subscribe_on=False)
    note(open_bridge._allowed("DIN.01"), "prazdny seznam znamena kohokoli")
finally:
    stop.set()

print(f"\n{passed} proslo, {failed} selhalo")
sys.exit(1 if failed else 0)
