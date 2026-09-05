/*
 * Test vykreslovaci logiky index.html bez prohlizece.
 *
 * Cely skript stranky se pusti nad minimalnim DOM shimem (getElementById
 * vraci stub pro libovolne id), pak se do nej nakrmi ramce z fixture.log
 * a zkontroluje se, co se opravdu vykreslilo. Overuje se logika, ne CSS.
 */
const fs = require("fs"), path = require("path");
const HERE = __dirname;

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : (fail++, console.log("  FAIL: " + m)); };
const has = (hay, needle, m) => ok(String(hay).includes(needle), `${m} — chybí "${needle}"`);
const eq = (a, b, m) => ok(a === b, `${m}: ${a} !== ${b}`);
const strip = s => String(s).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
/* strom nove vykresluje <select> a .leaf, ne .node */
const chosen = () => {
  const m = String(stub("tree").innerHTML).match(/<option value="\d+" selected>([^<]*)/);
  return m ? m[1] : null;
};
const activeLeaf = () => {
  const m = String(stub("tree").innerHTML).match(/class="leaf on"[\s\S]*?<\/div>/);
  return m ? strip(m[0]) : null;
};

/* --- DOM shim ---------------------------------------------------------- */
const els = new Map();
function stub(id) {
  if (els.has(id)) return els.get(id);
  const e = {
    id, innerHTML: "", textContent: "", hidden: false, scrollTop: 0,
    clientWidth: 0, dataset: {},
    style: { setProperty(k, v) { this[k] = v; } },
    classList: { _s: new Set(),
      add(c){this._s.add(c);}, remove(c){this._s.delete(c);},
      toggle(c,on){on?this._s.add(c):this._s.delete(c);},
      contains(c){return this._s.has(c);} },
    addEventListener(){}, appendChild(){},
  };
  els.set(id, e);
  return e;
}
const timers = [];
global.document = {
  getElementById: stub,
  querySelectorAll: () => [],
  addEventListener(){},
};
global.EventSource = class { constructor(){ EventSource.last = this; this.h = {}; }
  addEventListener(n, f){ this.h[n] = f; } };
const sent = [];                       // zaznam odeslanych prikazu
global.fetch = async (path, opt) => {
  if (opt && opt.body) sent.push([path, JSON.parse(opt.body)]);
  return { json: async () => ({}) };
};
const keysSent = () => sent.filter(([p]) => p === "/key").map(([, b]) => b.code);
global.setInterval = (f, t) => { timers.push(f); return timers.length; };
global.setTimeout = () => 0;
global.addEventListener = () => {};        // resize listener stranky

/* --- nacteni skriptu stranky ------------------------------------------- */
const html = fs.readFileSync(path.join(HERE, "..", "index.html"), "utf8");
const script = html.split('<script>\n"use strict";')[1].split("</script>")[0];
const mod = new Function(script + "\n;return {framer, AMP, decode, nav, goToMenu, goToItem, goToValue, PROTO, swrFrom, ampLive, measureSegments, segCount, BARS, SEG_PITCH};")();

/* --- ramce z fixture.log ------------------------------------------------ */
function fixtureFrames() {
  const bytes = [];
  for (const line of fs.readFileSync(path.join(HERE, "fixture.log"), "utf8").split("\n")) {
    if (line.includes("length=")) continue;
    if (/^\s*(?:[0-9a-f]{2}\s+)*[0-9a-f]{2}\s*$/.test(line))
      for (const b of line.trim().split(/\s+/)) bytes.push(parseInt(b, 16));
  }
  return Uint8Array.from(bytes);
}
/* jeden konkretni STATUS: prozene se framer a vezme se posledni stav */
function feed(pred) {
  const all = fixtureFrames();
  const { Framer, decode } = new Function(
    html.split('<script>\n"use strict";')[1].split("async function post")[0] +
    "\nreturn {Framer, decode};")();
  let hit = null;
  new Framer(p => { const s = decode(p); if (!s.short && pred(s)) hit = p; }).push(all);
  if (!hit) throw new Error("ve fixture neni odpovidajici paket");
  mod.framer.push(Uint8Array.from([0xAA,0xAA,0xAA,hit.length,...hit,
                                   hit.reduce((a,b)=>(a+b)&0xFF,0)]));
}

/* --- 1. OPERATE FULL TX: hodnoty ze screenshotu ------------------------- */
console.log("1. OPERATE FULL TX — hodnoty ze screenshotu");
feed(s => s.rev === 1 && s.flags.operate && s.flags.full && s.flags.tx && s.ctx === 0x01);
{
  ok(stub("val_fw").textContent  === "1200 W", "FW = "  + stub("val_fw").textContent);
  ok(stub("val_rev").textContent === "101 W",  "REV = " + stub("val_rev").textContent);
  ok(stub("val_v").textContent   === "43 V",   "V = "   + stub("val_v").textContent);
  ok(stub("val_i").textContent   === "37 A",   "I = "   + stub("val_i").textContent);
  eq(+stub("on_fw").dataset.lit, 40, "FW: všech 40 diod svítí (1200/1200)");
  const ticks = id => [...String(stub(id).innerHTML)
      .matchAll(/<span[^>]*>([^<]*)<\/span>/g)].map(m => m[1]);
  has(stub("bars").innerHTML, 'class="bar rtl"', "REV bar je zrcadleny (rtl)");
  ok(JSON.stringify(ticks("ticks_rev")) === JSON.stringify(["150","100","50"]),
     "REV stupnice klesa zprava doleva: " + ticks("ticks_rev").join(","));
  ok(JSON.stringify(ticks("ticks_fw")) === JSON.stringify(["300","600","900"]),
     "FW stupnice roste zleva doprava: " + ticks("ticks_fw").join(","));
  // Cisla stoji vystredena presne nad svou hodnotou, bez delicich linek
  has(stub("ticks_fw").innerHTML, 'left:25%', "první značka ve čtvrtině");
  has(stub("ticks_fw").innerHTML, 'left:75%', "poslední ve třech čtvrtinách");
  ok(!/border/.test(String(stub("ticks_fw").innerHTML)), "žádné dělicí linky");
  // Stupnice lezi pod pruhem, ne v nem
  const bars = String(stub("bars").innerHTML);
  const track = bars.slice(bars.indexOf('id="track_fw"'), bars.indexOf('id="val_fw"'));
  ok(!track.includes('id="ticks_fw"'), "stupnice není uvnitř pruhu");
  has(bars, 'class="scale" id="ticks_fw"', "stupnice je samostatný řádek pod pruhem");
  const st = strip(stub("statusRow").innerHTML);
  has(st, "40 m", "pásmo ve stavovém řádku");
  has(stub("statusRow").innerHTML, '40<span class="u">m</span>',
      "jednotka m je oddělená a šedivá");
  has(st, "ICOM", "CAT");   has(st, "FULL", "režim výkonu");
  has(st, "16.2 dB", "GAIN"); has(st, "40 °C", "teplota");
  has(stub("statusRow").innerHTML, '16.2<span class="u"> dB</span>',
      "jednotka dB je oddělená a šedivá");
  ok(!String(stub("statusRow").innerHTML).includes("swrval"),
     "GAIN se nebarví jako SWR, je to jiná veličina");
  has(st, "7.100", "kmitočet");
  ok(!/class="v stale"/.test(String(stub("statusRow").innerHTML)),
     "při vysílání je kmitočet bílý, ne zašedlý");
  ok(stub("btnOperate").textContent === "OPERATE", "tlačítko hlásí OPERATE");
  ok(stub("btnOperate").className === "st-op", "OPERATE inverzně (výplň, tmavý text)");
  eq(stub("btnMode").textContent, "PWR-H", "FULL → PWR-H");
  ok(stub("btnMode").className === "st-hi", "PWR-H pískově žlutě");
  has(stub("leds").innerHTML, '"led on r">TX', "LED TX svítí");
  ok(stub("warnBox").hidden === true, "varování skryté");
}

/* --- 2. HALF: preskalovani bargrafu ------------------------------------ */
console.log("2. HALF — bargraf PA OUT se přeškáluje na 600 W");
feed(s => s.rev === 1 && s.flags.operate && !s.flags.full && s.ctx === 0x01);
{
  ok(stub("val_fw").textContent === "581 W", "FW = " + stub("val_fw").textContent);
  eq(+stub("on_fw").dataset.lit, 39, "581.3/600 → 39 ze 40 diod (ne ~19)");
  has(strip(stub("statusRow").innerHTML), "HALF", "režim HALF");
  eq(stub("btnMode").textContent, "PWR-L", "HALF → PWR-L");
  ok(stub("btnMode").className === "st-off", "PWR-L šedivě");
}

/* --- 3. STANDBY: DRIVE misto PA OUT, SWR misto GAIN --------------------- */
console.log("3. STANDBY — DRIVE místo PA OUT, SWR místo GAIN");
feed(s => s.rev === 1 && !s.flags.operate && s.ctx === 0x00);
{
  ok(stub("val_fw").textContent === "38 W", "budicí výkon = " + stub("val_fw").textContent);
  eq(+stub("on_fw").dataset.lit, 15, "ve STANDBY škála budiče: 38.4/100 → 15 diod");
  const st = strip(stub("statusRow").innerHTML);
  has(st, "SWR", "sloupec SWR");
  has(st, "1.62", "hodnota SWR");
  has(stub("statusRow").innerHTML, 'class="swrval"', "SWR nese barvu pruhu REV");
  ok(!st.includes("dB"), "GAIN se ve STANDBY nezobrazuje");
  ok(stub("btnOperate").textContent === "STANDBY", "tlačítko hlásí STANDBY");
  ok(stub("btnOperate").className === "st-off", "STANDBY šedivě");
}

/* --- 4. setup strom ----------------------------------------------------- */
console.log("4. setup strom zrcadlí polohu zesilovače");
feed(s => s.rev === 1 && s.ctx === 0x09);
{
  const t = stub("tree").innerHTML;
  has(t, "SET CAT", "uzel SET CAT");
  has(t, 'tnode live', "aktivní větev zvýrazněna");
  ok(chosen() === "ICOM", "vybraná položka = ICOM, dostal jsem: " + chosen());
  has(strip(t), "SPE", "ostatní položky menu vypsané");
  has(t, 'data-sel="9"', "menu je rozbalovací pole");
  has(t, 'data-apply="9"', "má tlačítko Apply");
}
console.log("5. MANUAL TUNE — L a C podle vah bitů");
feed(s => s.rev === 1 && s.ctx === 0x0D);
{
  const t = strip(stub("tree").innerHTML);
  has(t, "L_OUT 6.3 µH", "L_OUT = 63/10");
  has(t, "C_OUT 192.6 pF", "C_OUT podle vah z manuálu str. 21");
}
console.log("6. BACKLIGHT");
feed(s => s.rev === 1 && s.ctx === 0x0E);
has(strip(stub("tree").innerHTML), "200 / 255", "hodnota podsvícení");

/* --- 7. varovani -------------------------------------------------------- */
console.log("7. varovný stav 0x1B");
feed(s => s.rev === 1 && s.ctx === 0x1B);
{
  ok(stub("warnBox").hidden === false, "varování zobrazeno");
  has(stub("warnBox").textContent, "Reverse power above 300 W", "text varování");
}

/* --- 8. historie alarmu -------------------------------------------------- */
console.log("8. historie alarmů");
feed(s => s.rev === 1 && s.ctx === 0x1D);
{
  const t = strip(stub("tree").innerHTML);
  has(t, "ALARM HISTORY", "hlavička");
  has(t, "IN1", "vstup u záznamu");
  has(t, "Reverse power", "dekódovaný kód alarmu");
}

/* --- 9. Rev 2.0: posunuta mapa a nove polozky ------------------------- */
console.log("9. Rev 2.0 — SETUP OPTIONS je 0x06 a má 9 položek");
feed(s => s.rev === 2 && s.ctx === 0x06);
{
  const t = strip(stub("tree").innerHTML);
  has(t, "SETUP OPTIONS", "uzel SETUP OPTIONS");
  has(t, "START", "nová položka START");
  has(t, "TEMP.", "nová položka TEMP.");
  ok(chosen() === "TEMP.", "vybráno TEMP., dostal jsem: " + chosen());
}
console.log("10. Rev 2.0 — SET ANTENNA má dvě antény na pásmo, index v SETUP_0");
feed(s => s.rev === 2 && s.ctx === 0x07);
{
  const t = strip(stub("tree").innerHTML);
  has(t, "SET ANTENNA", "uzel SET ANTENNA");
  has(t, "SAVE", "položka SAVE");
  const leaf = activeLeaf();
  ok(leaf && leaf.includes("20m"), "vybráno 20m podle SETUP_0: " + leaf);
  ok(leaf && /\b2\b/.test(leaf) && /\b3\b/.test(leaf), "20m má antény 2 a 3: " + leaf);
  ok(/▸\s*2/.test(leaf || ""), "DEF_ANT označuje výchozí anténu: " + leaf);
  has(stub("tree").innerHTML, 'data-item="7:4"',
      "kliknutím na pásmo se na něj naviguje");
}
console.log("11. Rev 2.0 — SET TEN-TEC, který v Rev 1.0 neexistoval");
feed(s => s.rev === 2 && s.ctx === 0x0B);
{
  const t = strip(stub("tree").innerHTML);
  has(t, "SET TEN-TEC", "uzel SET TEN-TEC");
  has(t, "OMNI VII", "model OMNI VII");
  ok((chosen() || "").includes("ORION"), "vybráno ORION I/II: " + chosen());
}
console.log("12. Rev 2.0 — CAT 6 = RS-232, jednotka teploty z T_SCALE");
feed(s => s.rev === 2 && s.ctx === 0x01 && s.flags.celsius);
{
  const st = strip(stub("statusRow").innerHTML);
  has(st, "RS-232", "CAT 6 se čte podle výčtu Rev 2.0");
  has(st, "42 °C", "T_SCALE = 1 → Celsius");
  ok(!stub("leds").innerHTML.includes(">PROT<"),
     "LED PROT se v Rev 2.0 nezobrazuje (bit 7 je T_SCALE)");
}
feed(s => s.rev === 2 && !s.flags.celsius);
has(strip(stub("statusRow").innerHTML), "104 °F", "T_SCALE = 0 → Fahrenheit");

console.log("12b. TX bez výkonu: krátký problik se ignoruje, trvalý hlásí");
{
  const txPacket = () => {
    const p = new Array(30).fill(0);
    p[0] = 0xA0; p[1] = 0x84; p[2] = 0x00; p[14] = 0x60; p[21] = 25;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    return Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]);
  };
  const realNow = Date.now;

  // Sekvencer klíčuje PTT dřív než přijde výkon — pár set ms je normální.
  mod.framer.push(txPacket());
  ok(stub("warnBox").hidden === true, "krátký TX bez výkonu nic nehlásí");

  // Totéž o tři sekundy později už znamená, že PTT někdo drží.
  Date.now = () => realNow() + 3000;
  mod.framer.push(txPacket());
  ok(stub("warnBox").hidden === false, "trvalý TX bez výkonu se ohlásí");
  has(stub("warnBox").innerHTML, "locks BAND", "vysvětlí, co je zamčené");

  sent.length = 0; mod.nav.steps = [];
  mod.goToMenu(0x06);
  eq(keysSent().length, 0, "navigátor při TX neposílá žádné klávesy");
  ok(/TX is asserted/.test(mod.nav.status), "a řekne proč: " + mod.nav.status);

  // Jakmile výkon dorazí, hlášení zmizí
  const p = new Array(30).fill(0);
  p[0] = 0xA0; p[1] = 0x86; p[2] = 0x01; p[14] = 0x60; p[21] = 25;
  p[22] = 0xD0; p[23] = 0x07;                 // 2000 -> 200.0 W
  const chk = p.reduce((x, y) => (x + y) & 0xFF, 0);
  mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
  ok(stub("warnBox").hidden === true, "s výkonem je TX normální provoz");
  Date.now = realNow;
}

console.log("12b2. LED segmenty a peak detektor");
{
  const realNow = Date.now;
  let t = realNow();
  Date.now = () => t;
  const bar = (pa) => {                       // OPERATE FULL TX, dany vykon
    const p = new Array(30).fill(0);
    p[0] = 0xA0; p[1] = 0x80 | 0x16; p[2] = 0x01; p[14] = 0x20; p[21] = 40;
    const v = Math.round(pa * 10);
    p[22] = v & 0xFF; p[23] = v >> 8;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
  };

  bar(0); t += 5000; bar(0);                  // srovnat peak z předchozích testů
  bar(600);                                   // půl škály
  eq(+stub("on_fw").dataset.lit, 20, "600/1200 → 20 diod");
  eq(stub("on_fw").style.clipPath, "inset(0 50% 0 0)", "výřez odpovídá 20 diodám");

  bar(614);                                   // 20.47 diody → zaokrouhlí na celou
  eq(+stub("on_fw").dataset.lit, 20, "rozsvítí se vždy celá dioda, ne její část");
  bar(630);
  eq(+stub("on_fw").dataset.lit, 21, "o kus výš už svítí další celá");

  // Špička: SSB — hlasitá slabika a pak ticho
  bar(1200);
  eq(+stub("on_fw").dataset.lit, 40, "špička rozsvítí celý pruh");
  t += 300; bar(200);
  eq(+stub("on_fw").dataset.lit, 7, "hodnota spadla");
  eq(+stub("pk_fw").dataset.peak, 39, "špičková dioda drží nahoře");
  // Doba drzeni je 2400 ms; pak klesa po krocich v kadenci paketu
  for (let i = 0; i < 16; i++) { t += 170; bar(200); }
  const pk = +stub("pk_fw").dataset.peak;
  ok(pk > 7 && pk < 39, "po době držení špička klesá, teď " + pk);
  t += 3000; bar(200);
  eq(stub("pk_fw").dataset.peak, "", "nakonec dojede k hodnotě a zhasne");
  Date.now = realNow;
}

console.log("12b2b. číslo drží špičku, pruh sleduje okamžitou hodnotu");
{
  const realNow = Date.now;
  let t = realNow();
  Date.now = () => t;
  const bar = (pa) => {
    const p = new Array(30).fill(0);
    p[0] = 0xA0; p[1] = 0x80 | 0x16; p[2] = 0x01; p[14] = 0x20; p[21] = 40;
    const v = Math.round(pa * 10);
    p[22] = v & 0xFF; p[23] = v >> 8;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
  };

  bar(0); t += 5000; bar(0);                  // srovnat z předchozích testů
  bar(900);
  eq(stub("val_fw").textContent, "900 W", "nová špička se vypíše hned");

  t += 200; bar(120);                         // SSB: mezi slabikami
  eq(stub("val_fw").textContent, "900 W", "číslo drží špičku");
  eq(+stub("on_fw").dataset.lit, 4, "pruh mezitím ukazuje okamžitých 120 W");

  // Skutecna kadence je ~6 paketu/s; jeden velky skok by spicku srazil naraz
  for (let i = 0; i < 16; i++) { t += 170; bar(120); }
  const v = parseFloat(stub("val_fw").textContent);
  ok(v < 900 && v > 120, "po době držení číslo plynule klesá, teď " + v);

  t += 5000; bar(120);
  eq(stub("val_fw").textContent, "120 W", "nakonec dojede k aktuální hodnotě");

  // Zmena rozsahu musi starou spicku zahodit
  bar(1100);
  const p2 = new Array(30).fill(0);
  p2[0] = 0xA0; p2[1] = 0x80 | 0x06; p2[2] = 0x01;   // HALF, rozsah 600 W
  p2[14] = 0x20; p2[21] = 40; p2[22] = 0xB8; p2[23] = 0x0B;   // 300.0 W
  const c2 = p2.reduce((a, b) => (a + b) & 0xFF, 0);
  t += 100;
  mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p2, c2]));
  eq(stub("val_fw").textContent, "300 W",
     "po přepnutí FULL→HALF se stará špička zahodí");
  Date.now = realNow;
}

console.log("12b2c. napětí drží propad, ne maximum");
{
  // Navazat na cas predchoziho testu, ne zacinat znovu - jinak by hodiny
  // skocily zpet, coz je jiny pripad, nez chce tenhle test overit.
  const realNow = Date.now;
  let t = realNow() + 60000;
  Date.now = () => t;
  const volt = (v) => {
    const p = new Array(30).fill(0);
    p[0] = 0xA0; p[1] = 0x80 | 0x16; p[2] = 0x01; p[14] = 0x20; p[21] = 40;
    const x = Math.round(v * 10);
    p[26] = x & 0xFF; p[27] = x >> 8;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
  };

  volt(48); t += 3000; volt(48);              // srovnat z předchozích testů
  eq(stub("val_v").textContent, "48 V", "klidové napětí");

  volt(39);                                   // propad při zátěži
  eq(stub("val_v").textContent, "39 V", "propad se zachytí okamžitě");

  t += 200; volt(48);                         // napětí se vrátilo
  eq(stub("val_v").textContent, "39 V", "číslo drží propad, ne návrat");
  const lit = +stub("on_v").dataset.lit, pk = +stub("pk_v").dataset.peak;
  ok(pk < lit, `značka propadu leží uvnitř rozsvícené části (${pk} < ${lit})`);
  ok(stub("pk_v").classList.contains("dip"), "a kreslí se jako zářez");

  for (let i = 0; i < 18; i++) { t += 170; volt(48); }
  ok(parseFloat(stub("val_v").textContent) > 39, "po době držení se vrací nahoru");

  // Vykon naopak drzi maximum a znacka lezi nad rozsvicenou casti
  ok(!stub("pk_fw").classList.contains("dip"), "u výkonu se špička nekreslí jako zářez");
  Date.now = realNow;
}

console.log("12b3. diody mají pevnou velikost, jejich počet plyne ze šířky");
{
  const setW = w => { for (const x of mod.BARS) stub("track_" + x.id).clientWidth = w;
                      mod.measureSegments(); };

  eq(mod.segCount.fw, 40, "bez měřitelné šířky záložních 40");

  setW(840); eq(mod.segCount.fw, 120, "840 px / 7 px → 120 diod");
  setW(280); eq(mod.segCount.fw, 40,  "280 px → 40 diod");
  setW(40);  eq(mod.segCount.fw, 10,  "velmi úzko → nejméně 10 diod, ne míň");

  /* Rozteč musí být celé číslo pixelů, jinak prohlížeč zaokrouhlí každou
     hranici jinam a mezery mezi diodami nejsou stejně široké. */
  setW(500);
  const pitch = parseFloat(stub("wrap_fw").style["--pitch"]);
  const lit   = parseFloat(stub("wrap_fw").style["--lit"]);
  eq(pitch, 7, "rozteč je pevných 7 px");
  eq(lit, 5, "dioda 5 px");
  eq(pitch - lit, 2, "mezera 2 px");
  ok(Number.isInteger(pitch) && Number.isInteger(lit),
     "obojí celé číslo pixelů — jinak se mezery rozjedou");
  eq(mod.segCount.fw, 71, "500 px → 71 celých diod (497 px)");

  // Zbytek za posledni diodou se rozdeli soumerne na oba okraje
  const pad = parseFloat(stub("wrap_fw").style.left);
  eq(pad, 1.5, "zbytek 3 px rozdělen po 1.5 px");
  eq(parseFloat(stub("wrap_fw").style.right), pad, "souměrně z obou stran");

  // Vsechny ctyri pruhy dostanou tentyz rastr
  for (const x of mod.BARS) eq(mod.segCount[x.id], 71, "stejný rastr: " + x.id);

  setW(0);
}

console.log("12b4. segmenty pruhu nesmí kolidovat se stavovými indikátory");
{
  // .leds patri radku ALARM/PROT/TUNE/SET/TX nahore. Kdyz se tak pojmenovaly
  // i segmenty pruhu, dostaly indikatory position:absolute a odletely.
  const bars = String(stub("bars").innerHTML);
  ok(!/class="leds/.test(bars), "pruhy nepoužívají třídu .leds");
  has(bars, 'class="seg off"', "zhasnuté diody mají vlastní třídu");
  has(bars, 'class="segwrap"', "obal segmentů také");
}

console.log("12b5. kmitočet drží poslední známou hodnotu");
{
  const push = (tx, khz) => {
    const p = new Array(30).fill(0);
    p[0] = 0xA0; p[1] = 0x80 | 0x12 | (tx ? 4 : 0); p[2] = 0x01;
    p[14] = 0x20; p[21] = 40;
    p[16] = khz & 0xFF; p[17] = khz >> 8;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
  };
  push(true, 14195);
  has(strip(stub("statusRow").innerHTML), "14.195", "změřený kmitočet");
  ok(!/class="v stale"/.test(String(stub("statusRow").innerHTML)),
     "při měření bílý");

  push(false, 0);                              // TRX nevysílá, CAT mlčí
  has(strip(stub("statusRow").innerHTML), "14.195",
      "po skončení měření zůstane poslední hodnota");
  has(stub("statusRow").innerHTML, 'class="v stale"',
      "ale zašedlá, aby bylo zřejmé, že už se neměří");

  push(true, 99);                              // nábehové smetí, 99 kHz
  has(strip(stub("statusRow").innerHTML), "14.195",
      "nesmyslně nízká hodnota se nezapamatuje");
  ok(!strip(stub("statusRow").innerHTML).includes("0.099"),
     "0.099 se nikde neobjeví");
  push(true, 1810);                            // 160 m, uz platne
  has(strip(stub("statusRow").innerHTML), "1.810", "platná hodnota se převezme");
  ok(!/MHz/.test(String(stub("statusRow").innerHTML)), "jednotka MHz se nepíše");
}

console.log("12c. SWR se počítá z dopředného a odraženého výkonu");
{
  const f = mod.swrFrom;
  eq(f(100, 0), 1, "bez odrazu je SWR 1");
  ok(Math.abs(f(100, 11.1) - 2) < 0.02, "Pr/Pf = 1/9 → SWR 2, dostal jsem " + f(100, 11.1));
  ok(Math.abs(f(100, 25) - 3) < 0.02, "Pr/Pf = 1/4 → SWR 3, dostal jsem " + f(100, 25));
  eq(f(2, 0), null, "pod pár watty se nic nepočítá (šum)");
  eq(f(100, 100), null, "Pr >= Pf je nesmysl, ne nekonečno");
}

console.log("12c2. SWR se zobrazuje v první liště a jen při vysílání");
{
  const push = (tx, pa, pr) => {
    const p = new Array(30).fill(0);
    p[0] = 0xA0; p[1] = 0x80 | 0x12 | (tx ? 4 : 0); p[2] = 0x01;
    p[14] = 0x20; p[21] = 40;
    const v = Math.round(pa * 10), r = Math.round(pr * 10);
    p[22] = v & 0xFF; p[23] = v >> 8;
    p[24] = r & 0xFF; p[25] = r >> 8;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
  };
  push(true, 400, 44.4);
  eq(stub("swrTop").textContent, "SWR 2.0", "při TX se ukáže vypočtené SWR");
  const realNow2 = Date.now;
  let tt = realNow2();
  Date.now = () => tt;
  push(true, 400, 44.4);
  eq(stub("swrTop").textContent, "SWR 2.0", "hodnota drží");
  tt += 1000; push(false, 0, 0);
  eq(stub("swrTop").textContent, "SWR 2.0",
     "po puštění PTT ještě chvíli zůstane, jako hodnoty vedle pruhu");
  tt += 2000; push(false, 0, 0);
  eq(stub("swrTop").textContent, "", "po uplynutí doby držení zmizí");
  tt += 100; push(true, 2, 0);
  eq(stub("swrTop").textContent, "",
     "klíčování bez výkonu neukáže starou hodnotu jako aktuální");
  Date.now = realNow2;
  ok(!String(stub("bars").innerHTML).includes("swr_"),
     "v bargrafu už SWR není");
}

console.log("12d. IN a ANT ukazují všechny možnosti, vybranou zvýrazněnou");
feed(s => s.rev === 1 && s.flags.operate && s.flags.full && s.flags.tx && s.ctx === 0x01);
{
  const html = String(stub("statusRow").innerHTML);
  has(html, '<span class="pick">1</span>', "IN 1 je vybraný");
  has(html, '<span class="dim">2</span>', "IN 2 je tlumený");
  has(html, '<span class="pick">1</span>', "ANT 1 je vybraná");
  has(html, '<span class="dim">4</span>', "ANT 4 je tlumená");
  ok(!html.includes("---"), "žádné tři pomlčky ve stavovém řádku");
  ok(!/>#\d</.test(html), "u čísel antén už není mřížka");
}

console.log("12e. TEMP se barví podle bodů sepnutí ventilátoru (manuál 18.17)");
{
  const temp = (t, contest, celsius = true) => {
    const p = new Array(30).fill(0);
    p[0] = 0xA0;
    p[1] = (celsius ? 0x80 : 0) | (contest ? 0x20 : 0) | 0x12;
    p[2] = 0x01; p[14] = 0x20; p[21] = t;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
    const m = String(stub("statusRow").innerHTML).match(/class="v (t-\w+)"/);
    return m ? m[1] : null;
  };
  eq(temp(25, false),  "t-cool", "25 °C, ventilátor stojí");
  eq(temp(45, false),  "t-warm", "45 °C, první stupeň (práh 40)");
  eq(temp(66, false),  "t-hot",  "66 °C, druhý stupeň (práh 65)");
  eq(temp(80, false),  "t-vhot", "80 °C, třetí stupeň (práh 75)");
  eq(temp(92, false),  "t-trip", "92 °C, nad hranicí ochrany");
  // V CONTEST spínají vyšší stupně dřív
  eq(temp(62, false),  "t-warm", "62 °C mimo CONTEST je ještě první stupeň");
  eq(temp(62, true),   "t-hot",  "62 °C v CONTEST je už druhý stupeň (práh 60)");
  eq(temp(72, true),   "t-vhot", "72 °C v CONTEST je třetí stupeň (práh 70)");
  // Fahrenheity se před porovnáním převádějí
  eq(temp(104, false, false), "t-warm", "104 °F = 40 °C, první stupeň");
  eq(temp(77,  false, false), "t-cool", "77 °F = 25 °C, ventilátor stojí");
}

console.log("12f. běžící PA se pozná z toho, že chodí pakety");
{
  ok(mod.ampLive() === true, "po čerstvém paketu je PA živý");
  eq(stub("btnPower").textContent, "ON", "tlačítko hlásí ON");
  eq(stub("btnPower").className, "st-on", "a je zelené");
  const realNow = Date.now;
  Date.now = () => realNow() + 9000;          // pakety utichly
  ok(mod.ampLive() === false, "po tichu už PA živý není");
  Date.now = realNow;
}

console.log("13. strom je klikací a nabízí navigaci");
{
  const t = String(stub("tree").innerHTML);
  has(t, 'data-goto=', "uzly jsou tlačítka pro navigaci");
  has(t, 'data-nav="up"', "tlačítko pro opuštění menu");
  const { PROTO } = require("./pure.js");
  // trasa do SET YAESU vede pres SETUP OPTIONS -> CAT -> SET
  const y = PROTO[2].tree.find(n => n.ctx === 0x09);
  ok(y.parent === 0x08 && y.pItem === 3, "SET YAESU visí pod SET CAT, položka 3");
  const b = PROTO[2].tree.find(n => n.ctx === 0x0C);
  ok(b.pItem === undefined, "SET BAUDRATE nemá dokumentovanou trasu — nenaviguje se");
}

/* --- 14. navigator: uzavrena smycka --------------------------------- */
console.log("14. navigátor jde krok po kroku a čeká na potvrzení");
{
  /* Postaví Rev 2.0 STATUS s daným kontextem a vybranou položkou.
     sentAt se nuluje předem — navigátor jinak drží 400ms rozestup. */
  const step = (ctx, item, itemIsSetup0) => {
    mod.nav.sentAt = 0;
    const p = new Array(30).fill(0);
    p[0] = 0xA0; p[1] = 0x80 | 0x12; p[2] = ctx;
    p[3 + (itemIsSetup0 ? 0 : 1)] = item;
    p[14] = 0x20; p[21] = 40;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
  };

  sent.length = 0;
  mod.nav.steps = [];
  step(0x01, 0);                       // běžné zobrazení, mimo setup
  mod.goToMenu(0x09);                  // SET YAESU  (přes SETUP OPTIONS → CAT)
  eq(keysSent().length, 1, "poslána právě jedna klávesa");
  eq(keysSent()[0], 0x2F, "první krok je SET (vstup do setupu)");

  step(0x01, 0);                       // zesilovač zatím nereagoval
  eq(keysSent().length, 2, "bez potvrzení se SET zopakuje");
  eq(keysSent()[1], 0x2F, "a je to zase SET, ne další krok");

  step(0x06, 0);                       // SETUP OPTIONS, položka ANTENNA
  eq(keysSent().length, 3, "po potvrzení jde další klávesa");
  eq(keysSent()[2], 0x2E, "šipka vpravo k položce CAT (0→1)");

  step(0x06, 1);                       // vybráno CAT
  eq(keysSent()[3], 0x2F, "na správné položce se stiskne SET");

  step(0x08, 0);                       // jsme v SET CAT
  eq(keysSent()[4], 0x2E, "posun k YAESU (0→3)");
  step(0x08, 3);
  eq(keysSent()[5], 0x2F, "SET otevře SET YAESU");
  step(0x09, 0);
  ok(!mod.nav.busy(), "po dosažení cíle navigace končí");
  eq(keysSent().length, 6, "celkem 6 kláves, ani jedna navíc");
}

console.log("15. navigátor volí kratší cestu dokola a umí se vzdát");
{
  const step = (ctx, item) => {
    mod.nav.sentAt = 0;
    const p = new Array(30).fill(0);
    p[0] = 0xA0; p[1] = 0x80 | 0x12; p[2] = ctx; p[4] = item;
    p[14] = 0x20; p[21] = 40;
    const chk = p.reduce((a, b) => (a + b) & 0xFF, 0);
    mod.framer.push(Uint8Array.from([0xAA, 0xAA, 0xAA, 30, ...p, chk]));
  };

  sent.length = 0; mod.nav.steps = [];
  step(0x06, 1);                       // SETUP OPTIONS, položka 1, 9 položek
  mod.goToItem(0x06, 8);               // cíl QUIT — dozadu 2 kroky, dopředu 7
  eq(keysSent()[0], 0x2D, "kratší cesta je vlevo (wrap přes nulu)");

  sent.length = 0; mod.nav.steps = [];
  step(0x06, 1);
  mod.goToItem(0x06, 3);               // dopředu 2 kroky
  eq(keysSent()[0], 0x2E, "sem je kratší vpravo");

  sent.length = 0; mod.nav.steps = [];
  step(0x06, 0);
  mod.goToItem(0x06, 5);
  for (let i = 0; i < 20; i++) step(0x06, 0);   // zesilovač neposlouchá
  ok(!mod.nav.busy(), "po neúspěšných pokusech se navigace vzdá");
  ok(/did not follow/.test(mod.nav.status), "a řekne proč: " + mod.nav.status);
  ok(keysSent().length <= 13, "nebuší donekonečna, posláno " + keysSent().length);
}

console.log(`\n${pass} proslo, ${fail} selhalo`);
process.exit(fail ? 1 : 0);
