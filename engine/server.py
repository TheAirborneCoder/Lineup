# SPDX-License-Identifier: GPL-3.0-or-later
"""The local web server: the character viewer and its API, on 127.0.0.1 only.

    /                 the character viewer (viewer/models.html)
    /grooms           the strand hair, beard and fur viewer
    /api/info         {"app": "Lineup", ...}
    /api/models/*     model_service.ModelLibrary().serve
    /api/grooms/*     groom_service.GroomLibrary().serve

Only the files listed in STATIC are served, GET only, and requests whose Host header is not this machine's loopback
address are refused, so a web page on another site cannot read the game data through the browser.
"""
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import paths

APP = "Lineup"
HOST = "127.0.0.1"
PORTS = range(8910, 8920)
VIEWER = paths.REPO / "viewer"
HTML = "text/html; charset=utf-8"
JS = "text/javascript; charset=utf-8"
CSS = "text/css; charset=utf-8"

# URL path -> (file in viewer/, MIME type). Nothing else is ever read from disk.
STATIC = {
    "/": ("models.html", HTML),
    "/models": ("models.html", HTML),
    "/grooms": ("grooms.html", HTML),
    "/models.js": ("models.js", JS),
    "/models.css": ("models.css", CSS),
    "/model-view.js": ("model-view.js", JS),
    "/char-effects.js": ("char-effects.js", JS),
    "/grooms.js": ("grooms.js", JS),
    "/world.css": ("world.css", CSS),
    "/textures.css": ("textures.css", CSS),
}
HOSTS = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", re.I)


def info():
    return {"app": APP, "game": str(paths.GAME), "work": str(paths.WORK)}


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is already in use.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 64
    daemon_threads = True
    models = None
    grooms = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    server_version = APP

    def send(self, payload, mime="application/json; charset=utf-8", status=200, cache_control="no-store"):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def refuse_method(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = refuse_method

    def do_GET(self):
        route = urlsplit(self.path)
        if not HOSTS.match(self.headers.get("Host", "")):
            return self.send({"error": "Use http://127.0.0.1"}, status=403)
        if route.path.startswith("/api/") and self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.send({"error": "Use the local viewer"}, status=403)
        args = parse_qs(route.query)

        def arg(name, default=""):
            return args.get(name, [default])[0]
        try:
            if route.path in STATIC:
                name, mime = STATIC[route.path]
                return self.send((VIEWER / name).read_bytes(), mime)
            if route.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if route.path == "/api/info":
                return self.send(info())
            if route.path.startswith("/api/models/"):
                return self.server.models.serve(self, route.path.removeprefix("/api/models/"), arg)
            if route.path.startswith("/api/grooms/"):
                return self.server.grooms.serve(self, route.path.removeprefix("/api/grooms/"), arg)
            self.send({"error": "Unknown route"}, status=404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except LookupError as exc:
            self.fail(404, exc)
        except (ValueError, OSError) as exc:
            self.fail(400, exc)
        except Exception as exc:  # a decoder failure on one model must not take the viewer down
            if os.environ.get("LINEUP_DEBUG"):
                raise
            self.fail(500, exc)

    def fail(self, status, exc):
        try:
            self.send({"error": f"{type(exc).__name__}: {exc}"}, status=status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, format, *args):
        pass


def running_here(port):
    """True when a Lineup for the same game folder already answers on this port."""
    try:
        with urlopen(f"http://{HOST}:{port}/api/info", timeout=2) as response:
            other = json.load(response)
    except (OSError, URLError, ValueError):
        return False
    return other.get("app") == APP and other.get("game") == str(paths.GAME)


def bind(port):
    """(server or None, port, already running). Tries the requested port, else the first free in PORTS."""
    for candidate in ([port] if port else list(PORTS)):
        try:
            return Server((HOST, candidate), Handler), candidate, False
        except OSError:
            if running_here(candidate):
                return None, candidate, True
            if port:
                raise OSError(f"Port {port} is in use by another program. Choose another with --port N.") from None
    raise OSError(f"Ports {PORTS.start}-{PORTS.stop - 1} are all in use. Choose another with --port N.")


def serve(port=None, open_browser=True):
    from groom_service import GroomLibrary
    from model_service import ModelLibrary
    try:
        server, port, existing = bind(port)
    except OSError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    url = f"http://{HOST}:{port}/"
    if existing:
        print(f"Lineup is already running: {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    try:
        server.models = ModelLibrary()
        server.grooms = GroomLibrary()
    except ValueError as exc:
        server.server_close()
        print(f"\n{exc}", file=sys.stderr)
        return 2
    print(f"Lineup   {url}   (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

