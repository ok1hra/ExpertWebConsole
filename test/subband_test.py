#!/usr/bin/env python3
"""
The tuner's sub-band table and the lookup that decides when to send CAT_232.

The table is transcribed from the user's manual section 19 (p. 70) - the
protocol document gives only the index ranges. It is the one piece of data in
the daemon with no other source to check it against, so the shape is asserted
here: the counts per band, the first and last centre, and that the steps are
the ones the manual prints.
"""
import os
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


def eq(got, want, what):
    note(got == want, f"{what}   ({got!r})" if got == want
         else f"{what}   cekano {want!r}, prislo {got!r}")


# band, first index, last index, first centre, last centre, steps in the manual
MANUAL = [
    ("160 m", 0, 23, 1785, 2015, {10}),
    ("80 m", 24, 52, 3470, 4030, {20}),
    ("40 m", 53, 68, 6963, 7338, {25}),
    ("30 m", 69, 71, 10075, 10175, {50}),
    ("20 m", 72, 80, 13975, 14375, {50}),
    ("17 m", 81, 83, 18075, 18165, {50, 40}),        # nepravidelne
    ("15 m", 84, 94, 20975, 21475, {50}),
    ("12 m", 95, 97, 24891, 25038, {72, 75}),        # nepravidelne
    ("10 m", 98, 116, 27950, 29750, {100}),
    ("6 m", 117, 126, 49750, 54250, {500}),
]

print("1. tvar tabulky")
eq(len(E.SUB_CENTER_KHZ), 127, "127 polozek, indexy 0-126")
note(all(E.SUB_CENTER_KHZ[i] < E.SUB_CENTER_KHZ[i + 1]
         for i in range(126)), "kmitocty rostou")
eq(len(E.SUB_BAND_START), len(E.BANDS_M), "zacatek pasma pro kazde pasmo")
eq(E.SUB_BAND_START, tuple(lo for _, lo, _, _, _, _ in MANUAL),
   "zacatky pasem sedi s manualem")

print("\n2. pasma podle manualu, radek po radku")
for name, lo, hi, first, last, steps in MANUAL:
    seg = E.SUB_CENTER_KHZ[lo:hi + 1]
    got = {seg[i + 1] - seg[i] for i in range(len(seg) - 1)}
    ok = (seg[0] == first and seg[-1] == last and got == steps)
    note(ok, f"{name:>5}  [{lo}..{hi}]  {seg[0]}..{seg[-1]}  kroky {sorted(got)}")

print("\n3. hledani nejblizsiho stredu")
eq(E.sub_band_for(1785), 0, "presne prvni stred")
eq(E.sub_band_for(1789), 0, "o 4 kHz vys porad prvni segment")
eq(E.sub_band_for(1791), 1, "o 6 kHz vys uz druhy")
eq(E.sub_band_for(3650), 33, "80 m uprostred")
eq(E.sub_band_for(7100), 58, "40 m")
eq(E.sub_band_for(14250), 77, "20 m")
eq(E.sub_band_for(54250), 126, "posledni segment 6 m")

print("\n4. nepravidelne kroky vyjdou samy, bez zvlastniho pripadu")
eq(E.sub_band_for(18165), 83, "17 m posledni segment, krok 40")
eq(E.sub_band_for(18150), 83, "mezi 18125 a 18165 blize hornimu")
eq(E.sub_band_for(24963), 96, "12 m prostredni, kroky 72 a 75")
eq(E.sub_band_for(25000), 96, "25000 je o 1 kHz bliz k 24963 nez k 25038")
eq(E.sub_band_for(25010), 97, "o 10 kHz vys uz vyhrava 25038")

print("\n5. mimo pasma PA")
eq(E.sub_band_for(5357), None, "60 m - PA tam pasmo nema")
eq(E.sub_band_for(14450), None, "75 kHz nad poslednim stredem 20 m")
eq(E.sub_band_for(0), None, "nula")
eq(E.sub_band_for(1780), 0, "5 kHz pod prvnim stredem 160 m se jeste chyti")
eq(E.sub_band_for(1770), None, "15 kHz pod nim uz ne")
eq(E.sub_band_for(144300), None, "2 m")
eq(E.sub_band_for(430000), None, "70 cm")

print("\n6. kazde pasmo v tabulce ma svuj index pasma")
for i, (name, lo, hi, *_rest) in enumerate(MANUAL):
    mid = E.SUB_CENTER_KHZ[(lo + hi) // 2]
    sub = E.sub_band_for(mid)
    band = max(b for b, start in enumerate(E.SUB_BAND_START) if start <= sub)
    note(band == i and E.BANDS_M[band] == int(name.split()[0]),
         f"{mid} kHz -> sub {sub} -> {E.BANDS_M[band]} m")

print(f"\n{passed} proslo, {failed} selhalo")
sys.exit(1 if failed else 0)
