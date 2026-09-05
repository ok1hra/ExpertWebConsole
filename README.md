# EXPERT 1K-FA — webová konzole

Webový ekvivalent `EXPERT_Console.exe` pro koncový stupeň SPE EXPERT 1K-FA,
použitelný ze vzdálené QTH. Daemon vlastní sériový port a servíruje si vlastní
stránku; veškerá znalost protokolu je v `index.html`.

    prohlížeč ──http──► expert_console.py ──► /dev/ttyUSB0 ──► 1K-FA
                        (HTML + SSE + sériák)      9600 8N1

Jediná závislost je `pyserial`, a ta jen pro živý provoz:

    apt install python3-serial

## Rychlý start

    ./expert_console.py --simulate                  # bez hardwaru
    ./expert_console.py --replay test/fixture.log   # přehrání záznamu
    ./expert_console.py --port /dev/ttyUSB.pa --raw-port 7373 --listen 0.0.0.0

Konzole pak běží na <http://127.0.0.1:8080/>. Bez `--listen 0.0.0.0` poslouchá
jen na loopbacku — a platí to i pro `--raw-port`.

Když nic nechodí, diagnostika oddělí „drží port někdo jiný", „spíná DTR"
a „odpovídá zesilovač":

    sudo ./test/diag-serial.py /dev/ttyUSB.pa

## Proč se ruší ser2net

Kvůli **DTR**. ser2net ho vytáhne při připojení klienta a nechá ho tam — napájení
zesilovače tak ovládá jako vedlejší účinek, který nejde řídit.

**DTR je úrovňový vypínač, ne zapalovací hrana.** Nejdřív naměřeno na živém stroji:

    pulz 1000 ms, pak DTR dolů  →  45 s nic
    DTR držený nahoře           →  odpověď za 6,7 s

Protokol **Rev. 2.0** to pak potvrdil doslova (str. 4): zapnutí je *„simply raising
it at a voltage level greater than +5 Vdc"* a trvá **3 až 4,5 s**; vypnutí je
*„this control line has to be reset in its OFF state"* a trvá ~1 s, protože se stav
vzorkuje aspoň 500 ms. Rev. 1.0 mluvila o zapalovacím pulzu — na této firmware to
neplatí.

Daemon proto ovládá DTR přímo — `ON` ho zvedne, `OFF` srazí:

    DTR nahoře = zesilovač běží          DTR dole = vypnutý

Po zapnutí PA několik sekund bootuje (naměřeno 6,7 s) a `RCU` se mu přitom resetuje
do `OFF`, takže data začnou chodit až když watchdog v prohlížeči pošle `RCU_ON`
znovu. Zkouší to každých 1,5 s, takže stačí počkat.

Port se otevírá **exkluzivně** (`TIOCEXCL`). Bez toho pyserial otevře i port, který
už drží ser2net, oba pak čtou z téhož zařízení a příchozí bajty se mezi ně náhodně
rozdělí — projeví se to jako „zápisy odcházejí, zpět nic".

Kdyby se jiný kus železa choval podle Rev. 1.0, je tu `--dtr-mode pulse`.

## Dvě revize protokolu

V adresáři jsou obě specifikace a aplikace umí obě. Rozliší je podle `STATUS_CODE`,
takže není co nastavovat — aktuální revize je vidět v Diagnostice.

| | Rev. 1.0 | Rev. 2.0 |
|---|---|---|
| firmware | `06_11_06_x` | `>= 07_07_07_M` (*Second Series*) |
| `STATUS_CODE` | `0x80` | `0xA0` / `0xA1` (bit 0 = startovní režim) |
| `FLAGS` bit 7 | `PA_PROT` | `T_SCALE` (1 = °C, 0 = °F) |
| CAT výčet | 6 položek | +TEN-TEC, +FLEX-RADIO → `RS-232` 4→6, `NONE` 5→7 |
| `SETUP OPTIONS` | `0x07`, 7 položek | `0x06`, 9 položek (+START, +TEMP.) |
| `SET ANTENNA` | `0x08`, 1 anténa/pásmo | `0x07`, **2 antény/pásmo**, index v `SETUP_0` |
| `SET TEN-TEC` | — | `0x0B` |
| antény × pásma | `0x05` | zrušeno (`0x05` = `DATA STORED!`) |

Celá mapa `DISPLAY_CTX` se od `0x05` výš **posunula o jedna**. Proto konzole proti
firmware Second Series nic nezobrazovala — rámce chodily a checksum seděl, ale
`STATUS_CODE` neodpovídal a všechny kódy menu byly posunuté.

> ⚠️ **Důsledek úrovňového řízení:** zavření sériového portu shodí DTR, takže
> **restart nebo pád daemonu zesilovač vypne**. Je to bezpečná strana selhání,
> ale při restartu za provozu na to pozor. `--dtr-on-start` zajistí, že po
> startu daemonu zesilovač zase naběhne.

`--raw-port 7373` zachová raw TCP rozhraní, takže `expert-console.sh` s originální
Windows aplikací funguje dál — beze změny skriptu a bez asertování DTR.

## Ovládání

| Tlačítko | Kód | | Tlačítko | Kód |
|---|---|---|---|---|
| `←L` `L→` `←C` `C→` | 0x30–0x33 | | `OPERATE` | 0x1C |
| `TUNE` | 0x34 | | `ON` / `OFF` | *DTR nahoru / dolů* |
| | | | `PWR-L` / `PWR-H` | 0x1A (HALF/FULL) |
| `IN` `←BAND` `BAND→` `ANT` `CAT` | 0x28–0x2C | | `POWER` (FULL/HALF) | 0x1A |
| `←` `→` `SET` | 0x2D–0x2F | | `DISPLAY` | 0x1B |

> Obě revize uvádějí u `0x33` shodně `L+`, stejně jako u `0x31`. Podle rozložení
> kláves (`0x32` je `C-`) je to `C+` — chyba se táhne dokumentací dál.

Horní řádek ukazuje **stav, ne akci** — kliknutím se přepíná:
`OFF`/`ON` (šedivě/zeleně), `STANDBY`/`OPERATE` (šedivě/zeleně),
`PWR-L`/`PWR-H` (šedivě/pískově). Zbytek klávesnice je v zarolovací sekci.

> ⚠️ **Zamčená klávesnice při TX.** Dokud zesilovač vidí `TX`, ignoruje
> `BAND±`, `ANT`, `CAT`, `IN` i `SET` — do setupu se pak nedá vstoupit.
> Ověřeno měřením: `FLAGS` trvale `0x84` (TX) při 0,0 W budicího výkonu,
> reagovaly jen `OPERATE`, `MODE`, `OFF` a `DISPLAY`. Typická příčina je
> **vypnutý transceiver držící PTT**. Konzole na to upozorní a navigace
> se v tom stavu ani nerozjede.

## Bez adresního řádku a záložek

Lišty prohlížeče zaberou na malé obrazovce znatelnou část výšky. Tři cesty,
seřazené podle toho, co po vás chtějí předem:

**1. Režim aplikace — hned, bez čehokoli navíc.** Okno bez záložek i adresního
řádku:

    google-chrome --app=http://192.168.1.201:8080/
    chromium      --app=http://192.168.1.201:8080/
    firefox       --kiosk http://192.168.1.201:8080/     # celá obrazovka

Dá se z toho udělat zástupce na ploše a je to hotová věc.

**2. Celá obrazovka — `F11`.** Nulová příprava, ale musíte to zmáčknout po
každém otevření.

**3. Instalace jako aplikace — nejhezčí, ale chce HTTPS.** Konzole servíruje
`manifest.webmanifest` i ikonu, takže prohlížeč nabídne *Instalovat aplikaci*
a poběží ve vlastním okně s vlastní ikonou. Háček: Chrome instalaci nabídne
jen přes **HTTPS nebo z localhostu** — přes holé `http://192.168.1.201` se
nabídka neobjeví. Až dáte před daemon Apache s certifikátem (viz Nasazení),
začne to fungovat samo, nic dalšího se nastavovat nemusí.

Na **iOS** je to jednodušší: *Sdílet → Přidat na plochu* funguje i přes holé
HTTP a stránka se pak spustí bez lišt Safari.

## Setup strom

Menu je stavový automat **uvnitř zesilovače**. Neexistuje příkaz „nastav anténu pro
20 m na #2" — jde jen posílat `SET` / `←` / `→` a číst, kde to právě je. Klikací strom
proto na uzel neskáče, ale **dojde tam** uzavřenou smyčkou:

    pošli JEDNU klávesu → počkej na potvrzení v dalším STATUS paketu → další

Když zesilovač do několika pokusů nenásleduje, navigace se zastaví a napíše proč.
Nikdy nebuší klávesy naslepo do kilowattového stupně.

Kde je výběr položky zároveň hodnotou (`SET CAT`, `SET YAESU/ICOM/TEN-TEC`,
`SET BAUDRATE`), mapuje se to na rozbalovací pole: vybrat → *Apply* → aplikace dojde
na tu položku a potvrdí `SET`em. `MANUAL TUNE` a `BACKLIGHT` mají +/− tlačítka,
`SET ANTENNA` klikací pásma.

> Trasa do `SET BAUDRATE` není ve specifikaci popsaná, takže se do něj nenaviguje —
> zůstávají manuální šipky.

Bargrafy se škálují podle režimu: PA OUT 1200 W ve FULL, 600 W v HALF, a ve
STANDBY přepne na rozsah budiče. Bajty 23–24 mají dvojí význam — v OPERATE zisk
v dB, ve STANDBY SWR — a popisek se přepíná s nimi.

## Nasazení

Systemd jednotka:

    [Service]
    ExecStart=/opt/expert/expert_console.py --port /dev/ttyUSB.pa \
              --raw-port 7373 --listen 127.0.0.1 --http-port 8080
    Restart=always
    User=dan

Apache jako proxy, když je potřeba HTTPS a heslo zvenku. SSE nepotřebuje
`mod_proxy_wstunnel`:

    <Location /expert>
      AuthType Basic
      AuthUserFile /etc/apache2/.htpasswd
      Require valid-user
      ProxyPass        http://127.0.0.1:8080/
      ProxyPassReverse http://127.0.0.1:8080/
      SetEnv proxy-sendchunked 1
    </Location>

Daemon poslouchá na `127.0.0.1`, takže bez proxy není zvenku dosažitelný.

## Přepnutí ze ser2netu

1. `systemctl stop ser2net && systemctl disable ser2net`
2. Spustit daemon s `--port` a `--raw-port 7373`
3. `expert-console.sh` funguje dál beze změny (míří na `192.168.1.201:7373`)

## Ověření na železe

Pořadí je záměrné — nejdřív čtení, pak napájení, výkon nakonec.

1. Zesilovač už zapnutý: čísla a bargrafy musí sedět s čelním panelem,
   watchdog musí držet stream (v Diagnostice roste „Rámce OK").
2. `ON` z webu zvedne DTR → PA naběhne za ~7 s → watchdog obnoví stream.
3. `OFF` z webu srazí DTR → PA se vypne.
4. Ověřit, že `0x33` je `C+`, ne `L+`. PDF str. 7 uvádí u 0x33 chybně „L+"
   stejně jako u 0x31; podle rozložení kláves a screenshotu je to `C+`.
5. `TUNE` a `OPERATE` až nakonec, do zátěže.

### Referenční záznam

Nejsilnější ověření je porovnání s originální aplikací nad **týmiž bajty**:

    DEBUG=1 ./expert-console.sh          # hexdump do /tmp/expert1k.log

Během záznamu projít setup menu, OPERATE, krátce vysílat, přepnout HALF/FULL.
Pak `./expert_console.py --replay /tmp/expert1k.log` a porovnat s tím, co
v daném okamžiku ukazoval originál. Log ve směru `>` zároveň ukáže, jestli
originál streamuje přes `RCU_ON` nebo polluje, a potvrdí kódy kláves.

Souběžně: daemon s `--raw-port 7373`, na něj `expert-console.sh` — originál
a webová konzole pak zobrazují tytéž bajty vedle sebe.

## Testy

    ./test/run.sh          # dekodér + vykreslování, bez hardwaru
    ./test/run.sh --e2e    # navíc end-to-end proti simulátoru

`decode.test.js` a `render.test.js` pouštějí **kód vytažený z `index.html`**,
ne jeho kopii — testuje se to, co se opravdu nasazuje.

## Distribuce jedním souborem

`index.html` ležící vedle skriptu má přednost před vloženou kopií, takže při
vývoji stačí editovat HTML a dát F5. Před distribucí vložit aktuální podobu:

    ./expert_console.py --embed

Pak `expert_console.py` funguje sám o sobě, bez `index.html`.

## Přepínače

| Přepínač | Výchozí | Význam |
|---|---|---|
| `--port` | — | sériový port, např. `/dev/ttyUSB0` |
| `--simulate` | — | syntetický zesilovač (`--scenario normal\|alarm\|hot`) |
| `--replay FILE` | — | přehrání záznamu `socat -x -v` |
| `--record FILE` | — | zapisovat provoz do souboru |
| `--listen` | `127.0.0.1` | adresa HTTP serveru |
| `--http-port` | `8080` | port konzole |
| `--raw-port` | vypnuto | raw TCP pro originální aplikaci |
| `--auto-shutdown` | `0` | po N minutách bez klienta poslat `OFF` |
| `--dtr-mode` | `level` | `level` = DTR nahoře zapnuto; `pulse` = zapalovací pulz |
| `--dtr-on-start` | vypnuto | zapnout zesilovač hned při startu daemonu |
| `--dtr-pulse-ms` | `1000` | délka pulzu, jen pro `--dtr-mode pulse` |
| `--embed` | — | vložit `index.html` do skriptu a skončit |

## Rozsah

Fáze 1 řeší funkčnost, ne vzhled. Mimo rozsah: klikací navigace setupem
s uzavřenou smyčkou, `CAT_232` ladění, logování telemetrie na server,
více zesilovačů.

## Zdroje

- `EXPERT_1K-FA_RS232_PROTOCOL.pdf` — specifikace protokolu, Rev. 1.0
- `EXPERT_1K-FA_RS232_PROTOCOL_2.pdf` — specifikace protokolu, Rev. 2.0
- `1200w.png` — screenshot originální Windows konzole
- `expert-console.sh` — originální aplikace pod wine, referenční implementace
