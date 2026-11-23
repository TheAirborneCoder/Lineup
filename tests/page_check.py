#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Load the viewer in a headless Chromium browser (Edge or Chrome), pick a character and report console errors.

    python tests/page_check.py [--url http://127.0.0.1:8910] [--browser PATH] [--screenshot FILE]

Talks to the browser over the DevTools protocol with a small built-in WebSocket client, so it needs nothing beyond
the standard library. Reports: console errors and exceptions, failed or 4xx/5xx requests, requests that leave
127.0.0.1, whether the list filled, whether a character loaded and whether the canvas drew anything.
Exit status 1 when anything above is wrong.
"""
import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


class WebSocket:
    def __init__(self, url):
        host, rest = url.split("://", 1)[1].split("/", 1)
        name, port = host.split(":")
        self.sock = socket.create_connection((name, int(port)), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError("WebSocket upgrade refused: " + head.decode(errors="replace")[:200])
        self.buffer = b""

    def send(self, text):
        data = text.encode()
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(data))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
        mask = os.urandom(4)
        self.sock.sendall(bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def receive(self, timeout):
        self.sock.settimeout(timeout)
        message = b""
        while True:
            b0, b1 = self.read(2)
            size = b1 & 0x7F
            if size == 126:
                size = struct.unpack(">H", self.read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self.read(8))[0]
            payload = self.read(size)
            if b0 & 0x0F in (1, 2, 0):
                message += payload
                if b0 & 0x80:
                    return message.decode()
            elif b0 & 0x0F == 8:
                raise EOFError


class Page:
    def __init__(self, ws):
        self.ws, self.next_id = ws, 0
        self.events = []

    def call(self, method, timeout=120, **params):
        self.next_id += 1
        want = self.next_id
        self.ws.send(json.dumps({"id": want, "method": method, "params": params}))
        end = time.time() + timeout
        while time.time() < end:
            message = json.loads(self.ws.receive(max(1, end - time.time())))
            if message.get("id") == want:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            self.events.append(message)
        raise TimeoutError(method)

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.events.append(json.loads(self.ws.receive(max(0.2, end - time.time()))))
            except (socket.timeout, TimeoutError):
                break

    def evaluate(self, expression, timeout=60):
        result = self.call("Runtime.evaluate", timeout=timeout, expression=expression, returnByValue=True,
                           awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("exception", {}).get("description", "script error"))
        return result["result"].get("value")


def find_browser(explicit):
    for path in ([explicit] if explicit else []) + list(CANDIDATES) + [shutil.which(n) or "" for n in
                                                                      ("chromium", "google-chrome", "msedge")]:
        if path and os.path.isfile(path):
            return path
    raise SystemExit("No Chromium-based browser found; pass --browser PATH.")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")     # a console that cannot show a character must not stop the run
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8910/")
    ap.add_argument("--browser")
    ap.add_argument("--screenshot")
    ap.add_argument("--name", default="", help="select the first list row whose name contains this (default: first)")
    ap.add_argument("--search", default="", help="type this into the search box before selecting a row")
    ap.add_argument("--wait", type=float, default=45, help="seconds to let the character load")
    args = ap.parse_args()
    port = 9300 + os.getpid() % 500
    profile = tempfile.mkdtemp(prefix="lineup-page-")
    proc = subprocess.Popen([find_browser(args.browser), "--headless=new", "--disable-gpu-sandbox", "--no-first-run",
                             "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
                             f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--window-size=1500,950",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
                target = next(t for t in tabs if t["type"] == "page")
                break
            except (OSError, StopIteration, ValueError):
                time.sleep(0.2)
        else:
            raise SystemExit("The browser did not start.")
        page = Page(WebSocket(target["webSocketDebuggerUrl"]))
        for domain in ("Runtime", "Network", "Page", "Log"):
            page.call(f"{domain}.enable")
        page.call("Emulation.setDeviceMetricsOverride", width=1500, height=950, deviceScaleFactor=1, mobile=False)
        page.call("Page.navigate", url=args.url)
        page.pump(6)
        if args.search:
            page.evaluate("(() => { const q = document.getElementById('q'); q.value = %s;"
                          " q.dispatchEvent(new Event('input')); })()" % json.dumps(args.search))
            page.pump(4)
        listed = page.evaluate("document.querySelectorAll('#list .model-row').length")
        print(f"rows in the list: {listed}")
        tree = page.evaluate("[...document.querySelectorAll('#tree button')].map(b => b.textContent.trim())")
        print("category tree:", tree)
        nav = page.evaluate("[...document.querySelectorAll('header a')].map(a => a.textContent.trim() + ' -> ' + a.getAttribute('href'))")
        print("header links:", nav)
        if not listed:
            problems.append("the list is empty")
        else:
            page.evaluate("""(() => {
                const wanted = %s.toLowerCase();
                const rows = [...document.querySelectorAll('#list .model-row')];
                (rows.find(r => r.textContent.toLowerCase().includes(wanted)) || rows[0]).click();
            })()""" % json.dumps(args.name))
            page.pump(args.wait)
            title = page.evaluate("document.getElementById('title').textContent")
            stats = page.evaluate("document.getElementById('stats').textContent")
            status = page.evaluate("document.getElementById('status').textContent")
            print(f"selected: {title!r}  stats: {stats!r}  status: {status!r}")
            # A WebGL canvas cannot be read back with drawImage, so judge the picture the browser itself produced.
            rect = page.evaluate("(() => { const r = document.getElementById('view').getBoundingClientRect();"
                                 " return [r.x, r.y, r.width, r.height]; })()")
            shot = base64.b64decode(page.call("Page.captureScreenshot", format="png")["data"])
            try:
                import io
                from PIL import Image
                picture = Image.open(io.BytesIO(shot)).convert("RGB")
                x, y, w, h = (int(v) for v in rect)
                drew = len(picture.crop((x, y, x + w, y + h)).getcolors(1 << 24) or [])
            except ImportError:
                drew = None
            print(f"distinct colours in the viewport: {drew}")
            if drew is not None and drew < 200:
                problems.append("the viewport shows (almost) nothing (software WebGL may be unavailable)")
            if args.screenshot:
                with open(args.screenshot, "wb") as f:
                    f.write(shot)
        page.pump(1)
        errors, requests, failed, external = [], {}, [], []
        for event in page.events:
            method, p = event.get("method"), event.get("params", {})
            if method == "Runtime.exceptionThrown":
                errors.append("exception: " + p["exceptionDetails"].get("exception", {}).get("description", "")[:300])
            elif method == "Runtime.consoleAPICalled" and p["type"] in ("error", "assert"):
                errors.append("console." + p["type"] + ": " + " ".join(str(a.get("value", a.get("description", "")))
                                                                      for a in p["args"])[:300])
            elif method == "Log.entryAdded" and p["entry"]["level"] == "error":
                errors.append("log: " + p["entry"]["text"][:200] + " " + p["entry"].get("url", ""))
            elif method == "Network.requestWillBeSent":
                u = p["request"]["url"]
                requests[p["requestId"]] = u
                if not u.startswith(("http://127.0.0.1", "http://localhost", "data:", "blob:", "about:", "edge:", "chrome:")):
                    external.append(u)
            elif method == "Network.responseReceived" and p["response"]["status"] >= 400:
                failed.append(f"{p['response']['status']} {p['response']['url']}")
            elif method == "Network.loadingFailed":
                failed.append(f"failed {requests.get(p['requestId'], '?')}: {p.get('errorText')}")
        print(f"requests: {len(requests)}; with errors: {len(failed)}; outside 127.0.0.1: {len(external)}")
        for line in dict.fromkeys(errors):
            print("  console:", line)
        for line in dict.fromkeys(failed):
            print("  request:", line)
        for line in dict.fromkeys(external):
            print("  external:", line)
        problems += list(dict.fromkeys(errors)) + list(dict.fromkeys(failed)) + [f"external {u}" for u in external]
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    print("PASS" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
