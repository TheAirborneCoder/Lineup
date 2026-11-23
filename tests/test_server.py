# SPDX-License-Identifier: GPL-3.0-or-later
"""The web server's routing and safety properties, without models: static allow-list, GET only, Host check."""
import http.client
import json
import threading
import unittest

import _setup
import server

SCRATCH = _setup.SCRATCH  # importing _setup puts engine/ on sys.path and uses throwaway folders


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Library:
            def serve(self, handler, route, arg):
                return handler.send({"route": route, "q": arg("q", "none")})
        cls.httpd = server.Server((server.HOST, 0), server.Handler)
        cls.httpd.models = cls.httpd.grooms = Library()
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, method="GET", host=None):
        connection = http.client.HTTPConnection(server.HOST, self.port, timeout=10)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"{server.HOST}:{self.port}")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), body

    def test_the_two_pages_and_their_files(self):
        for path in ("/", "/models", "/grooms", "/models.js", "/models.css", "/model-view.js", "/char-effects.js",
                     "/grooms.js", "/world.css", "/textures.css"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body, path)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff", path)
        self.assertIn(b"Lineup", self.get("/")[2])

    def test_mime_types(self):
        self.assertTrue(self.get("/")[1]["Content-Type"].startswith("text/html"))
        self.assertTrue(self.get("/models.js")[1]["Content-Type"].startswith("text/javascript"))
        self.assertTrue(self.get("/models.css")[1]["Content-Type"].startswith("text/css"))

    def test_only_listed_files_are_served(self):
        for path in ("/lineup.py", "/../lineup.py", "/%2e%2e/lineup.py", "/viewer/models.js", "/engine/paths.py",
                     "/models.html", "/LICENSE", "//etc/passwd", "/models.js/../paths.py"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_every_static_entry_exists_on_disk(self):
        for path, (name, _mime) in server.STATIC.items():
            self.assertTrue((server.VIEWER / name).is_file(), path)

    def test_info_names_the_app(self):
        status, _, body = self.get("/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["app"], "Lineup")

    def test_only_get_is_allowed(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            status, headers, _ = self.get("/", method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers.get("Allow"), "GET")

    def test_a_foreign_host_header_is_refused(self):
        for host in ("evil.example", "evil.example:80", "127.0.0.1.evil.example"):
            self.assertEqual(self.get("/api/info", host=host)[0], 403, host)
        self.assertEqual(self.get("/api/info", host=f"localhost:{self.port}")[0], 200)

    def test_api_routes_are_handed_to_the_libraries_with_their_query(self):
        status, _, body = self.get("/api/models/list?q=abc")
        self.assertEqual((status, json.loads(body)), (200, {"route": "list", "q": "abc"}))
        self.assertEqual(json.loads(self.get("/api/grooms/summary")[2])["route"], "summary")

    def test_unknown_routes_and_the_favicon(self):
        self.assertEqual(self.get("/api/nothing")[0], 404)
        self.assertEqual(self.get("/favicon.ico")[0], 204)

    def test_it_binds_to_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
