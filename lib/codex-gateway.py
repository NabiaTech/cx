#!/usr/bin/env python3
"""
Codex Gateway - Minimal local HTTP ingest service (self-hostable)
Accepts POST /ingest with JSON body: { "source": "codex", "events": [...] }
Writes received events to ~/.codexlogs/gateway/received-YYYYMMDD.jsonl
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import pathlib
from datetime import datetime
import argparse


class IngestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/ingest":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''

        try:
            payload = json.loads(body.decode('utf-8'))
            events = payload.get("events", [])
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        # Append to date-stamped JSONL file
        base = pathlib.Path.home() / ".codexlogs" / "gateway"
        base.mkdir(parents=True, exist_ok=True)
        out_path = base / f"received-{datetime.utcnow().strftime('%Y%m%d')}.jsonl"
        with open(out_path, 'a', encoding='utf-8') as f:
            for evt in events:
                f.write(json.dumps(evt, ensure_ascii=False) + "\n")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # Quieter default logging
        return


def main():
    parser = argparse.ArgumentParser(description="Run a minimal Codex ingest server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default 8080)")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), IngestHandler)
    print(f"Codex Gateway listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

