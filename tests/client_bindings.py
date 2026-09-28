"""Exercise Python argument conversion and HTTP calls in a worker thread.

Runs against a local stub; no OpenCode or Discord account is needed.
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from bot.oc import OpenCodeClient


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        self.respond({"data": [], "cursor": query})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, body))
        self.respond({"data": body})

    do_PATCH = do_POST

    def respond(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ClientBindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_arguments_from_worker_thread(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = OpenCodeClient(f"http://127.0.0.1:{server.server_port}", timeout=3)
        try:
            # An empty attachment list is what plain DMs pass to prompt().
            for files in (None, [], [{"url": "file:///tmp/image.png", "mime": "image/png"}]):
                result = await client.prompt("test", "hello", files=files, delivery="steer")
                self.assertEqual(result, {"text": "hello", "delivery": "steer", **({"files": files} if files else {})})
            self.assertEqual(await client.prompt("test", "hello"), {"text": "hello"})
            model = {"providerID": "test", "id": "model", "options": {"enabled": True, "n": 2}}
            self.assertEqual(await client.create_session(title="title", agent="build", model=model, directory="/tmp"),
                             {"title": "title", "agent": "build", "model": model, "location": {"directory": "/tmp"}})
            self.assertEqual(await client.create_session(), {})
            _, query = await client.list_sessions(cursor="next", directory="/tmp", search="hello world")
            self.assertEqual(query["search"], ["hello world"])
            self.assertEqual(query["cursor"], ["next"])
            self.assertEqual(query["directory"], ["/tmp"])
            await client.list_sessions()
            self.assertEqual(await client.update_session("test", title="renamed"), {"title": "renamed"})
            self.assertIsNone(await client.update_session("test"))
            self.assertEqual(await client.fork_session("test", "msg"), {"before": "msg"})
            self.assertEqual(await client.fork_session("test"), {"before": None})
            await client.set_model("test", "provider", "model", "high")
            self.assertEqual(server.requests[-1][1]["model"]["variant"], "high")
            await client.set_model("test", "provider", "model")
            self.assertNotIn("variant", server.requests[-1][1]["model"])
            self.assertEqual(await client.reply_permission("test", "req", "once", "approved"),
                             {"decision": "once", "message": "approved"})
            self.assertEqual(await client.reply_permission("test", "req", "once"), {"decision": "once"})
        finally:
            await client.close()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
