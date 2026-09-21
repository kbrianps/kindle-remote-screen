#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
kindle-remote-screen: see and control a jailbroken Kindle from the browser.

  * Mirror: `fbstream` on the Kindle reads the framebuffer, downsamples it to
    536x724 and sends 4 bits per pixel only when the screen changes, over a
    persistent SSH stream that runs only while the page is open. The server
    keeps the latest frame in memory, so /shot answers immediately.
  * Touch: `ktouch` daemon (virtual touchscreen via uinput). Each tap is a
    line "X Y [hold]" written straight into the daemon's pipe over SSH.
  * Buttons: power and home use the Kindle's own lipc calls.
  * Verification: signature of the cached frame before/after; "ok" on the
    first change, "no effect" after 3.6 s.

Requirements on the Kindle after each boot: scripts/kindle-setup.sh.

Usage: kindle-remote-screen.py [--host kindle] [--port 8777]
"""
import sys, time, hashlib, subprocess, threading, itertools, struct, argparse, os
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: sudo apt install python3-pil")
try:
    import numpy as np
except ImportError:
    np = None

ap = argparse.ArgumentParser(description="Kindle remote screen server")
ap.add_argument("--host", default="kindle", help="SSH host of the Kindle, as in ~/.ssh/config (default: kindle)")
ap.add_argument("--port", type=int, default=8777, help="local HTTP port (default: 8777)")
ap.add_argument("--label", help="connection label shown on the page (default: USB or Wi-Fi, guessed from the host address)")
ap.add_argument("--touch-cmd", default="/tmp/ktouch.cmd", help="command file read by the ktouch daemon")
args = ap.parse_args()

SSH_HOST = args.host
PORT = args.port
CMD_FILE = args.touch_cmd
W, H = 1072, 1448          # Kindle 11th gen (KT5) panel
OW, OH = 536, 724
STREAM_CMD = "/mnt/us/fbstream 30"


def guess_label(host):
    """USB networking on the Kindle uses 192.168.15.x (UsbNetLite/USBNetwork default)."""
    try:
        out = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=5).stdout
        addr = next((l.split()[1] for l in out.splitlines() if l.startswith("hostname ")), host)
    except Exception:
        addr = host
    return "USB" if addr.startswith("192.168.15.") else "Wi-Fi"


LABEL = args.label or guess_label(SSH_HOST)

_ids = itertools.count(1)
_results = {}

# ---------- cached frame ----------
_frame = {"png": None, "sig": None, "t": 0.0, "n": 0, "alive": False}
_UNPACK = bytes(sum(([(b >> 4) * 17, (b & 15) * 17] for b in range(256)), []))


def unpack4(payload):
    """4 bits/pixel -> 8 bits/pixel (536x724)."""
    if np is not None:
        a = np.frombuffer(payload, dtype=np.uint8)
        out = np.empty(a.size * 2, dtype=np.uint8)
        out[0::2] = (a >> 4) * 17
        out[1::2] = (a & 15) * 17
        return out.tobytes()
    return b"".join(_UNPACK[2 * b:2 * b + 2] for b in payload)


def frame_sig(img):
    """Signature that ignores the status bar (the clock changes on its own)."""
    small = img.crop((0, 45, OW, OH)).resize((134, 170))
    return hashlib.md5(small.tobytes()).hexdigest()


# ---------- stay awake only while the page is open ----------
# While the page keeps requesting /shot, reset the Kindle's screensaver timer
# (touchScreenSaverTimeout). It is not a lock: in screensaver powerd rejects
# the call, so the power button still works; if the page closes or the server
# dies, the Kindle goes to sleep on its normal timeout (10 min).
VIEW_WINDOW = 20      # s without /shot = page closed or tab hidden
POKE_EVERY = 60       # s between pokes
_awake = {"view": 0.0, "poke": 0.0, "last": "-"}


def viewing():
    return time.time() - _awake["view"] < VIEW_WINDOW


def stream_loop():
    """Keep fbstream running over SSH only while the page is open."""
    while True:
        if not viewing():               # nobody watching: the Kindle does not read the screen
            time.sleep(0.3)
            continue
        p = None
        try:
            # dedicated connection (no ControlMaster): killing ssh closes the session on the Kindle
            p = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ControlPath=none",
                                  SSH_HOST, STREAM_CMD],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            def rd(n):
                b = b""
                while len(b) < n:
                    c = p.stdout.read(n - len(b))
                    if not c:
                        raise EOFError
                    b += c
                return b
            while True:
                t = rd(1)
                if t == b"F":
                    n = struct.unpack("<I", rd(4))[0]
                    img = Image.frombytes("L", (OW, OH), unpack4(rd(n)))
                    buf = BytesIO(); img.save(buf, "PNG", compress_level=1)
                    _frame.update(png=buf.getvalue(), sig=frame_sig(img), t=time.time(), n=_frame["n"] + 1, alive=True)
                elif t == b"S":
                    _frame["alive"] = True; _frame["t"] = time.time()
                if not viewing():
                    break
        except Exception:
            pass
        finally:
            _frame["alive"] = False
            if p:
                try: p.kill()
                except Exception: pass
        if viewing():
            time.sleep(2)               # stream dropped with the page open: retry
        else:
            try: ssh("kill $(pidof fbstream) 2>/dev/null; true", timeout=10)   # belt and braces
            except Exception: pass


threading.Thread(target=stream_loop, daemon=True).start()


def keepawake_loop():
    while True:
        now = time.time()
        if now - _awake["view"] < VIEW_WINDOW and now - _awake["poke"] >= POKE_EVERY:
            _awake["poke"] = now
            try:
                rc, out = ssh("lipc-set-prop com.lab126.powerd touchScreenSaverTimeout 1", timeout=15)
                _awake["last"] = "ok" if rc == 0 else ("asleep" if "NoSuchProperty" in out else "error")
            except Exception:
                _awake["last"] = "error"
        time.sleep(5)


threading.Thread(target=keepawake_loop, daemon=True).start()

# ---------- Kindle ----------
def ssh(cmd, timeout=20):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", SSH_HOST, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def fresh():
    return _frame["alive"] and _frame["png"] and time.time() - _frame["t"] < 3


def current_sig():
    return _frame["sig"] if fresh() else None


_pipe = {"path": None}

def tap_pipe():
    if _pipe["path"] is None:
        rc, out = ssh(f'for p in $(pidof tail); do grep -q {os.path.basename(CMD_FILE)} /proc/$p/cmdline 2>/dev/null && [ -w /proc/$p/fd/1 ] && {{ echo /proc/$p/fd/1; break; }}; done', timeout=15)
        _pipe["path"] = out.strip() if rc == 0 and out.strip().startswith("/proc/") else ""
    return _pipe["path"]


_chan = {"p": None}

def tap_channel():
    """An SSH session that stays open writing into the daemon's pipe: each
    tap costs a few ms instead of a whole SSH connection."""
    p = _chan["p"]
    if p is not None and p.poll() is None:
        return p
    pipe = tap_pipe()
    if not pipe:
        return None
    p = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH_HOST,
                          f"exec cat > {pipe}"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, bufsize=0)
    _chan["p"] = p
    return p


def send_tap(x, y, hold_ms=0):
    x = max(0, min(W - 1, int(x))); y = max(0, min(H - 1, int(y)))
    line = f"{x} {y} {int(hold_ms) or 45}\n".encode()
    p = tap_channel()
    if p is not None:
        try:
            p.stdin.write(line); p.stdin.flush()
            return
        except Exception:
            _chan["p"] = None; _pipe["path"] = None
    rc, out = ssh(f'echo "{line.decode().strip()}" >> {CMD_FILE}', timeout=15)   # slow fallback
    if rc != 0:
        raise RuntimeError(f"ssh failed: {out[:120]}")


def send_swipe(x1, y1, x2, y2, ms=220):
    c = lambda v, m: max(0, min(m - 1, int(v)))
    line = f"S {c(x1,W)} {c(y1,H)} {c(x2,W)} {c(y2,H)} {int(ms)}\n".encode()
    p = tap_channel()
    if p is None:
        raise RuntimeError("touch channel unavailable")
    p.stdin.write(line); p.stdin.flush()


def swipe_async(x1, y1, x2, y2, ms=220):
    tid = next(_ids); _results[tid] = "pending"
    def work():
        try:
            b = current_sig(); send_swipe(x1, y1, x2, y2, ms)
            if b is None:
                _results[tid] = "sent (no stream to verify)"; return
            deadline = time.time() + 3.6
            while time.time() < deadline:
                time.sleep(0.08)
                if current_sig() != b:
                    _results[tid] = "ok"; return
            _results[tid] = "no effect"
        except Exception as e:
            _results[tid] = f"error: {type(e).__name__}: {str(e)[:80]}"
    threading.Thread(target=work, daemon=True).start()
    return tid


def find_arrows():
    """Find the scrollbar ▲/▼ glyphs in the cached frame (536x724).
    Criterion: isolated dark blob of 4..12 rows whose per-row width forms a
    triangle. Returns (y_up, y_down) in native coordinates."""
    png = _frame["png"]
    if not png or np is None:
        return None, None
    a = np.asarray(Image.open(BytesIO(png)).convert("L"))
    col = (a[:, 495:524] < 110); dark = col.sum(axis=1)
    rows = np.where(dark >= 2)[0]; blobs = []
    if len(rows):
        s = p = rows[0]
        for r in rows[1:]:
            if r != p + 1: blobs.append((s, p)); s = r
            p = r
        blobs.append((s, p))
    up = down = None
    for s, e in blobs:
        h = e - s + 1
        if not (4 <= h <= 12): continue
        # isolated = no other 'full' row (>=2 px) nearby; the arrow's own 1 px tip does not count
        if (dark[max(0, s - 6):s] >= 2).any() or (dark[e + 1:e + 7] >= 2).any(): continue
        w = [int(x) for x in dark[s:e + 1]]
        if max(w) < 5: continue
        inc = all(w[i] <= w[i + 1] + 1 for i in range(len(w) - 1)) and w[-1] >= w[0] + 3
        dec = all(w[i] + 1 >= w[i + 1] for i in range(len(w) - 1)) and w[0] >= w[-1] + 3
        y = (s + e) // 2 * 2
        if inc and 150 <= y <= 600 and up is None: up = y
        if dec and 1100 <= y <= 1350: down = y
    return up, down


def is_reader():
    """Book reader = frame without any solid horizontal divider.
    Home, library, store and menus always have at least one; book text never
    covers >85% of the width on a thin row. The footer (reader progress bar)
    is left out of the analysis."""
    png = _frame["png"]
    if not png or np is None:
        return False
    a = np.asarray(Image.open(BytesIO(png)).convert("L"))
    cov = (a[:690, 20:516] < 120).mean(axis=1)
    return not bool((cov > 0.85).any())


def page_target(direction):
    """Page turns are only allowed in the reader. Returns (x, y, desc) or (None, reason)."""
    if not is_reader():
        return None, "not the reader; nothing done"
    return ((1000, 724, "next page") if direction in ("next", "down")
            else (70, 724, "previous page"))


def scroll_target(direction):
    """Mouse wheel, never tapping blindly:
       1) visible scroll arrow -> tap it
       2) reader -> turn the page
       3) any other screen -> nothing (returns (None, reason))"""
    if is_reader():
        return page_target("next" if direction == "down" else "prev")
    up, down = find_arrows()
    if direction == "down" and down:
        return (1016, down, "down arrow")
    if direction == "up" and up:
        return (1016, up, "up arrow")
    return None, "nothing to scroll here"


def tap_async(x, y, hold_ms=0):
    tid = next(_ids); _results[tid] = "pending"
    def work():
        try:
            b = current_sig()
            send_tap(x, y, hold_ms)
            if b is None:
                _results[tid] = "sent (no stream to verify)"; return
            deadline = time.time() + 3.6
            while time.time() < deadline:
                time.sleep(0.08)
                if current_sig() != b:
                    _results[tid] = "ok"; return
            _results[tid] = "no effect"
        except Exception as e:
            _results[tid] = f"error: {type(e).__name__}: {str(e)[:80]}"
    threading.Thread(target=work, daemon=True).start()
    return tid


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kindle</title><style>
:root{color-scheme:dark;--bg:#111;--fg:#eee;--dim:#888;--ok:#8fd18f;--bad:#e08a8a;--paper:#fff}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif;
     display:flex;flex-direction:column;align-items:center;gap:10px;padding:10px 12px}
header{display:flex;align-items:center;gap:10px;color:var(--dim);font-size:13px}
#dot{width:9px;height:9px;border-radius:50%;background:#555} #dot.on{background:var(--ok)} #dot.off{background:var(--bad)}
#dev{position:relative;padding:14px 12px 22px;border-radius:18px;background:linear-gradient(#2a2a2a,#1b1b1b);
     box-shadow:0 10px 30px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.06)}
#wrap{position:relative;line-height:0;border-radius:3px;overflow:hidden;background:var(--paper)}
#v{display:block;user-select:none;-webkit-user-drag:none;height:min(76vh,calc(92vw*1448/1072));width:auto;aspect-ratio:1072/1448;cursor:crosshair;background:var(--paper)}
#ring{position:absolute;width:28px;height:28px;margin:-14px 0 0 -14px;border-radius:50%;border:2px solid #222;opacity:0;pointer-events:none}
#ring.on{animation:pop .5s ease-out} @keyframes pop{from{opacity:.9;transform:scale(.3)}to{opacity:0;transform:scale(1.6)}}
.row{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;align-items:center}
button{background:#222;color:var(--fg);border:1px solid #3a3a3a;border-radius:8px;padding:9px 16px;font:inherit;cursor:pointer}
button:hover{background:#2c2c2c}
</style></head><body>
<header><span id="dot"></span><span id="conn">Connecting…</span><span>·</span><span>CONNLABEL</span><span>·</span><span id="fps"></span></header>
<div id="dev"><div id="wrap">
  <img id="v" alt="" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><div id="ring"></div>
</div></div>
<div class="row">
  <button id="power">Wake/Sleep</button>
  <button id="home">Home</button>
</div>
<script>
const v=document.getElementById('v'), ring=document.getElementById('ring'), dot=document.getElementById('dot'),
      conn=document.getElementById('conn'), fps=document.getElementById('fps');
let bad=0, shown=0, winStart=Date.now();

function loop(){
  if(document.hidden){                                      // hidden tab: no frame requests (and the Kindle may sleep)
    dot.className='off'; conn.textContent='Paused (background tab)'; setTimeout(loop, 1000); return; }
  const im=new Image();
  im.onload=()=>{
    v.src=im.src;
    bad=0; dot.className='on'; conn.textContent='Live';
    shown++; if(Date.now()-winStart>2000){ fps.textContent=(shown/((Date.now()-winStart)/1000)).toFixed(0)+' fps'; shown=0; winStart=Date.now(); }
    setTimeout(loop, 45);
  };
  im.onerror=()=>{ if(++bad>=3){ dot.className='off'; conn.textContent='No signal (Kindle asleep?)'; } setTimeout(loop,1200); };
  im.src=`/shot?${Math.random()}`;
}
function kxy(e){ const r=v.getBoundingClientRect();
  return [Math.round((e.clientX-r.left)/r.width*WIDTH), Math.round((e.clientY-r.top)/r.height*HEIGHT)]; }
function tap(e, hold){
  const r=v.getBoundingClientRect(), [x,y]=kxy(e);
  ring.style.left=(e.clientX-r.left)+'px'; ring.style.top=(e.clientY-r.top)+'px';
  ring.classList.remove('on'); void ring.offsetWidth; ring.classList.add('on');
  fetch(`/tap?x=${x}&y=${y}${hold?'&hold='+hold:''}`).catch(()=>{});
}
function swipe(a, b, ms){
  const [x1,y1]=kxy(a), [x2,y2]=kxy(b);
  fetch(`/swipe?x1=${x1}&y1=${y1}&x2=${x2}&y2=${y2}&ms=${ms}`).catch(()=>{});
}
// mouse wheel: scroll arrows (or page turn in the reader); with Shift, page turn
let wheelBusy=false;
v.addEventListener('wheel', async e=>{
  e.preventDefault(); if(wheelBusy) return; wheelBusy=true;
  const down=e.deltaY>0;
  await fetch(e.shiftKey ? `/page?dir=${down?'next':'prev'}` : `/scroll?dir=${down?'down':'up'}`).catch(()=>{});
  setTimeout(()=>wheelBusy=false, 900);
},{passive:false});
// keyboard: arrows and PageUp/PageDown turn pages (reader only); Home = home screen
document.addEventListener('keydown', e=>{
  let d=null; if(e.key==='ArrowRight'||e.key==='PageDown'||e.key===' ') d='next'; if(e.key==='ArrowLeft'||e.key==='PageUp') d='prev';
  if(d){ e.preventDefault(); fetch(`/page?dir=${d}`).catch(()=>{}); return; }
  if(e.key==='Home'){ e.preventDefault(); document.getElementById('home').click(); }
});
// mouse: click = tap; hold still = long press; click and drag = finger swipe on the Kindle
let downAt=0, downEv=null;
v.draggable=false; v.addEventListener('dragstart', e=>e.preventDefault());   // never drag the image itself
v.addEventListener('mousedown', e=>{ if(e.button!==0) return; e.preventDefault(); downAt=Date.now(); downEv=e; });
window.addEventListener('mouseup', e=>{
  if(e.button!==0||!downEv) return;
  const d=downEv, held=Date.now()-downAt; downEv=null;
  if(Math.hypot(e.clientX-d.clientX, e.clientY-d.clientY) < 8){ tap(d, held>=450?Math.min(held,1500):0); return; }
  swipe(d, e, Math.max(120, Math.min(held, 900)));
});
v.addEventListener('contextmenu', e=>{ e.preventDefault(); tap(e,650); });
document.getElementById('power').onclick=()=>fetch('/btn?b=power').catch(()=>{});
document.getElementById('home').onclick=()=>fetch('/btn?b=home').catch(()=>{});
loop();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(200, PAGE.replace("WIDTH", str(W)).replace("HEIGHT", str(H)).replace("CONNLABEL", LABEL),
                                  "text/html; charset=utf-8")
            if u.path == "/shot":
                _awake["view"] = time.time()
                for _ in range(30):                           # first visit: wait for the stream to come up
                    if fresh():
                        break
                    time.sleep(0.1)
                if fresh():
                    return self._send(200, _frame["png"], "image/png")
                return self._send(503, "no stream (Kindle asleep or unreachable)")
            if u.path == "/stat":
                return self._send(200, f"stream={'ok' if _frame['alive'] else 'off'} frames={_frame['n']} age={time.time()-_frame['t']:.2f}s png={len(_frame['png'] or b'')}b awake={'yes' if viewing() else 'no'} poke={_awake['last']}")
            if u.path == "/tap":
                tid = tap_async(int(q["x"][0]), int(q["y"][0]), int(q.get("hold", ["0"])[0]))
                return self._send(200, str(tid))
            if u.path in ("/scroll", "/page"):
                d = q.get("dir", ["down"])[0]
                t = scroll_target(d) if u.path == "/scroll" else page_target(d)
                if t[0] is None:
                    return self._send(200, f"none {t[1]}")
                x, y, what = t
                return self._send(200, f"{tap_async(x, y)} {what}")
            if u.path == "/swipe":
                tid = swipe_async(int(q["x1"][0]), int(q["y1"][0]), int(q["x2"][0]), int(q["y2"][0]), int(q.get("ms", ["220"])[0]))
                return self._send(200, str(tid))
            if u.path == "/result":
                return self._send(200, _results.get(int(q["id"][0]), "unknown"))
            if u.path == "/btn":
                b = q["b"][0]
                if b not in ("power", "home"):
                    return self._send(400, "unknown button")
                before = current_sig()
                if b == "home":
                    ssh("lipc-set-prop com.lab126.appmgrd start app://com.lab126.booklet.home", timeout=15)
                else:                                         # awake -> sleep, screensaver -> wake
                    ssh('if lipc-get-prop com.lab126.powerd status | grep -q "Powerd state: Active"; '
                        'then lipc-set-prop com.lab126.powerd powerButton 1; '
                        'else lipc-set-prop com.lab126.powerd wakeUp 1; fi', timeout=15)
                deadline = time.time() + 3.6
                while time.time() < deadline:
                    time.sleep(0.2)
                    if current_sig() != before:
                        return self._send(200, "ok")
                return self._send(200, "no effect")
            self._send(404, "not found")
        except Exception as e:
            self._send(500, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    print(f"Kindle: ssh {SSH_HOST} (stream {STREAM_CMD}, touch {CMD_FILE})")
    print(f"Open: http://127.0.0.1:{PORT}")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
