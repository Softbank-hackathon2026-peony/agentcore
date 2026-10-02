"""메모 API: 요청마다 메모를 돌려주는 가벼운 서버 (상태 없음)."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
GREETING = os.environ.get("GREETING", "hello")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"memo": GREETING, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
