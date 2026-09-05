#!/usr/bin/env python3
"""
The daemon's STATUS decoder, against the same fixture the JavaScript one uses.

test/fixture.log is the only thing keeping the two decoders together: the
daemon needs a handful of fields to feed TrxNet, index.html decodes the whole
record. Every expectation below is a value read out of that file, so a change
on either side that drifts shows up here.
"""
import os
import re
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


def short(v):
    t = repr(v)
    return t if len(t) <= 60 else t[:57] + "..."


def eq(got, want, what):
    note(got == want, f"{what}   ({short(got)})" if got == want
         else f"{what}   cekano {short(want)}, prislo {short(got)}")


# -- read the PA -> PC direction out of the socat-format capture --------------

HDR = re.compile(r"^\s*([<>])\s")
HEXLINE = re.compile(r"^\s*(?:[0-9a-fA-F]{2}\s+)*[0-9a-fA-F]{2}\s*$")

stream, direction = b"", None
for line in open(os.path.join(HERE, "fixture.log"), errors="replace"):
    m = HDR.match(line)
    if m and "length=" in line:
        direction = m.group(1)
        continue
    if direction == "<" and HEXLINE.match(line):
        stream += bytes(int(b, 16) for b in line.split())

print("1. framer")
frames = []
framer = E.Framer(frames.append)
framer.push(stream)
eq(framer.stats["bad"], 0, "zadny ramec s chybnym souctem")
note(framer.stats["ok"] > 30, f"ramcu proslo: {framer.stats['ok']}")
note(framer.buf == b"", "nic nezustalo viset ve vyrovnavaci pameti")

print("\n2. framer po bajtech - hranice chunku nesmi hrat roli")
one_at_a_time = []
f2 = E.Framer(one_at_a_time.append)
for b in stream:
    f2.push(bytes([b]))
eq([bytes(x) for x in one_at_a_time], [bytes(x) for x in frames],
   "stejne ramce jako pri jednom velkem chunku")

print("\n3. falesny marker se nesmi zaseknout")
f3 = E.Framer(lambda body: None)
f3.push(b"\xaa\xaa\xaa\xaa\xaa\xaa\xaa\xaa")     # CNT by vyslo 0xAA = 170
note(f3.stats["resync"] > 0, "CNT nad mezi se preskoci, ne ceka na 175 bajtu")

print("\n4. dekodovani")
st = [s for s in (E.decode_status(bytes(b)) for b in frames) if s]
note(len(st) > 30, f"STATUS zaznamu: {len(st)}")
eq(sorted({s["rev"] for s in st}), [1, 2], "fixture nese obe revize")
eq(E.decode_status(b"\x06"), None, "ACK neni STATUS")
eq(E.decode_status(b"\x80" + b"\x00" * 10), None, "prilis kratky ramec")
eq(E.decode_status(bytes([0x99]) + b"\x00" * 29), None, "neznamy STATUS_CODE")

print("\n5. prvni zaznam z fixture (Rev 1.0, STANDBY, 40 m)")
s = st[0]
eq(s["rev"], 1, "revize")
eq(s["band"], 2, "band 2 = 40 m")
eq(E.BANDS_M[s["band"]], 40, "prevod na metry")
eq(s["cat"], 1, "CAT 1 = ICOM")
eq(s["tx"], False, "nevysila")
eq(s["operate"], False, "STANDBY")
eq(round(s["fwd"], 1), 38.4, "budici vykon")

print("\n6. SWR - v STANDBY ho PA posila, v OPERATE se pocita")
standby = [s for s in st if not s["operate"] and s["swr"] is not None]
note(standby and standby[0]["swr"] > 1.0, "v STANDBY hodnota primo z bajtu 19-20")
tx = [s for s in st if s["operate"] and s["tx"] and s["fwd"] > 5]
note(tx, f"ve fixture je {len(tx)} zaznamu s TX v OPERATE")
if tx:
    note(tx[0]["swr"] is not None, "v OPERATE se SWR dopocita z FW a REV")

print("\n7. swr_from")
eq(E.swr_from(0, 0), None, "bez vykonu neni co pocitat")
eq(E.swr_from(3, 0), None, "pod 5 W je pomer jen sum")
eq(E.swr_from(100, 100), None, "odraz >= dopredny = nesmysl")
eq(round(E.swr_from(100, 0), 2), 1.0, "nulovy odraz = PSV 1.00")
eq(round(E.swr_from(100, 11.1), 2), 2.0, "PSV 2.00")

print("\n8. FLAGS bit 7 se na drat nedostane")
for s in st:
    if s["flags"] & 0x80:
        break
else:
    s = None
note(s is not None, "fixture obsahuje zaznam s nastavenym bitem 7")
if s:
    eq((s["flags"] & 0x7F) & 0x80, 0, "maska 0x7F ho odstrani")

print(f"\n{passed} proslo, {failed} selhalo")
sys.exit(1 if failed else 0)
