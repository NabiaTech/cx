#!/usr/bin/env python3
"""
Codex Gateway - Minimal local HTTP ingest service (self-hostable)
Accepts POST /ingest with JSON body: { "source": "codex", "events": [...] }
Writes received events to ~/.codexlogs/gateway/received-YYYYMMDD.jsonl

Configuration via XDG-compliant TOML: ~/.config/nabi/cx/config.toml
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import pathlib
import sys
from datetime import datetime
import argparse

# Import cx_config from same directory
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from cx_config import load_config

# Load configuration at module level
_config = load_config()


class IngestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        endpoint = _config["gateway"]["endpoint"]
        if self.path != endpoint:
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
        base_dir = pathlib.Path(_config["logging"]["base_dir"]).expanduser()
        base = base_dir / "gateway"
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
    parser.add_argument("--host", help=f"Bind host (default from config: {_config['gateway']['host']})")
    parser.add_argument("--port", type=int, help=f"Bind port (default from config: {_config['gateway']['port']})")
    args = parser.parse_args()

    # Use config values as defaults, CLI args override
    host = args.host or _config["gateway"]["host"]
    port = args.port or _config["gateway"]["port"]

    server = HTTPServer((host, port), IngestHandler)
    print(f"Codex Gateway listening on http://{host}:{port}{_config['gateway']['endpoint']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

