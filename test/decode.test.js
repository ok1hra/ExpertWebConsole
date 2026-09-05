const { AMP, PROTO, Framer, decode, decodeCout, buildFrame, toHex } = require('./pure.js');
let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : (fail++, console.log("  FAIL: " + m)); };
const eq = (a, b, m) => ok(a === b, `${m}: ${a} !== ${b}`);

// --- pomocnik: slozi STATUS paket ------------------------------------
function status(over = {}) {
  const p = new Array(30).fill(0);
  p[0] = over.code ?? 0x80;
  const f = over;
  p[1]  = f.flags ?? 0x12;                 // OPERATE + FULL
  p[2]  = f.ctx   ?? 0x01;
  p[14] = ((f.band ?? 2) << 4) | (f.input ?? 0);
  p[15] = f.subBand ?? 60;
  p[16] = (f.freq ?? 7100) & 0xFF; p[17] = ((f.freq ?? 7100) >> 8) & 0xFF;
  p[18] = ((f.cat ?? 1) << 4) | (f.ant ?? 0);
  p[19] = (f.sg ?? 162) & 0xFF;   p[20] = ((f.sg ?? 162) >> 8) & 0xFF;
  p[21] = f.temp ?? 40;
  p[22] = (f.pa ?? 12000) & 0xFF; p[23] = ((f.pa ?? 12000) >> 8) & 0xFF;
  p[24] = (f.pr ?? 1008)  & 0xFF; p[25] = ((f.pr ?? 1008)  >> 8) & 0xFF;
  p[26] = (f.va ?? 432)   & 0xFF; p[27] = ((f.va ?? 432)   >> 8) & 0xFF;
  p[28] = (f.ia ?? 367)   & 0xFF; p[29] = ((f.ia ?? 367)   >> 8) & 0xFF;
  if (f.setup) f.setup.forEach((v, i) => p[3 + i] = v);
  const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
  return [0xAA, 0xAA, 0xAA, 30, ...p, chk];
}
const collect = () => { const got = []; const fr = new Framer(p => got.push(decode(p))); return { got, fr }; };

// --- 1. zakladni dekod, hodnoty ze screenshotu -----------------------
console.log("1. dekod hodnot ze screenshotu (1200.0 W / 100.8 W / 43.2 V / 36.7 A)");
{
  const { got, fr } = collect();
  fr.push(Uint8Array.from(status()));
  eq(got.length, 1, "jeden ramec");
  const s = got[0];
  eq(s.kind, "STATUS", "typ");
  eq(s.paOut, 1200.0, "PA OUT");
  eq(s.pr, 100.8, "PW REV");
  eq(s.va, 43.2, "V PA");
  eq(s.ia, 36.7, "I PA");
  eq(s.temp, 40, "teplota");
  eq(AMP.bands[s.band], "40m", "pasmo");
  eq(AMP.cats[s.cat], "ICOM", "CAT");
  eq(AMP.ants[s.ant], "1", "antena");
  eq(s.freq, 7100, "kmitocet");
  eq(s.gainTxt, "16.2 dB", "PA GAIN");
  eq(s.flags.operate, true, "OPERATE"); eq(s.flags.full, true, "FULL");
  eq(fr.stats.ok, 1, "stat ok"); eq(fr.stats.bad, 0, "stat bad");
}

// --- 2. dvoji vyznam bajtu 23-24 -------------------------------------
console.log("2. SWR vs GAIN podle FLAGS bit 1");
{
  const { got, fr } = collect();
  fr.push(Uint8Array.from(status({ flags: 0x10, sg: 162 })));   // STANDBY
  fr.push(Uint8Array.from(status({ flags: 0x10, sg: 0 })));
  fr.push(Uint8Array.from(status({ flags: 0x10, sg: 9999 })));
  fr.push(Uint8Array.from(status({ flags: 0x12, sg: 99 })));    // OPERATE
  fr.push(Uint8Array.from(status({ flags: 0x12, sg: 201 })));
  eq(got[0].swrTxt, "1.62", "SWR 1.62 ve STANDBY");
  eq(got[0].gain, null, "ve STANDBY neni gain");
  eq(got[1].swrTxt, "—", "SWR 0 = bez signalu");
  eq(got[2].swrTxt, "∞", "SWR 9999 = nekonecno");
  eq(got[3].gainTxt, "< 10.0 dB", "gain 99");
  eq(got[4].gainTxt, "> 20.0 dB", "gain 201");
}

// --- 3. ramovani: payload obsahujici 0xAA ----------------------------
console.log("3. payload s bajty 0xAA nesmi rozbit ramovani");
{
  const { got, fr } = collect();
  // teplota 0xAA=170 a subBand 0x2A, plus 0xAA v poli setup
  fr.push(Uint8Array.from(status({ temp: 0xAA, setup: [0xAA,0xAA,0xAA] })));
  eq(got.length, 1, "ramec prosel");
  eq(got[0].temp, 0xAA, "teplota 170");
  eq(fr.stats.bad, 0, "zadny chybny ramec");
}

// --- 4. resync po poskozenem bajtu -----------------------------------
console.log("4. poskozeny bajt -> zahozeni a resync, ne zamrznuti");
{
  const { got, fr } = collect();
  const a = status(), b = status({ pa: 6000 });
  a[10] ^= 0xFF;                                   // rozbij checksum
  fr.push(Uint8Array.from([...a, ...b]));
  eq(got.length, 1, "prosel jen druhy ramec");
  eq(got[0].paOut, 600.0, "druhy ramec dekodovan");
  ok(fr.stats.bad >= 1, "chybny ramec zapocitan");
  ok(fr.stats.resync >= 1, "resync zapocitan");
}

// --- 5. rozdelene prijmy (SSE chunky) --------------------------------
console.log("5. ramec rozdeleny na libovolne kusy");
{
  const { got, fr } = collect();
  const bytes = status();
  for (let i = 0; i < bytes.length; i++) fr.push(Uint8Array.from([bytes[i]]));
  eq(got.length, 1, "slozeno po bajtech");
  eq(got[0].paOut, 1200.0, "hodnota po slozeni");
}
{
  const { got, fr } = collect();
  const s = [...status(), ...status(), ...status()];
  fr.push(Uint8Array.from(s.slice(0, 7)));
  fr.push(Uint8Array.from(s.slice(7, 50)));
  fr.push(Uint8Array.from(s.slice(50)));
  eq(got.length, 3, "tri ramce ze tri nesouvislych kusu");
}

// --- 6. smeti pred ramcem --------------------------------------------
console.log("6. smeti a nedokoncene znacky pred ramcem");
{
  const { got, fr } = collect();
  fr.push(Uint8Array.from([0x00,0xFF,0xAA,0xAA,0x13,0xAA, ...status()]));
  eq(got.length, 1, "ramec nalezen za smetim");
}

// --- 7. kratke odpovedi ACK/NAK/UNK ----------------------------------
console.log("7. ACK / NAK / UNK");
{
  const { got, fr } = collect();
  fr.push(Uint8Array.from([0xAA,0xAA,0xAA,1,0x06,0x06, 0xAA,0xAA,0xAA,1,0x15,0x15,
                           0xAA,0xAA,0xAA,1,0xFF,0xFF]));
  eq(got.length, 3, "tri kratke ramce");
  eq(got[0].kind, "ACK", "ACK"); eq(got[1].kind, "NAK", "NAK"); eq(got[2].kind, "UNK", "UNK");
  ok(got.every(g => g.short), "oznaceny jako short");
}

// --- 8. odchozi ramce ------------------------------------------------
console.log("8. skladani prikazu PC -> PA");
{
  // manual str. 5: OPERATE = 55 55 55 02 10 1C 2C
  eq(toHex(buildFrame(0x10, 0x1C)), "55555502101c2c", "OPERATE dle manualu");
  eq(toHex(buildFrame(0x10, 0x18)), "5555550210182 8".replace(" ",""), "OFF");
  eq(toHex(buildFrame(0x80)), "555555018080", "RCU_ON dle manualu");
  eq(toHex(buildFrame(0x81)), "555555018181", "RCU_OFF dle manualu");
}

// --- 9. Cout podle vah bitu ------------------------------------------
console.log("9. C_OUT, priklad z manualu str. 21");
{
  // 0001001101b -> 192.6 pF
  eq(Math.round(decodeCout(0b0001001101) * 10) / 10, 192.6, "192.6 pF");
  eq(decodeCout(0), 0, "nula");
}

// --- 10. setup kontext ------------------------------------------------
// --- 10. delsi paket z novejsiho firmware ----------------------------
console.log("10. paket delsi nez 30 B (Second Series) se prijme");
{
  const { got, fr } = collect();
  const base = status();                       // 0xAA*3, CNT, 30 B, CHK
  const p = base.slice(4, 34).concat([0x11, 0x22, 0x33, 0x44]);  // +4 bajty
  const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
  fr.push(Uint8Array.from([0xAA, 0xAA, 0xAA, p.length, ...p, chk]));
  eq(got.length, 1, "ramec prijat");
  eq(got[0].kind, "STATUS", "dekodovan jako STATUS");
  eq(got[0].len, 34, "delka");
  eq(got[0].extra, 4, "pocet bajtu nad ramec manualu");
  eq(got[0].paOut, 1200.0, "dokumentovany prefix se cte stejne");
  eq(got[0].va, 43.2, "napeti z prefixu");
}
console.log("11. kratky neznamy ramec se ohlasi i s bajty");
{
  const { got, fr } = collect();
  const p = [0x80, 0x12, 0x01, 0x99];
  const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
  fr.push(Uint8Array.from([0xAA, 0xAA, 0xAA, p.length, ...p, chk]));
  eq(got.length, 1, "ramec vydan");
  eq(got[0].kind, "?", "oznacen jako neznamy");
  eq(got[0].len, 4, "hlasi delku");
  eq(got[0].hex, "80 12 01 99", "hlasi bajty pro identifikaci");
}

// --- 12. rozpoznani revize protokolu ---------------------------------
console.log("12. revize se pozna podle STATUS_CODE");
{
  const { got, fr } = collect();
  fr.push(Uint8Array.from(status({ code: 0x80 })));               // Rev 1.0
  fr.push(Uint8Array.from(status({ code: 0xA0 })));               // Rev 2.0 / STANDBY
  fr.push(Uint8Array.from(status({ code: 0xA1 })));               // Rev 2.0 / OPERATE
  fr.push(Uint8Array.from(status({ code: 0x7F })));               // neznamy
  eq(got.length, 4, "vsechny ramce vydany");
  eq(got[0].rev, 1, "0x80 -> Rev 1.0");
  eq(got[0].proto.name, "Rev 1.0", "nazev revize");
  eq(got[0].startup, null, "Rev 1.0 nema startup bit");
  eq(got[1].rev, 2, "0xA0 -> Rev 2.0");
  eq(got[1].startup, "STANDBY", "0xA0 = start ve STANDBY");
  eq(got[2].startup, "OPERATE", "0xA1 = start v OPERATE");
  eq(got[3].kind, "?", "neznamy kod odmitnut");
  ok(got[3].hex.startsWith("7f"), "neznamy ramec hlasi bajty");
}

// --- 13. FLAGS bit 7 ma v kazde revizi jiny vyznam --------------------
console.log("13. FLAGS bit 7: PA_PROT (Rev 1) vs T_SCALE (Rev 2)");
{
  const { got, fr } = collect();
  fr.push(Uint8Array.from(status({ code: 0x80, flags: 0x80 | 0x12 })));
  fr.push(Uint8Array.from(status({ code: 0xA0, flags: 0x80 | 0x12 })));
  fr.push(Uint8Array.from(status({ code: 0xA0, flags: 0x12 })));
  ok(got[0].flags.paProt === true,  "Rev 1: bit 7 = ochrana PA");
  ok(got[0].flags.celsius === true, "Rev 1: teplota vzdy ve C");
  ok(got[1].flags.paProt === false, "Rev 2: bit 7 uz neni ochrana");
  ok(got[1].flags.celsius === true, "Rev 2: bit 7 = 1 -> Celsius");
  ok(got[2].flags.celsius === false,"Rev 2: bit 7 = 0 -> Fahrenheit");
}

// --- 14. CAT vycet se mezi revizemi rozsiril --------------------------
console.log("14. CAT: Rev 2.0 pridava TEN-TEC a FLEX-RADIO");
{
  eq(PROTO[1].cats[4], "RS-232",     "Rev 1: 4 = RS-232");
  eq(PROTO[1].cats[5], "NONE",       "Rev 1: 5 = NONE");
  eq(PROTO[2].cats[4], "TEN-TEC",    "Rev 2: 4 = TEN-TEC");
  eq(PROTO[2].cats[5], "FLEX-RADIO", "Rev 2: 5 = FLEX-RADIO");
  eq(PROTO[2].cats[6], "RS-232",     "Rev 2: RS-232 se posunul na 6");
  eq(PROTO[2].cats[7], "NONE",       "Rev 2: NONE se posunul na 7");
  const { got, fr } = collect();
  fr.push(Uint8Array.from(status({ code: 0xA0, cat: 6 })));
  eq(got[0].catName, "RS-232", "dekod pouzije vycet spravne revize");
}

// --- 15. DISPLAY_CTX se v Rev 2.0 posunul ----------------------------
console.log("15. DISPLAY_CTX mapa se posunula");
{
  eq(PROTO[1].ctx[0x07], "SETUP OPTIONS", "Rev 1: 0x07 = SETUP OPTIONS");
  eq(PROTO[2].ctx[0x06], "SETUP OPTIONS", "Rev 2: 0x06 = SETUP OPTIONS");
  eq(PROTO[2].ctx[0x07], "SET ANTENNA",   "Rev 2: 0x07 = SET ANTENNA");
  eq(PROTO[2].ctx[0x0B], "SET TEN-TEC",   "Rev 2: 0x0B = SET TEN-TEC (nove)");
  eq(PROTO[1].ctx[0x0B], "SET ICOM",      "Rev 1: 0x0B byl SET ICOM");
  eq(PROTO[2].menu[0x06].length, 9, "Rev 2: SETUP OPTIONS ma 9 polozek");
  eq(PROTO[2].menu[0x06][6], "START", "Rev 2: pribyl START");
  eq(PROTO[2].menu[0x06][7], "TEMP.", "Rev 2: pribyl TEMP.");
  eq(PROTO[1].menu[0x07].length, 7, "Rev 1: melo 7 polozek");
}

console.log("16. SETUP_1 nese vybranou polozku");
{
  const { got, fr } = collect();
  fr.push(Uint8Array.from(status({ ctx: 0x07, setup: [0, 3] })));
  eq(got[0].ctx, 0x07, "DISPLAY_CTX");
  eq(got[0].setup[1], 3, "vybrana polozka BACKLIGHT");
  eq(got[0].setup.length, 11, "SETUP_0..10");
}

console.log(`\n${pass} proslo, ${fail} selhalo`);
process.exit(fail ? 1 : 0);
