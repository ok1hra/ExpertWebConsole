#!/usr/bin/env python3
"""
Diagnostika serioveho spojeni na EXPERT 1K-FA.

Oddeli: drzi port nekdo jiny? spina DTR? odpovida zesilovac?

Zesilovac po zapnuti nekolik sekund bootuje a RCU se mu resetuje do OFF,
takze se RCU_ON posila opakovane - stejne jako watchdog v prohlizeci.

DAEMON MUSI BYT ZASTAVENY - port se otevira exkluzivne.

  sudo ./test/diag-serial.py /dev/ttyUSB.pa
  sudo ./test/diag-serial.py /dev/ttyUSB.pa --pulse 2000 --wait 45
"""
import argparse
import subprocess
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("Chybi pyserial: apt install python3-serial")

RCU_ON = bytes([0x55, 0x55, 0x55, 0x01, 0x80, 0x80])

ap = argparse.ArgumentParser()
ap.add_argument("port", nargs="?", default="/dev/ttyUSB.pa")
ap.add_argument("--pulse", type=int, default=1000, help="delka DTR pulzu [ms]")
ap.add_argument("--wait", type=int, default=40, help="jak dlouho cekat na boot [s]")
args = ap.parse_args()


def hdr(t):
    print(f"\n=== {t} " + "=" * max(0, 58 - len(t)))


def poll(ser, secs, label):
    """
    Posila RCU_ON kazde 1.5 s a ceka na odpoved - stejne jako watchdog.
    Vraci (bajty, za_jak_dlouho) nebo (b"", None).
    """
    print(f"  {label}: cekam az {secs} s, RCU_ON kazde 1.5 s")
    ser.reset_input_buffer()
    t0, buf, next_cmd, dot = time.time(), b"", 0.0, 0
    while time.time() - t0 < secs:
        now = time.time() - t0
        if now >= next_cmd:
            ser.write(RCU_ON)
            next_cmd += 1.5
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
            if b"\xaa\xaa\xaa" in buf:
                dt = time.time() - t0
                print(f"\n    ODPOVED po {dt:.1f} s, {len(buf)} B")
                print("    " + buf[:80].hex(" "))
                return buf, dt
        if int(now) > dot:
            dot = int(now)
            print(f"\r    {dot:2d} s …", end="", flush=True)
        time.sleep(0.02)
    print(f"\r    nic za {secs} s" + " " * 20)
    return buf, None


hdr(f"1. kdo drzi {args.port}")
real = subprocess.run(["readlink", "-f", args.port],
                      capture_output=True, text=True).stdout.strip()
print(f"  symlink miri na: {real or args.port}")
busy = False
for cmd in (["fuser", "-v", args.port], ["fuser", "-v", real]):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        out = (r.stdout + r.stderr).strip()
        if out and "\n" in out:
            print("  " + out.replace("\n", "\n  "))
            print("  ^^ POZOR: port drzi jiny proces")
            busy = True
            break
    except (OSError, subprocess.TimeoutExpired):
        pass
if not busy:
    print("  port nikdo nedrzi")

hdr("2. otevreni portu")
try:
    ser = serial.Serial(args.port, 9600, timeout=0, exclusive=True)
except serial.SerialException as e:
    sys.exit(f"  NELZE OTEVRIT: {e}\n  Bezi jeste daemon nebo ser2net?")
ser.dtr = False
ser.rts = False
time.sleep(0.3)
print(f"  {ser.name} @ 9600 8N1")
print(f"  linky (DTR srazen): DTR={int(ser.dtr)} RTS={int(ser.rts)} "
      f"CTS={int(ser.cts)} DSR={int(ser.dsr)} CD={int(ser.cd)}")

hdr("3. bezi zesilovac uz ted?")
before, t_before = poll(ser, 6, "bez sahani na DTR")

if before:
    hdr("4. zaver")
    print("  Zesilovac je zapnuty a odpovida. Prijem i vysilani funguji.")
    print("  Spust daemon a melo by to chodit:")
    print(f"    ./expert_console.py --port {args.port} --raw-port 7373 "
          f"--listen 0.0.0.0")
    ser.close()
    raise SystemExit(0)

hdr(f"4. DTR pulz {args.pulse} ms")
print("  Manual uvadi 200 ms, ale nektere prevodniky potrebuji vic.")
print("  Delka nevadi, dokud se DTR vrati dolu.")
ser.dtr = True
time.sleep(args.pulse / 1000.0)
ser.dtr = False
print(f"  pulz hotov, DTR zpet dolu (DTR={int(ser.dtr)}). Sleduj celni panel.")
after, t_after = poll(ser, args.wait, "po pulzu")

if after:
    hdr("5. zaver")
    print(f"  Pulz {args.pulse} ms zesilovac zapnul, naboot trval {t_after:.1f} s.")
    print("  DTR je pritom zpet dole, takze OFF bude fungovat. To je spravny stav.")
    print("  Spust daemon takto:")
    print(f"    ./expert_console.py --port {args.port} --raw-port 7373 \\")
    print(f"        --listen 0.0.0.0 --dtr-pulse-ms {args.pulse}")
    print(f"  Po stisku ON pockej ~{t_after + 3:.0f} s, nez zacnou chodit data.")
    ser.close()
    raise SystemExit(0)

hdr("5. DTR drzeny nahore (jako ser2net)")
print("  Pulz nestacil. Zkousim drzet DTR trvale.")
ser.dtr = True
held, t_held = poll(ser, args.wait, "s drzenym DTR")
ser.dtr = False
print(f"  DTR zpet dolu (DTR={int(ser.dtr)})")

hdr("6. zaver")
if held:
    print(f"  Zesilovac odpovida az s trvale drzenym DTR (po {t_held:.1f} s).")
    print(f"  Zkus nejdriv delsi pulz, treba:")
    print(f"    sudo {sys.argv[0]} {args.port} --pulse {args.pulse * 3}")
    print("  Kdyz ani to nepomuze, pouzij --dtr-hold, ale pocitej s tim,")
    print("  ze pak zesilovac nepujde vypnout ani z celniho panelu.")
else:
    print("  Zesilovac neodpovedel v zadnem rezimu. Zkontroluj v tomto poradi:")
    print("    a) sviti celni panel? naskocil zesilovac vubec?")
    print("       (kdyz naskocil, ale neodpovida, je problem v RX vetvi)")
    print("    b) je kabel v RS-232 konektoru na zadni strane PA?")
    print("    c) protiotazka bez naseho kodu:")
    print(f"       socat -x -v {args.port},raw,echo=0,b9600 -")
    print("       a do nej vlozit  \\x55\\x55\\x55\\x01\\x80\\x80")
    print("    d) prijem: prepoj RX-TX nakratko (pin 2-3) a over, ze se")
    print("       odeslane bajty vrati - tim se overi cely prevodnik")
ser.close()
