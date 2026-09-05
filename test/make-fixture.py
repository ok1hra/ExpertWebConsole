#!/usr/bin/env python3
"""
Vygeneruje test/fixture.log - synteticky zaznam ve formatu `socat -x -v`.

Slouzi k demu a testu --replay bez hardwaru. Az bude k dispozici skutecny
zaznam z `DEBUG=1 ./expert-console.sh`, pouzij prednostne ten.
"""
import os

def status(flags, ctx, setup=(), band=2, inp=0, sub=60, freq=7100, cat=1,
           ant=0, sg=162, temp=40, pa=0, pr=0, va=0, ia=0, code=0x80):
    """code: 0x80 = protokol Rev 1.0, 0xA0/0xA1 = Rev 2.0."""
    p = [0] * 30
    p[0] = code
    p[1], p[2] = flags, ctx
    for i, v in enumerate(setup):
        p[3 + i] = v
    p[14] = (band << 4) | inp
    p[15] = sub
    p[16], p[17] = freq & 0xFF, freq >> 8
    p[18] = (cat << 4) | ant
    p[19], p[20] = sg & 0xFF, sg >> 8
    p[21] = temp
    for off, val in ((22, pa), (24, pr), (26, va), (28, ia)):
        p[off], p[off + 1] = val & 0xFF, val >> 8
    return [0xAA, 0xAA, 0xAA, 30] + p + [sum(p) & 0xFF]


ACK = [0xAA, 0xAA, 0xAA, 0x01, 0x06, 0x06]

# FLAGS: b6 BEEP, b4 FULL, b2 TX, b1 OPERATE
STBY = 0x50                     # BEEP + FULL, STANDBY
OP   = 0x52                     # + OPERATE
OPTX = 0x56                     # + TX
HALF = 0x46                     # BEEP + OPERATE + TX, HALF

seq = [(">", [0x55, 0x55, 0x55, 0x01, 0x80, 0x80]), ("<", ACK)]

# STANDBY, budic 38.4 W, SWR 1.62
for _ in range(4):
    seq.append(("<", status(STBY, 0x00, sg=162, pa=384, temp=32)))

# OPERATE + TX - presne hodnoty ze screenshotu 1200w.png
for _ in range(8):
    seq.append(("<", status(OPTX, 0x01, sg=162, temp=40,
                            pa=12000, pr=1008, va=432, ia=367)))

# HALF - bargraf PA OUT se musi preskalovat na 600 W
for _ in range(4):
    seq.append(("<", status(HALF, 0x01, sg=158, temp=44,
                            pa=5813, pr=402, va=451, ia=221)))

# Setup strom: SETUP OPTIONS -> CAT -> SET CAT
for item in (0, 1):
    seq.append(("<", status(OP, 0x07, setup=(0, item))))
seq.append(("<", status(OP, 0x09, setup=(0, 1))))       # SET CAT, ICOM
# C_OUT = 0x004D = priklad z manualu str. 21 -> 192.6 pF
seq.append(("<", status(OP, 0x0D, setup=(0, 63, 0x4D, 0x00))))  # MANUAL TUNE
seq.append(("<", status(OP, 0x0E, setup=(0, 200))))     # BACKLIGHT

# Varovani: reverzni vykon nad 300 W
for _ in range(3):
    seq.append(("<", status(OPTX, 0x1B, pa=9000, pr=3200, va=398, ia=402)))

# Historie alarmu: dva zaznamy
seq.append(("<", status(OP, 0x1D, setup=((1 << 4) | 2, 0x11, 0x9B))))

# ---------------------------------------------------------------------
# Protokol Rev 2.0 - firmware >= 07_07_07_M, "CE/FCC Compliant Second
# Series". STATUS_CODE je 0xA0/0xA1 a mapa DISPLAY_CTX je posunuta.
# ---------------------------------------------------------------------
R2 = 0xA0                       # start ve STANDBY;  0xA1 = start v OPERATE
# FLAGS bit 7 uz neni PA_PROT, ale T_SCALE (1 = Celsius)
R2_OPTX = 0x80 | 0x56
R2_OP   = 0x80 | 0x52

for _ in range(6):
    seq.append(("<", status(R2_OPTX, 0x01, code=R2, cat=6, sg=171, temp=42,
                            pa=11200, pr=880, va=428, ia=352)))

# SETUP OPTIONS je nyni 0x06 a ma 9 polozek (pribyly START a TEMP.)
seq.append(("<", status(R2_OP, 0x06, code=R2, setup=(0, 7))))     # TEMP.

# SET ANTENNA je 0x07, index v SETUP_0, dve anteny na pasmo
# byte = (ant#2 << 4) | ant#1 ; zde 20 m -> #2 a #3
ant_rows = [(1 << 4) | 0] * 10
ant_rows[4] = (2 << 4) | 1
seq.append(("<", status(R2_OP, 0x07, code=R2, setup=(4, *ant_rows))))

# SET TEN-TEC (0x0B) v Rev 1.0 neexistovalo
seq.append(("<", status(R2_OP, 0x0B, code=R2, setup=(0, 1))))     # ORION I/II

# Fahrenheit: T_SCALE = 0
seq.append(("<", status(0x56, 0x01, code=0xA1, cat=6, temp=104,
                        pa=9000, pr=400, va=430, ia=300)))

out = []
t = 0.0
for direction, data in seq:
    t += 0.130
    ts = f"2025/09/04 21:25:{t % 60:09.6f}"
    out.append(f"{direction} {ts}  length={len(data)} from=0 to={len(data)-1}")
    for i in range(0, len(data), 16):
        out.append(" " + " ".join(f"{b:02x}" for b in data[i:i + 16]))

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture.log")
open(path, "w").write("\n".join(out) + "\n")
print(f"{path}: {len(seq)} zaznamu")
