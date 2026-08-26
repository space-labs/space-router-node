"""Serve the real GUI frontend with a pywebview-compatible API bridge.

The shipped desktop app is `gui/assets/index.html` + `app.js` rendered in a
webview, talking to Python through `window.pywebview.api`. A packaged WKWebView
cannot be driven by a test harness, and driving the real window steals focus
from whoever is using the machine.

This serves the SAME asset files and wires the SAME `gui.api.Api` object behind
an HTTP RPC endpoint, then injects a `window.pywebview.api` shim with identical
call semantics (method name -> args -> awaited result). The frontend is
unmodified and does not know the difference, so a browser driving this is
driving the real GUI, not a mock of it.

Usage:
    python -m tests.e2e.gui_bridge [--port 8770]
"""
from __future__ import annotations

import argparse
import http.server
import inspect
import json
import mimetypes
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "gui" / "assets"

# Injected into index.html. Mirrors pywebview's bridge: every call returns a
# promise resolving to whatever the Python method returned.
_SHIM = """
<script>
(function () {
  async function rpc(method, args) {
    const r = await fetch("/__rpc", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({method: method, args: args || []}),
    });
    const body = await r.json();
    if (body.error) throw new Error(body.error);
    return body.result;
  }
  const names = window.__API_METHODS__ || [];
  const api = {};
  for (const n of names) {
    api[n] = function (...args) { return rpc(n, args); };
  }
  window.pywebview = {api: api};
  // The api object must exist before app.js runs, but the READY event must fire
  // after it has registered its listener (app.js:3059 does
  // addEventListener("pywebviewready", init) at the end of the file). Firing it
  // from <head> means nobody is listening yet and the app never initialises,
  // which renders a blank window. Real pywebview fires it after page load.
  window.addEventListener("load", function () {
    setTimeout(function () {
      window.dispatchEvent(new Event("pywebviewready"));
    }, 0);
  });
})();
</script>
"""


def build_api():
    from gui.api import Api
    from gui.config_store import ConfigStore
    from gui.node_manager import NodeManager

    return Api(ConfigStore(), NodeManager())


def make_handler(api):
    methods = [
        n for n, m in inspect.getmembers(api, predicate=callable)
        if not n.startswith("_")
    ]

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            payload = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if self.path != "/__rpc":
                return self._send(404, json.dumps({"error": "not found"}))
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            name, args = req.get("method"), req.get("args", [])
            fn = getattr(api, name, None)
            if fn is None or name.startswith("_"):
                return self._send(400, json.dumps({"error": f"no method {name}"}))
            try:
                return self._send(200, json.dumps({"result": fn(*args)}, default=str))
            except Exception as exc:  # surfaced to the page like pywebview does
                return self._send(200, json.dumps({"error": f"{type(exc).__name__}: {exc}"}))

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                html = (ASSETS / "index.html").read_text()
                inject = (
                    "<script>window.__API_METHODS__ = "
                    + json.dumps(methods)
                    + ";</script>"
                    + _SHIM
                )
                # Inject before the app's own script so the bridge exists first.
                html = html.replace("</head>", inject + "</head>", 1)
                return self._send(200, html, "text/html; charset=utf-8")
            rel = path.lstrip("/")
            target = (ASSETS / rel).resolve()
            if not str(target).startswith(str(ASSETS.resolve())) or not target.is_file():
                return self._send(404, "not found", "text/plain")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._send(200, target.read_bytes(), ctype)

        def log_message(self, *a):
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    api = build_api()
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(api),
    )
    print(f"GUI bridge on http://127.0.0.1:{args.port}  (HOME={os.environ.get('HOME')})",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
