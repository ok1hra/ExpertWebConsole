# EXPERT 1K-FA — Web Console

![Web console](ExpertWebConsole.png)

A web replacement for `Expert_Console2.exe`, the Windows console for the SPE
EXPERT 1K-FA linear amplifier — usable from a remote QTH. A small daemon owns
the serial port and serves its own page; the protocol lives in `index.html`.

With `--trxnet` the daemon also joins the [TrxNet](https://github.com/ok1hra/TrxNet)
device network as `PA.01` and keeps working **with no browser open at all** —
see [TrxNet](#trxnet).

---

## Quick start

Install the only dependency (needed for live operation only):

    apt install python3-serial

Try it without any hardware — the simulator answers keystrokes and streams
plausible telemetry:

    ./expert_console.py --simulate

Then open <http://127.0.0.1:8080/>.

Run it against the amplifier:

    ./expert_console.py --port /dev/ttyUSB.pa --raw-port 7373 --listen 0.0.0.0

Press **ON** in the top row and wait roughly seven seconds — the amplifier
raises DTR, boots, and the watchdog restarts the telemetry stream by itself.

Three things worth knowing before you start:

- Without `--listen 0.0.0.0` the console is reachable only from the machine
  itself. This applies to `--raw-port` as well.
- The serial port is opened **exclusively**. If `ser2net` still holds it, the
  daemon says so instead of quietly fighting over the bytes.
- Nothing on screen? Open **Diagnostics** and read `RX bytes` — see
  [Troubleshooting](#troubleshooting).

If the amplifier does not answer at all, this splits the problem into "is the
port taken", "does DTR switch" and "does the amplifier reply":

    sudo ./test/diag-serial.py /dev/ttyUSB.pa

---

## How it fits together

    browser ──http──► expert_console.py ──► /dev/ttyUSB.pa ──► 1K-FA
                      (HTML + SSE + serial)      9600 8N1
      TrxNet ──udp──►

The daemon is deliberately thin: serial port, DTR, HTTP, and passing bytes
through. Framing, checksums, the 30-byte status record and the setup menus are
all decoded in the browser.

The one exception is TrxNet. A network device cannot depend on someone having a
page open, so the daemon carries its own framer and decodes the handful of
STATUS fields it publishes — about sixty lines, deliberately not the whole
record. `test/decode_test.py` and `test/decode.test.js` run against the same
`test/fixture.log`, which is what keeps the two decoders from drifting apart.

## Running the original Windows console under wine

`expert-console.sh` launches the original `Expert_Console2.exe` under wine. It
serves as the **reference implementation** — useful whenever you are unsure
whether the web console is reading something correctly.

    ./expert-console.sh                 # normal run
    DEBUG=1 ./expert-console.sh         # plus a hex dump to /tmp/expert1k.log

The script bridges TCP to a PTY with `socat`, links it to `COM1` inside wine and
starts the application. It points at `192.168.1.201:7373`, which is what our
daemon's `--raw-port 7373` serves — so it keeps working **unchanged** after you
retire ser2net.

With `DEBUG=1` you get paired ground truth: the bytes in the log, and the
reference interpretation of those same bytes on screen. That is exactly how the
decoder in this project was verified.

> The script hard-codes `EXE=/home/dan/inst/Expert_Console2.exe`, and wine
> rewrites `dosdevices/com1` on startup — hence the ordering inside: first
> `wineserver -p`, only then the symlink.

## Why ser2net had to go

Because of **DTR**. ser2net raises it when a client connects and leaves it
there, which controls the amplifier's power as a side effect you cannot steer.

**DTR is a level switch, not an ignition pulse.** Measured on the bench first:

    1000 ms pulse, then DTR low  →  nothing for 45 s
    DTR held high                →  reply after 6.7 s

Protocol **Rev. 2.0** then confirmed it in as many words (p. 4): turning on is
*"simply raising it at a voltage level greater than +5 Vdc"* and takes **3 to
4.5 seconds**; turning off means *"this control line has to be reset in its OFF
state"*, about a second, because the state is sampled for at least 500 ms.

    DTR high = amplifier running          DTR low = off

After power-up the amplifier resets `RCU` to `OFF`, so telemetry only resumes
once the browser's watchdog sends `RCU_ON` again. It retries every 1.5 s, so
waiting is enough.

The port is opened **exclusively** (`TIOCEXCL`). Without that, pyserial happily
opens a port ser2net already holds; both then read from the same device and the
incoming bytes are split between them at random — which looks exactly like
"writes go out, nothing comes back".

If a different unit behaves the way Rev. 1.0 describes, use `--dtr-mode pulse`.

> ⚠️ Closing the port drops DTR, so **restarting or crashing the daemon turns
> the amplifier off**. That is the safe direction to fail in, but be aware of it
> when restarting during operation. `--dtr-on-start` brings it back up with the
> daemon.

## Two protocol revisions

Both are supported and told apart by `STATUS_CODE` — there is nothing to
configure, and the active revision is shown in Diagnostics.

| | Rev. 1.0 | Rev. 2.0 |
|---|---|---|
| firmware | `06_11_06_x` | `>= 07_07_07_M` (*Second Series*) |
| `STATUS_CODE` | `0x80` | `0xA0` / `0xA1` (bit 0 = startup mode) |
| `FLAGS` bit 7 | `PA_PROT` | `T_SCALE` (1 = °C, 0 = °F) |
| CAT list | 6 entries | +TEN-TEC, +FLEX-RADIO → `RS-232` 4→6, `NONE` 5→7 |
| `SETUP OPTIONS` | `0x07`, 7 items | `0x06`, 9 items (+START, +TEMP.) |
| `SET ANTENNA` | `0x08`, 1 antenna/band | `0x07`, **2 antennas/band**, index in `SETUP_0` |
| `SET TEN-TEC` | — | `0x0B` |
| antennas × bands | `0x05` | dropped (`0x05` = `DATA STORED!`) |

The whole `DISPLAY_CTX` map **shifted by one** from `0x05` upwards.

## Controls

The top row shows **state, not action** — clicking toggles it. `OPERATE` and
`PWR-H` are drawn inverted (filled), because those are the states in which the
amplifier is actually working. Next to them sits the calculated `SWR`, visible
only while transmitting and for a moment afterwards.

| Button | Code | | Button | Code |
|---|---|---|---|---|
| `ON` / `OFF` | *DTR high / low* | | `←L` `L→` `←C` `C→` | 0x30–0x33 |
| `STANDBY` / `OPERATE` | 0x1C | | `IN` `←BAND` `BAND→` | 0x28–0x2A |
| `PWR-L` / `PWR-H` | 0x1A | | `ANT` `CAT` | 0x2B, 0x2C |
| `TUNE` | 0x34 | | `←` `→` `SET` | 0x2D–0x2F |
| | | | `DISPLAY` | 0x1B |

> Both revisions list `0x33` as `L+`, the same as `0x31`. Going by the keyboard
> layout (`0x32` is `C-`) it is really `C+` — the error persists through both
> documents.

### Bar graphs

Each bar is built from LED segments on a fixed 7 px pitch (5 px lit, 2 px gap).
Narrowing the window reduces their number rather than their size, and the unlit
segments stay visible so the full span is always readable.

- the **number beside the bar** shows the **peak**, refreshed slowly enough to
  be legible; the bar itself carries the instantaneous value
- `FW`, `REV` and `I` hold the maximum, **`V` holds the minimum** — with supply
  voltage the interesting figure is the sag under load, not the open-circuit
  reading
- a peak is drawn as a lit segment above the illuminated part, a dip as a
  **bright notch** inside it
- ranges follow the mode: `FW` is 1200 W in FULL, 600 W in HALF, and switches to
  the exciter range in STANDBY
- `SWR` is calculated from forward and reflected power — in OPERATE the
  amplifier does not send it, bytes 23–24 carry gain instead

Temperature is coloured by the fan switching thresholds (manual §18.17) and
honours both CONTEST mode and the °F setting:

    CONTEST off:  <40 grey · 40 green · 65 yellow · 75 sand · 90 red
    CONTEST on:   first stage always running, second from 60, third from 70

## Setup tree

The menu is a state machine **inside the amplifier**. There is no "set the
antenna for 20 m to #2" command — only `SET` / `←` / `→`, plus reading back
where it currently is. So clicking a node cannot jump anywhere; it has to **walk
there**, in a closed loop:

    send ONE key → wait for the next STATUS packet to confirm → then the next

If the amplifier does not follow within a few attempts, the walk stops and says
so. It never drums blind keystrokes into a kilowatt amplifier.

Where selecting an item *is* the value (`SET CAT`, `SET YAESU/ICOM/TEN-TEC`,
`SET BAUDRATE`), that maps onto a drop-down: choose, then *Apply*.
`MANUAL TUNE` and `BACKLIGHT` get +/− buttons, `SET ANTENNA` clickable bands.

> The route into `SET BAUDRATE` is not documented in the specification, so the
> walker will not enter it — the manual arrows still work.

## TrxNet

The console can join [TrxNet](https://github.com/ok1hra/TrxNet), the P2P network
the remoteQTH devices use — UDP broadcast discovery, a minimal CoAP, no broker.
The amplifier appears as `PA.01`, follows the transceiver's frequency and can be
operated from any other device on the segment.

    ./expert_console.py --port /dev/ttyUSB.pa --trxnet --trxnet-id 01

**This works with no browser open.** The daemon runs the `RCU_ON` watchdog
itself — which it now does whether or not TrxNet is on, so `--record` is useful
headless too.

### Topics

Payloads are raw little-endian, as everywhere in TrxNet.

| Published | Type | Encoding |
|---|---|---|
| `/pa-flags` | u16 | bit map below |
| `/fwd` | u16 | forward power, W × 10, instantaneous (not the peak the bars show) |
| `/ref` | u16 | reflected power, W × 10, instantaneous |
| `/swr` | u16 | SWR × 100; `0` = no answer, `65535` = ∞ |
| `/band` | u8 | metres: 160, 80, 40, 30, 20, 17, 15, 12, 10, 6 |

    /pa-flags
    bit  0  TUNE      ┐
         1  OPERATE   │
         2  TX        │ the amplifier's own FLAGS byte,
         3  ALARM     │ bits 0-6 passed through unchanged
         4  FULL      │
         5  CONTEST   │
         6  BEEP      ┘
         7  always 0    PA_PROT in Rev. 1.0, T_SCALE in Rev. 2.0 — it would
                        mean two different things on the wire
         8  ON          DTR high, the amplifier is running
         9  LINK        STATUS packets are arriving (< 3 s)
        10  REV2        1 = Rev. 2.0, 0 = Rev. 1.0
     11-15  reserved, zero

> Why not plain `/flags`? That name is taken: the IC-705 interface publishes a
> CI-V bitfield under it (PTT, SPLIT, RIT…), and a consumer that met both would
> read one as the other. It already happens — the TrxNet Monitor decodes
> `/flags` as CI-V, so an amplifier in OPERATE and FULL would show up as
> "SPLIT | AFC | NR". A topic of our own means anything that does not know the
> amplifier prints raw hex, which is at least visibly undecoded.

| Subscribed | Type | Effect |
|---|---|---|
| `/hz` | u32 | the transceiver's frequency in Hz → `CAT_232` |
| `/s-on` | u8 | 0/1 → DTR low/high |
| `/s-operate` | u8 | 0 = STANDBY, 1 = OPERATE |
| `/s-full` | u8 | 0 = HALF (PWR-L), 1 = FULL (PWR-H) |
| `/s-tune` | u8 | 1 = start tuning |

While transmitting, `/fwd` `/ref` `/swr` go out with every STATUS packet — five
to eight a second, which is the point of an instantaneous reading. Otherwise
they are sent on change plus a heartbeat every five seconds. A peer that joins
gets the whole set at once, as `TRX_CON`.

**With the amplifier switched off the topics keep going**, on the same
heartbeat: `/pa-flags` carries bit 8 from DTR and bit 9 clear, and the readings
go out as "no answer". Bits 8 and 9 are the daemon's own knowledge, not the
amplifier's, and they are exactly the two worth having when no telemetry
flows — a consumer that hears nothing at all cannot tell "switched off" from
"gone", and a panel whose `ON` button toggles over the last value it heard then
keeps sending the opposite one, with nothing able to correct it.

### Commands are not keystrokes

`OPERATE` and `PWR-L/H` are **toggle keys**, so a blind press is as likely to
switch the wrong way. Each command therefore runs a closed loop — compare, send
one key, wait for the amplifier to confirm, up to three times — the same
approach the setup tree walker uses. Sending `/s-operate 1` twice leaves the
amplifier in OPERATE, as it should.

What "wait for the amplifier to confirm" has to mean is **a STATUS newer than
the keystroke**, not a timer. Measured on the wire: the amplifier ACKs an
`OPERATE` key in 52 ms and then goes completely quiet for about **1.2 s** while
it throws the relays — no ACK and no STATUS, though the stream otherwise runs at
eight packets a second. A loop retrying on a wall clock therefore pressed a
toggle key two more times inside the window in which it could not possibly have
answered, and the parity of the press count decided where the amplifier ended
up. It even looked like a success: the loop saw `OPERATE` arrive, called itself
done, and the presses already inside the amplifier undid it a second later —
which is precisely "it went to OPERATE and came back after two seconds". So a
retry now waits for a STATUS that arrived at least 1.5 s after the keystroke and
still disagrees; while the amplifier is quiet, nothing is sent. `test/toggle_test.py`
holds the amplifier silent for the measured 1.2 s and fails on the second
keystroke.

A command that arrives while no telemetry is flowing is held for ten seconds and
then dropped. That covers the one case worth covering: `/s-on 1` and
`/s-operate 1` sent together, with the amplifier taking about seven seconds to
come up.

### Following the transceiver

`/hz` is turned into a `CAT_232` frame (`0x82 LO HI`, kHz). A frame goes out at
once when the frequency crosses into another of the tuner's 127 sub-bands, and
otherwise at most once a second, so the frequency on the amplifier's display
keeps up without flooding a link that allows eight commands a second.

> ⚠️ `CAT_232` only has an effect with **CAT set to `RS-232`** in the amplifier's
> menu (protocol Rev. 2.0, p. 7). The daemon says so in the log and in
> `/health`, and keeps sending regardless. Note what that setting costs: the
> amplifier then ignores the transceiver's own CAT bus and follows the daemon
> instead.

The sub-band table is transcribed from the **user's manual §19** (p. 70) — the
protocol document gives only the index ranges. `test/subband_test.py` checks it
against that table band by band.

### Who may command the amplifier

`--trxnet-subscribe` is **off by default**. TrxNet has no authentication of any
kind, so with it on, anything on the segment can start a tune into whatever
antenna happens to be selected.

    --trxnet-allow "705.01 OI3.ff"

restricts commands to those names. Be clear about what that buys: the sender's
name is carried unsigned in the packet, so an allow list guards against a
misconfigured device, **not** against an attacker. It is not optional in one
case though — `/hz` is published by the OI3 keyer as well as the IC-705, and
without a list the amplifier would follow whichever spoke last.

### Diagnostics

`/health` grows a `trxnet` block in the canonical shape of the profile
(`INTEGRATION.md` §8.2), and the Diagnostics panel shows the same thing:

    curl -s localhost:8080/health | jq .trxnet

`tableFull` is the one to look at when a device is missing: the peer table
filled and somebody was dropped. `--trxnet-prio "705 OI3"` protects the names
that matter.

## Losing the address bar and tabs

**1. Application mode — works immediately, nothing to prepare:**

    google-chrome --app=http://192.168.1.201:8080/
    firefox       --kiosk http://192.168.1.201:8080/

**2. Full screen — `F11`.**

**3. Install as an app.** The console serves `manifest.webmanifest` and an icon,
but Chrome only offers installation over **HTTPS or from localhost**. Once
Apache with a certificate sits in front of the daemon, it starts working on its
own. On **iOS**, *Share → Add to Home Screen* works over plain HTTP too.

## Deployment

    [Service]
    ExecStart=/opt/expert/expert_console.py --port /dev/ttyUSB.pa \
              --raw-port 7373 --listen 127.0.0.1 --http-port 8080
    Restart=always
    User=dan

Apache as a reverse proxy when you need HTTPS and a password from outside. SSE
does not need `mod_proxy_wstunnel`:

    <Location /expert>
      AuthType Basic
      AuthUserFile /etc/apache2/.htpasswd
      Require valid-user
      ProxyPass        http://127.0.0.1:8080/
      ProxyPassReverse http://127.0.0.1:8080/
      SetEnv proxy-sendchunked 1
    </Location>

### With TrxNet

    [Service]
    ExecStart=/opt/expert/expert_console.py --port /dev/ttyUSB.pa \
              --raw-port 7373 --listen 127.0.0.1 --http-port 8080 \
              --trxnet --trxnet-id 01 --trxnet-subscribe \
              --trxnet-allow "705.01" --trxnet-prio "705 OI3"
    Restart=always
    User=dan

Discovery is a **broadcast**: it does not cross routers or subnets, and guest
networks, mesh systems and AP client isolation all silently swallow it. Check it
on the network it will actually run on.

### Migrating from ser2net

1. `systemctl stop ser2net && systemctl disable ser2net`
2. Start the daemon with `--port` and `--raw-port 7373`
3. `expert-console.sh` keeps working unchanged

## Troubleshooting

**The console is empty, no data arriving.** Open Diagnostics and look at
`RX bytes`. Rising while `Frames OK` stays at zero means bytes are arriving but
falling apart — wrong speed, interference, or a shared port. Zero means nothing
is coming off the serial line at all. Frames that cannot be decoded are printed
in full hex.

**The port is busy.** `sudo fuser -v /dev/ttyUSB.pa` and
`systemctl status ser2net`.

**`SET`, `BAND`, `ANT`, `CAT` and `IN` do nothing while `OPERATE` and `MODE`
work.** The amplifier sees `TX` and locks everything that would move the RF path
while transmitting. The console warns about this once `TX` with zero drive power
has persisted for more than two seconds. The usual cause is **a powered-off
transceiver holding PTT**.

**Key probe** in Diagnostics: sends a key, watches the state and reports what
moved. `Hold` sends it 8 times a second for 1.5 s — both revisions give 8/s as
the ceiling for the link.

## Verifying against the hardware

1. With `--auto-shutdown 0` and the amplifier already on: the readings and bars
   must match the front panel, and the `RCU_ON` watchdog must keep the stream up.
2. `ON` from the web raises DTR → the PA comes up in about 7 s.
3. `OFF` from the web drops DTR → the PA shuts down.
4. Confirm that `0x33` is `C+` and not `L+`, by comparison with the original.
5. `TUNE` and `OPERATE` last, into a load.

With `--trxnet --trxnet-subscribe`, in this order:

6. `/s-on 1` → DTR rises, the PA comes up in about 7 s, `/pa-flags` bits 8 and 9
7. `/s-full` there and back — and **twice the same value**, which must not
   toggle it
8. `/hz` from a live transceiver → the bands follow, `/band` matches the front
   panel
9. `/s-tune` **into a dummy load**, last of all

## Tests

    ./test/run.sh          # decoder and rendering, no hardware needed
    ./test/run.sh --e2e    # plus end-to-end against the simulator

`decode_test.py` covers the daemon's own decoder and `subband_test.py` the
tuner's sub-band table; `toggle_test.py` drives the command loop against an
amplifier that falls silent for the measured 1.2 s, which the simulator cannot
show because it flips on the byte; `trxnet_e2e.py` joins a real TrxNet peer to
the simulator and drives the amplifier over the network — including with the
amplifier switched off, where the topics have to keep going. That last one binds
**port 5799, not 5683** — on a machine sitting on the real network, a test
announcing itself as `PA.01` would be picked up by the actual fleet.

`decode.test.js` and `render.test.js` run **code extracted from `index.html`**
rather than a copy of it, so what gets tested is what actually ships.
`render.test.js` uses a minimal DOM shim, which lets the rendering logic and the
setup-tree walker be verified without a browser. `bundle.py` builds a standalone
copy in a temporary directory and checks that it really runs with no
`index.html` in sight.

## Single-file distribution

`expert_console.py` always reads `index.html` from beside itself, so during
development you edit the HTML and press F5. There is exactly one copy of the
page, and nothing to keep in sync.

When you need one file to drop on another machine:

    ./expert_console.py --bundle expert-standalone.py

That writes a standalone copy with the page folded in — it runs anywhere with
no companion files. The bundle is a build artifact, not a source: it is
gitignored, and running `--bundle` on a bundle is refused.

> Earlier versions folded the page back into `expert_console.py` itself. That
> left two copies of the same content, doubling every GUI change in the history
> and drifting apart whenever the step was forgotten.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--port` | — | serial port, e.g. `/dev/ttyUSB.pa` |
| `--simulate` | — | synthetic amplifier (`--scenario normal\|alarm\|hot`) |
| `--replay FILE` | — | replay a `socat -x -v` capture |
| `--record FILE` | — | write traffic to a file |
| `--listen` | `127.0.0.1` | bind address for both HTTP and the raw port |
| `--http-port` | `8080` | console port |
| `--raw-port` | off | raw TCP for the original application |
| `--auto-shutdown` | `0` | power down after N minutes with no client |
| `--dtr-mode` | `level` | `level` = DTR high means on; `pulse` = ignition pulse |
| `--dtr-on-start` | off | power the amplifier up as the daemon starts |
| `--dtr-pulse-ms` | `1000` | pulse length, `--dtr-mode pulse` only |
| `--bundle FILE` | — | write a standalone copy with the page folded in, then exit |
| `--trxnet` | off | join the TrxNet network |
| `--trxnet-id` | `01` | NET_ID, two hex digits; `00` is the reserved "disabled" value |
| `--trxnet-type` | `PA` | device type prefix, giving `PA.01` |
| `--trxnet-port` | `5683` | UDP port, shared by every device on the network |
| `--trxnet-subscribe` | off | act on commands from the network |
| `--trxnet-no-publish` | off | announce presence but publish no state |
| `--trxnet-prio` | — | name prefixes to keep when the peer table fills |
| `--trxnet-allow` | — | peer names allowed to command the amplifier; empty = anyone |

## Scope

Out of scope: walking the setup menus from the daemon (antennas, CAT, backlight
stay in the browser), server-side telemetry logging, multiple amplifiers.

`CAT_232` used to be out of scope and no longer is — it is how `/hz` reaches the
amplifier. The daemon does not publish `/hz` itself: it subscribes to it, and
publishing the same topic would feed a loop back through the other devices.

## Sources

- `EXPERT_1K-FA_RS232_PROTOCOL_2.pdf` — protocol **Rev. 2.0**, covering the
  *CE/FCC Compliant Second Series* with firmware `>= 07_07_07_M`
- `expert_manual_v20.pdf` — user's manual (§18.17 has the fan thresholds)
- `expert-console.sh` + `Expert_Console2.exe` — the original console under wine

Support for **Rev. 1.0** (firmware `06_11_06_x`) is kept in the code, but its
specification is not in this repository — it can be downloaded from
`linear-amplifier.com`, as the opening notice in Rev. 2.0 points out.
