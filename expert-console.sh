#!/bin/bash

SERIAL=/tmp/expert1k
HOST=192.168.1.201
PORT=7373
WINEPREFIX="$HOME/.wine"
EXE=/home/dan/inst/Expert_Console2.exe
LOG=/tmp/expert1k.log
DEBUG=${DEBUG:-0}

export WINEPREFIX

# Uklid predchoziho PTY linku
rm -f "$SERIAL"

# TCP -> virtualni serial port
if [ "$DEBUG" = "1" ]; then
    socat -x -v \
        pty,raw,echo=0,link="$SERIAL" \
        tcp:"$HOST":"$PORT",nodelay 2>"$LOG" &
else
    socat \
        pty,raw,echo=0,link="$SERIAL" \
        tcp:"$HOST":"$PORT",nodelay &
fi

SOCAT_PID=$!

# Pockej na vytvoreni PTY
for i in {1..50}; do
    [ -e "$SERIAL" ] && break
    sleep 0.1
done

if [ ! -e "$SERIAL" ]; then
    echo "Nepodarilo se vytvorit $SERIAL"
    kill "$SOCAT_PID" 2>/dev/null
    exit 1
fi

# Wine pri startu (mountmgr) prepisuje dosdevices/com1..com4 na realne
# /dev/ttyS0..3 - proto musi wineserver nabehnout DRIV, nez nalinkujeme PTY,
# a pak zustat nazivu (-p), aby uz mountmgr znovu neskenoval.
wineserver -k 2>/dev/null
sleep 1
wineserver -p
WINEDEBUG=-all wine wineboot -u >/dev/null 2>&1
sleep 1

# Wine COM1 (az ted, po startu mountmgr)
mkdir -p "$WINEPREFIX/dosdevices"
ln -sf "$SERIAL" "$WINEPREFIX/dosdevices/com1"

if [ "$(readlink "$WINEPREFIX/dosdevices/com1")" != "$SERIAL" ]; then
    echo "VAROVANI: com1 neukazuje na $SERIAL - wine ho prepsal"
fi

echo "COM1 -> $SERIAL -> $(readlink -f "$SERIAL")"
echo "TCP  -> $HOST:$PORT"
[ "$DEBUG" = "1" ] && echo "LOG  -> $LOG"

# Spust Expert Console
wine "$EXE"

# Po ukonceni aplikace ukonci socat a vrat com1 na realny port
kill "$SOCAT_PID" 2>/dev/null
wait "$SOCAT_PID" 2>/dev/null
rm -f "$SERIAL"
ln -sf /dev/ttyS0 "$WINEPREFIX/dosdevices/com1"
wineserver -k 2>/dev/null
