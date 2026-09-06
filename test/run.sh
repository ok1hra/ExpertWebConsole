#!/bin/bash
# Vsechny testy, ktere bezi bez hardwaru.
#
#   test/run.sh          jen offline testy (dekoder + vykreslovani)
#   test/run.sh --e2e    navic end-to-end proti simulatoru a pres TrxNet
set -e
cd "$(dirname "$0")"

echo "== dekoder a ramovac =="
python3 - <<'PY'
html = open('../index.html', encoding='utf-8').read()
js = html.split('<script>\n"use strict";', 1)[1]
pure = js[:js.index('async function post')]
open('pure.js', 'w', encoding='utf-8').write(
    pure + "\nmodule.exports={AMP,PROTO,Framer,decode,decodeCout,buildFrame,toHex,KEY};\n")
PY
node decode.test.js

echo
echo "== python dekoder (tytez bajty jako vyse) =="
python3 decode_test.py

echo
echo "== tabulka sub-pasem =="
python3 subband_test.py

echo
echo "== prepinaci klavesy proti mlcicimu zesilovaci =="
python3 toggle_test.py

echo
echo "== vykreslovani (DOM shim) =="
python3 make-fixture.py >/dev/null
node render.test.js

echo
echo "== standalone bundle =="
python3 bundle.py

if [ "$1" = "--e2e" ]; then
  echo
  echo "== end-to-end proti simulatoru =="
  python3 ../expert_console.py --simulate --http-port 8099 --raw-port 7399 \
      >/tmp/expert-e2e.log 2>&1 &
  SIM=$!
  trap 'kill $SIM 2>/dev/null' EXIT
  sleep 2
  python3 e2e.py
  kill $SIM 2>/dev/null
  wait $SIM 2>/dev/null || true

  echo
  echo "== end-to-end pres TrxNet =="
  # Port 5799, ne 5683: na stroji v ostre siti by se test ohlasil skutecnemu
  # fleetu jako PA.01 a IC-705 by mu zacal publikovat.
  python3 ../expert_console.py --simulate --http-port 8099 \
      --trxnet --trxnet-subscribe --trxnet-port 5799 \
      >/tmp/expert-trxnet-e2e.log 2>&1 &
  SIM=$!
  sleep 2
  python3 trxnet_e2e.py
fi
