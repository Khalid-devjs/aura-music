"""Minimal authenticated file-drop server for the Aura Music bot.

The Kernel cloud VM (clean IP) downloads YouTube audio, then POSTs the
file to this server through the SSH reverse tunnel (VM localhost:9090 ->
our 127.0.0.1:9090). The bot picks files up from the drop dir.

Usage: python3 file_drop_server.py [port]
"""
import hashlib
import hmac
import os
import sys
import time
import http.server
import json

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9090
DROP_DIR = "/root/musicbot/kernel_drop"
TOKEN = os.environ.get("KERNEL_DROP_TOKEN", "")

os.makedirs(DROP_DIR, exist_ok=True)


class Handler(http.server.BaseHTTPRequestHandler):
    def _auth_ok(self) -> bool:
        auth = self.headers.get("X-Drop-Token", "")
        return hmac.compare_digest(auth, TOKEN)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "files": len(os.listdir(DROP_DIR))}).encode())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        if self.path != "/upload":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        fname = self.headers.get("X-File-Name", f"file_{int(time.time())}.bin")
        # sanitize filename
        fname = os.path.basename(fname).replace("/", "_")
        fpath = os.path.join(DROP_DIR, fname)
        try:
            with open(fpath, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "saved": fpath, "size": length}).encode())

    def log_message(self, fmt, *args):  # keep quiet
        sys.stderr.write("[file_drop] %s\n" % (fmt % args))


if __name__ == "__main__":
    if not TOKEN:
        print("KERNEL_DROP_TOKEN env var required", file=sys.stderr)
        sys.exit(1)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"File-drop listening on 127.0.0.1:{PORT}, drop dir {DROP_DIR}")
    server.serve_forever()
