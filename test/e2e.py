#!/usr/bin/env python3
"""
End-to-end test proti simulatoru: prikaz -> ocekavana zmena v STATUS paketu.

  ./expert_console.py --simulate --http-port 8099 --raw-port 7399 &
  python3 test/e2e.py
"""
import json, socket, sys, threading, time, urllib.request

BASE = "http://127.0.0.1:8099"
RAW_PORT = 7399
KEY = dict(SET=0x2F, RIGHT=0x2E, LEFT=0x2D, OPERATE=0x1C, MODE=0x1A,
           BAND_UP=0x2A, ANT=0x2B, IN=0x28, TUNE=0x34)
state, lock = {}, threading.Lock()
passed = failed = 0


def post(path, obj):
    r = urllib.request.Request(BASE + path, json.dumps(obj).encode(),
                               {"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=3).read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())          # 4xx nese telo s "error"


def frames(buf):
    """Stejny ramovac jako v index.html, vcetne meze CNT."""
    i = 0
    while i + 5 <= len(buf):
        if buf[i:i + 3] != b"\xaa\xaa\xaa":
            i += 1
            continue
        cnt = buf[i + 3]
        if cnt > 64:
            i += 1
            continue
        if i + 4 + cnt + 1 > len(buf):
            break
        body = buf[i + 4:i + 4 + cnt]
        if sum(body) & 0xFF == buf[i + 4 + cnt]:
            yield body
            i += 4 + cnt + 1
        else:
            i += 1


def reader():
    buf = b""
    with urllib.request.urlopen(BASE + "/stream", timeout=60) as s:
        for line in s:
            if not line.startswith(b"data: "):
                continue
            try:
                chunk = bytes.fromhex(line[6:].strip().decode())
            except ValueError:
                continue
            if not chunk.startswith(b"\xaa"):
                continue
            buf += chunk
            for f in frames(buf):
                if len(f) == 30 and f[0] == 0x80:
                    with lock:
                        state.update(flags=f[1], ctx=f[2], item=f[4],
                                     band=f[14] >> 4, inp=f[14] & 0xF,
                                     ant=f[18] & 0xF, freq=f[16] | f[17] << 8)
            buf = buf[-40:]


def check(pred, what, t=3.0):
    global passed, failed
    end, s = time.time() + t, {}
    while time.time() < end:
        with lock:
            s = dict(state)
        if s and pred(s):
            print(f"  ok   {what}")
            passed += 1
            return
        time.sleep(0.05)
    print(f"  FAIL {what}   (stav: {s})")
    failed += 1


def note(cond, what):
    global passed, failed
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    passed += bool(cond)
    failed += (not cond)


def key(name):
    post("/key", {"code": KEY[name]})
    time.sleep(0.35)


threading.Thread(target=reader, daemon=True).start()
time.sleep(0.5)
post("/raw", {"hex": "555555018080"})                    # RCU_ON
time.sleep(0.5)

print("A. streamovani po RCU_ON")
check(lambda s: "ctx" in s, "prichazi STATUS pakety")

print("B. navigace setup stromem")
key("SET");   check(lambda s: s["ctx"] == 0x07, "SET -> SETUP OPTIONS (0x07)")
key("RIGHT"); check(lambda s: s["item"] == 1, "sipka -> CAT (polozka 1)")
key("RIGHT"); check(lambda s: s["item"] == 2, "sipka -> MANUAL TUNE (2)")
key("LEFT");  check(lambda s: s["item"] == 1, "sipka zpet -> CAT (1)")
key("SET");   check(lambda s: s["ctx"] == 0x09, "SET -> SET CAT (0x09)")
key("RIGHT"); key("SET")
check(lambda s: s["ctx"] == 0x0B, "ICOM -> SET ICOM (0x0B)")
key("SET");   check(lambda s: s["ctx"] == 0x07, "SET -> zpet do SETUP OPTIONS")

print("C. provozni rezimy")
key("OPERATE"); check(lambda s: s["flags"] & 0x02, "OPERATE (FLAGS bit 1)")
key("MODE");    check(lambda s: not s["flags"] & 0x10, "HALF (FLAGS bit 4 = 0)")
key("MODE");    check(lambda s: s["flags"] & 0x10, "zpet FULL")
key("OPERATE"); check(lambda s: not s["flags"] & 0x02, "zpet STANDBY")

print("D. pasmo, antena, vstup")
with lock:
    b0, a0, i0 = state["band"], state["ant"], state["inp"]
key("BAND_UP"); check(lambda s: s["band"] == (b0 + 1) % 10, "BAND+ posune pasmo")
check(lambda s: s["freq"] > 1000, "kmitocet odpovida pasmu")
key("ANT");     check(lambda s: s["ant"] == (a0 + 1) % 5, "ANT prepne antenu")
# relativne - simulator si stav mezi behy drzi
key("IN");      check(lambda s: s["inp"] == i0 ^ 1, "IN prepne vstup")

print("E. raw TCP port (originalni aplikace)")
try:
    c = socket.create_connection(("127.0.0.1", RAW_PORT), timeout=3)
    c.sendall(bytes.fromhex("555555018080"))
    c.settimeout(1)
    # RCU uz je zapnuty, takze prvni prijde ACK; STATUS pakety tecou za nim
    data, end = b"", time.time() + 3
    while time.time() < end and not any(len(f) == 30 for f in frames(data)):
        try:
            data += c.recv(4096)
        except socket.timeout:
            break
    note(any(len(f) == 30 for f in frames(data)),
         f"raw klient dostava STATUS pakety ({len(data)} B)")
    c2 = socket.create_connection(("127.0.0.1", RAW_PORT), timeout=3)
    c2.settimeout(2)
    try:
        note(c2.recv(100) == b"", "druhy raw klient odmitnut")
    except socket.timeout:
        note(False, "druhy raw klient odmitnut")
    c2.close()
    c.close()
except OSError as e:
    note(False, f"raw TCP: {e}")

print("F. napajeni pres DTR (urovnove)")
h = json.loads(urllib.request.urlopen(BASE + "/health", timeout=3).read())
note(h.get("dtr_mode") == "level", f"vychozi rezim je level ({h.get('dtr_mode')})")
note(post("/power", {"dtr": False}).get("dtr") is False, "OFF srazi DTR")
time.sleep(0.4)
note(not json.loads(urllib.request.urlopen(BASE + "/health", timeout=3).read())["dtr"],
     "health hlasi vypnuto")
note(post("/power", {"dtr": True}).get("dtr") is True, "ON zvedne DTR")
time.sleep(0.6)
post("/raw", {"hex": "555555018080"})            # po zapnuti se RCU resetuje
check(lambda s: "ctx" in s, "po zapnuti opet tecou pakety")
note("error" in post("/power", {}), "prazdny pozadavek odmitnut")

print("G. rate limit 8 prikazu/s")
res = [post("/key", {"code": KEY["SET"]}) for _ in range(14)]
dropped = sum(1 for r in res if r.get("rate_limited"))
note(dropped > 0, f"zahozeno {dropped} z 14 prikazu nad limitem")

print(f"\n{passed} proslo, {failed} selhalo")
sys.exit(1 if failed else 0)
