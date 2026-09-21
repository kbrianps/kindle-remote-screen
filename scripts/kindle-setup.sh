#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Starts the Kindle-side piece that does not survive a reboot: the ktouch
# virtual touchscreen. Run it once after each Kindle boot, before opening the
# page. Safe to repeat: it only starts what is not running. It writes only to
# /tmp and /dev/.udev (both in RAM), so a reboot undoes everything.
#
# Usage: scripts/kindle-setup.sh [ssh-host]      (default host: kindle)
#
# WARNING: never kill ktouch while the Kindle UI is running. Removing a
# virtual input device made the Kindle's old Xorg crash once; it respawns by
# itself, and step 2 below brings the UI back, but it is best avoided.
set -u
K=${1:-kindle}
run(){ ssh -o BatchMode=yes -o ConnectTimeout=8 "$K" "$@"; }

if ! run 'echo ok' >/dev/null 2>&1; then
  echo "Kindle is not answering over SSH ($K). Wake it up (power button) and try again."; exit 1
fi
if ! run 'test -x /mnt/us/ktouch && test -x /mnt/us/fbstream'; then
  echo "Binaries missing on the Kindle. Run: make install HOST=$K"; exit 1
fi

# 1) virtual touchscreen: udev rule so Xorg adopts it, then the daemon
run 'mkdir -p /dev/.udev/rules.d
     R=/dev/.udev/rules.d/99-ktouch.rules
     [ -f $R ] || echo "SUBSYSTEM==\"input\", ATTRS{name}==\"ktouch\", ENV{ID_INPUT}=\"1\", ENV{ID_INPUT_TOUCHSCREEN}=\"1\"" > $R
     udevadm control --reload-rules 2>/dev/null
     if ! pidof ktouch >/dev/null; then
       : > /tmp/ktouch.cmd
       nohup setsid sh -c "tail -f /tmp/ktouch.cmd | /mnt/us/ktouch daemon" >/tmp/ktouch.log 2>&1 </dev/null &
       sleep 3
       udevadm trigger --action=add --subsystem-match=input --attr-match=name="ktouch" 2>/dev/null
       sleep 3
     fi
     echo "ktouch: $(pidof ktouch || echo FAILED)"
     grep -q "ktouch: initialized" /var/log/Xorg.0.log && echo "Xorg adopted ktouch" || echo "Xorg has not listed ktouch yet (see /var/log/Xorg.0.log)"'

# 2) if the UI went down (Xorg respawned), bring the upstart chain back up
run 'if ! pidof pillowd >/dev/null; then
       echo "UI down: bringing it back"
       start lab126_gui >/dev/null 2>&1; sleep 10
       initctl status framework | grep -q running || start framework >/dev/null 2>&1; sleep 15
       lipc-set-prop com.lab126.appmgrd start app://com.lab126.booklet.home >/dev/null 2>&1
     fi
     echo "UI: $(pidof pillowd >/dev/null && echo running || echo DOWN)"'

echo "Done. Start the server: ./kindle-remote-screen.py --host $K"
