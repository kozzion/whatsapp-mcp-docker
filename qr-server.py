"""
Serves the WhatsApp bridge's pairing QR code as a localhost web page.

WhatsApp rotates the pairing code every ~20s, so this tails the container's
logs for the latest `QR_CODE_RAW:` line, renders it to a PNG server-side
(no third-party services involved), and the page refreshes the image.

Usage:  python qr-server.py   ->  open http://localhost:8765
"""
import http.server
import socketserver
import subprocess
import threading
import io
import json
import urllib.request
import qrcode

PORT = 8765
CONTAINER = "whatsapp-mcp"
CONTROL_URL = "http://localhost:8080/api"   # bridge login control API
_latest = {"code": ""}


def _bridge_post(path):
    try:
        req = urllib.request.Request(CONTROL_URL + path, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except Exception as e:
        return 502, json.dumps({"success": False, "message": str(e)}).encode()

def tail_logs():
    # Follow the container's stdout and keep the most recent QR code.
    proc = subprocess.Popen(
        ["docker", "logs", "-f", "--tail", "50", CONTAINER],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for line in proc.stdout:
        marker = "QR_CODE_RAW:"
        if marker in line:
            _latest["code"] = line.split(marker, 1)[1].strip()

def render_png(code):
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(code)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>WhatsApp QR</title>
<style>
  body{font-family:system-ui,sans-serif;text-align:center;background:#111;color:#eee;padding:24px}
  img{background:#fff;padding:16px;border-radius:12px}
  .muted{color:#888;font-size:14px;max-width:520px;margin:12px auto}
  button{font-size:16px;padding:10px 20px;margin:6px;border:0;border-radius:8px;cursor:pointer}
  #gen{background:#25d366;color:#053} #stop{background:#444;color:#eee}
  #wrap{display:none}
</style></head><body>
<h2>Scan with WhatsApp &rarr; Linked Devices &rarr; Link a Device</h2>
<p class="muted">For your safety the QR is <b>not</b> shown until you ask for it —
each scan is a linking attempt, and too many in a row makes WhatsApp say
"couldn't link device, try again later". Click Generate only when you're
ready to scan, and stop as soon as you're paired.</p>
<button id="gen" onclick="start()">Generate QR code</button>
<div id="wrap">
  <div><img id="qr" alt="QR" width="320" height="320"></div>
  <p class="muted" id="status"></p>
  <button id="stop" onclick="stop()">Stop</button>
</div>
<script>
let timer = null;
async function tick(){
  try{
    const code = (await (await fetch('/code')).text()).trim();
    const status = document.getElementById('status');
    if(!code){ status.textContent = "waiting for bridge to emit a code…"; return; }
    if(code === "PAIRED"){ status.textContent = "✅ already paired — you can stop and close this tab"; stop(); return; }
    document.getElementById('qr').src = '/qr.png?t=' + Date.now();
    status.textContent = "code refreshes automatically — scan promptly, then click Stop";
  }catch(e){ document.getElementById('status').textContent = "server gone — is qr-server.py still running?"; }
}
async function start(){
  if(timer) return;
  document.getElementById('gen').disabled = true;
  document.getElementById('wrap').style.display = 'block';
  document.getElementById('status').textContent = "asking the bridge to start pairing…";
  try{ await fetch('/start', {method:'POST'}); }catch(e){}
  tick();
  timer = setInterval(tick, 2000);
}
async function stop(){
  if(timer){ clearInterval(timer); timer = null; }
  try{ await fetch('/stop', {method:'POST'}); }catch(e){}
  document.getElementById('wrap').style.display = 'none';
  document.getElementById('gen').disabled = false;
}
</script>
</body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/code":
            self._send(_latest["code"].encode(), "text/plain")
        elif self.path.startswith("/qr.png"):
            code = _latest["code"]
            if not code:
                self.send_response(404); self.end_headers(); return
            self._send(render_png(code), "image/png")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path == "/start":
            _latest["code"] = ""          # drop any stale code before pairing
            status, body = _bridge_post("/login")
            self._send(body, "application/json")
        elif self.path == "/stop":
            status, body = _bridge_post("/logout")
            self._send(body, "application/json")
        else:
            self._send(b'{"error":"not found"}', "application/json")

if __name__ == "__main__":
    threading.Thread(target=tail_logs, daemon=True).start()
    print(f"Open http://localhost:{PORT} in your browser")
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        httpd.serve_forever()
