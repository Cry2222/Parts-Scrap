"""Local mobile web app for Volvo Penta part lookups.

Runs a small HTTP server on the phone itself (Termux) and serves a
touch-friendly page. Reuses scrape_part() from ecommerce_crawler, so the
Telegram bot and the app always share one scraping implementation.

    python mobile_app.py          # then open http://localhost:8000

Environment:
    PORT   listen port (default 8000)
    HOST   bind address (default 127.0.0.1; set 0.0.0.0 to reach it from
           another device on the same Wi-Fi)
"""

import asyncio
import json
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from ecommerce_crawler import OUTPUT_DIR, format_result, scrape_part

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1220">
<title>Parts Scrap</title>
<style>
  :root {
    --bg:#f4f6fa; --card:#fff; --fg:#111827; --muted:#6b7280;
    --line:#e5e7eb; --accent:#1d4ed8; --ok:#047857; --bad:#b91c1c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0b1220; --card:#151d2e; --fg:#e5e7eb; --muted:#9aa4b2;
      --line:#243049; --accent:#60a5fa; --ok:#34d399; --bad:#f87171;
    }
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body {
    margin:0; background:var(--bg); color:var(--fg);
    font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
  }
  header {
    position:sticky; top:0; z-index:5; background:var(--bg);
    padding:14px 16px 10px; border-bottom:1px solid var(--line);
  }
  h1 { margin:0 0 10px; font-size:19px; letter-spacing:-.01em; }
  .wrap { padding:16px; max-width:720px; margin:0 auto; }
  textarea {
    width:100%; min-height:76px; padding:12px; border-radius:12px;
    border:1px solid var(--line); background:var(--card); color:var(--fg);
    font:16px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; resize:vertical;
  }
  .row { display:flex; gap:8px; margin-top:10px; }
  button {
    flex:1; padding:13px 16px; border:0; border-radius:12px; font-size:16px;
    font-weight:600; background:var(--accent); color:#fff;
  }
  button.ghost { background:transparent; color:var(--accent); border:1px solid var(--line); }
  button:disabled { opacity:.5; }
  #status { margin-top:10px; font-size:14px; color:var(--muted); min-height:20px; }
  .bar { height:4px; background:var(--line); border-radius:4px; overflow:hidden; margin-top:8px; }
  .bar > i { display:block; height:100%; width:0; background:var(--accent); transition:width .25s; }
  .card {
    background:var(--card); border:1px solid var(--line); border-radius:14px;
    padding:14px; margin-top:12px; overflow:hidden;
  }
  .card h2 { margin:0 0 2px; font-size:17px; }
  .pn { font:13px ui-monospace, Menlo, monospace; color:var(--muted); }
  .card img {
    width:100%; max-height:230px; object-fit:contain; border-radius:10px;
    margin:10px 0; background:#fff;
  }
  dl { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; margin:10px 0 0; font-size:15px; }
  dt { color:var(--muted); }
  dd { margin:0; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
  .chip {
    font:13px ui-monospace, Menlo, monospace; padding:4px 9px; border-radius:999px;
    background:var(--bg); border:1px solid var(--line);
  }
  .tag { font-size:12px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); color:var(--muted); }
  .fail { border-color:var(--bad); }
  .fail .pn { color:var(--bad); }
  a { color:var(--accent); }
  .err { font-size:14px; color:var(--bad); margin-top:8px; word-break:break-word; }
</style>
</head>
<body>
<header>
  <h1>Parts Scrap</h1>
  <textarea id="input" placeholder="8159975 3580458" inputmode="latin" autocapitalize="characters"></textarea>
  <div class="row">
    <button id="go">Look up</button>
    <button id="save" class="ghost" disabled>Save file</button>
  </div>
  <div id="status"></div>
  <div class="bar"><i id="prog"></i></div>
</header>
<div class="wrap" id="out"></div>

<script>
const $ = s => document.querySelector(s);
let results = [];

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function card(d) {
  const ok = d.status === 'success';
  const el = document.createElement('div');
  el.className = 'card' + (ok ? '' : ' fail');

  let html = '<div class="pn">' + esc(d.part_number) +
             ' <span class="tag">' + esc(d.method) + '</span></div>';
  html += '<h2>' + esc(ok ? d.name : 'Not found') + '</h2>';

  if (ok && d.image && d.image !== 'N/A') {
    html += '<img loading="lazy" src="' + esc(d.image) + '" alt="' + esc(d.name) + '">';
  }
  if (ok) {
    html += '<dl>';
    if (d.price !== 'N/A')    html += '<dt>Price</dt><dd>' + esc(d.price) + '</dd>';
    if (d.weight !== 'N/A')   html += '<dt>Weight</dt><dd>' + esc(d.weight) + '</dd>';
    if (d.category !== 'N/A') html += '<dt>Category</dt><dd>' + esc(d.category) + '</dd>';
    html += '</dl>';

    if (d.fits_products.length) {
      html += '<div class="pn" style="margin-top:12px">Fits ' +
              d.fits_products.length + ' model(s)</div><div class="chips">' +
              d.fits_products.map(m => '<span class="chip">' + esc(m) + '</span>').join('') +
              '</div>';
    }
  }
  if (d.error) html += '<div class="err">' + esc(d.error) + '</div>';
  html += '<div style="margin-top:12px"><a href="' + esc(d.url) +
          '" target="_blank" rel="noopener">Open on volvopenta.com</a></div>';

  el.innerHTML = html;
  return el;
}

async function run() {
  const raw = $('#input').value;
  const parts = [...new Set((raw.match(/[A-Za-z0-9-]{4,}/g) || []).map(s => s.toUpperCase()))];
  if (!parts.length) { $('#status').textContent = 'Enter at least one part number.'; return; }

  results = [];
  $('#out').innerHTML = '';
  $('#go').disabled = true;
  $('#save').disabled = true;

  for (let i = 0; i < parts.length; i++) {
    $('#status').textContent = 'Looking up ' + (i + 1) + '/' + parts.length + ' — ' + parts[i];
    $('#prog').style.width = (i / parts.length * 100) + '%';
    try {
      const r = await fetch('/api/part?number=' + encodeURIComponent(parts[i]));
      const d = await r.json();
      results.push(d);
      $('#out').appendChild(card(d));
    } catch (e) {
      $('#status').textContent = 'Request failed: ' + e;
    }
  }

  $('#prog').style.width = '100%';
  const ok = results.filter(r => r.status === 'success').length;
  $('#status').textContent = 'Done — ' + ok + '/' + results.length + ' found.';
  $('#go').disabled = false;
  $('#save').disabled = results.length === 0;
}

async function save() {
  $('#save').disabled = true;
  const r = await fetch('/api/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(results)
  });
  const d = await r.json();
  $('#status').innerHTML = d.ok
    ? 'Saved <a href="/download/' + encodeURIComponent(d.filename) + '">' + esc(d.filename) + '</a>'
    : 'Could not save: ' + esc(d.error);
  $('#save').disabled = false;
}

$('#go').addEventListener('click', run);
$('#save').addEventListener('click', save);
$('#input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) run();
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PartsScrap/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload))

    def do_GET(self):
        route = urlparse(self.path)

        if route.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")

        elif route.path == "/api/part":
            number = (parse_qs(route.query).get("number") or [""])[0].strip()
            if not re.fullmatch(r'[A-Za-z0-9\-]{4,}', number):
                self._json(400, {"error": "invalid part number"})
                return
            self._json(200, asyncio.run(scrape_part(number)))

        elif route.path.startswith("/download/"):
            # basename() keeps a crafted path from escaping OUTPUT_DIR.
            name = os.path.basename(route.path[len("/download/"):])
            path = os.path.join(OUTPUT_DIR, name)
            if not name or not os.path.isfile(path):
                self._json(404, {"error": "no such file"})
                return
            with open(path, "rb") as fh:
                self._send(200, fh.read(), "text/plain; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="{name}"'})

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/save":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            results = json.loads(self.rfile.read(length) or b"[]")
            if not isinstance(results, list):
                raise ValueError("expected a list of results")
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return

        filename = f"volvo_parts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        try:
            with open(os.path.join(OUTPUT_DIR, filename), "w", encoding="utf-8") as fh:
                found = sum(1 for r in results if r.get("status") == "success")
                fh.write("Volvo Penta Parts Lookup\n")
                fh.write(f"Date: {datetime.now()}\n")
                fh.write(f"Total: {len(results)} | Success: {found}\n\n")
                for item in results:
                    fh.write(format_result(item))
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return

        self._json(200, {"ok": True, "filename": filename})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Parts Scrap app on http://{HOST}:{PORT}  (files -> {OUTPUT_DIR})")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
