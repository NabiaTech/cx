#!/usr/bin/env python3
"""
Codex Tail Shipper - Continuously follow JSONL logs and ship new events
Defaults to ~/.codexlogs, polls for changes, and posts to a generic HTTP endpoint.
Uses stdlib only (urllib), persists per-file offsets to resume across restarts.

Configuration via XDG-compliant TOML: ~/.config/nabi/cx/config.toml
"""

import os
import sys
import json
import time
import pathlib
import argparse
from typing import Dict, Any, List, Optional
from urllib import request, error

# Import cx_config from same directory
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from cx_config import load_config

# Load configuration at module level
_config = load_config()

# State file location derived from config
_base_dir = pathlib.Path(_config["logging"]["base_dir"]).expanduser()
STATE_FILE = _base_dir / ".tail_shipper_state.json"


def to_generic_event(record: Dict[str, Any], include_text: bool = False) -> Dict[str, Any]:
    evt: Dict[str, Any] = {
        "ts": record.get("ts"),
        "session_id": record.get("session_id"),
        "kind": record.get("event") or record.get("direction") or "log",
        "metadata": {}
    }
    if record.get("event") == "session_started":
        evt["metadata"].update({
            "cmd": record.get("cmd", []),
            "cwd": record.get("cwd")
        })
    elif record.get("event") == "session_ended":
        evt["metadata"].update({
            "exit_code": record.get("exit_code"),
            "total_bytes_in": record.get("total_bytes_in"),
            "total_bytes_out": record.get("total_bytes_out")
        })
    elif record.get("direction") in ("in", "out"):
        evt["metadata"].update({
            "direction": record.get("direction"),
            "bytes": record.get("bytes"),
            "total_bytes_in": record.get("total_bytes_in"),
            "total_bytes_out": record.get("total_bytes_out")
        })
        if include_text:
            evt["metadata"]["text"] = record.get("text")
    if "error" in record:
        evt["metadata"]["error"] = record.get("error")
        evt["metadata"]["error_type"] = record.get("error_type")
    return evt


def post_json(url: str, payload: Any, timeout: float = 10.0) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except error.URLError as e:
        print(f"POST failed: {e}", file=sys.stderr)
        return False


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"files": {}}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    tmp.replace(STATE_FILE)


def discover_jsonl(base: pathlib.Path) -> List[pathlib.Path]:
    return [p for p in base.rglob("*.jsonl") if p.is_file()]


def tail_file(path: pathlib.Path, offset: int) -> (int, List[Dict[str, Any]]):
    events: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(rec)
            new_offset = f.tell()
    except FileNotFoundError:
        return offset, []
    return new_offset, events


def main():
    # Get defaults from config
    tail_config = _config["tail_shipper"]
    gateway_config = _config["gateway"]
    default_endpoint = f"http://{gateway_config['host']}:{gateway_config['port']}{gateway_config['endpoint']}"

    parser = argparse.ArgumentParser(description="Tail Codex JSONL logs and ship to a generic endpoint")
    parser.add_argument("--endpoint", default=default_endpoint,
                        help=f"HTTP endpoint to POST events (default from config: {default_endpoint})")
    parser.add_argument("--base", default=_config["logging"]["base_dir"],
                        help=f"Logs base dir (default from config: {_config['logging']['base_dir']})")
    parser.add_argument("--interval", type=float, default=tail_config["poll_interval_seconds"],
                        help=f"Poll interval seconds (default from config: {tail_config['poll_interval_seconds']})")
    parser.add_argument("--batch-size", type=int, default=tail_config["batch_size"],
                        help=f"Batch size per POST (default from config: {tail_config['batch_size']})")
    parser.add_argument("--include-text", action="store_true", default=tail_config["include_text"],
                        help=f"Include raw text content (default from config: {tail_config['include_text']})")
    parser.add_argument("--from-beginning", action="store_true", help="Start at beginning (default: tail from end)")

    args = parser.parse_args()

    base = pathlib.Path(args.base).expanduser()
    state = load_state()
    files_state: Dict[str, Dict[str, Any]] = state.setdefault("files", {})

    # Initialize offsets for new files
    for p in discover_jsonl(base):
        key = str(p)
        if key not in files_state:
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                size = 0
            files_state[key] = {"offset": 0 if args.from_beginning else size, "mtime": p.stat().st_mtime if p.exists() else 0}

    print(f"Following JSONL logs under {base}; posting to {args.endpoint}")

    try:
        while True:
            # Rescan for new files
            for p in discover_jsonl(base):
                key = str(p)
                info = files_state.get(key)
                st = p.stat()
                if info is None:
                    files_state[key] = {"offset": 0 if args.from_beginning else st.st_size, "mtime": st.st_mtime}
                    continue
                # If file was truncated or rotated, reset offset
                if info["offset"] > st.st_size:
                    info["offset"] = 0
                info["mtime"] = st.st_mtime

            # Tail and ship
            batch: List[Dict[str, Any]] = []
            for key, info in list(files_state.items()):
                p = pathlib.Path(key)
                if not p.exists():
                    continue
                new_off, recs = tail_file(p, info["offset"])
                if recs:
                    for r in recs:
                        batch.append(to_generic_event(r, include_text=args.include_text))
                info["offset"] = new_off

            if batch:
                # Send in chunks
                for i in range(0, len(batch), args.batch_size):
                    payload = {"source": "codex", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "events": batch[i:i+args.batch_size]}
                    ok = post_json(args.endpoint, payload)
                    if ok:
                        print(f"Posted {len(batch[i:i+args.batch_size])} events")
                    else:
                        print("Failed to post events batch", file=sys.stderr)

            # Persist state periodically
            save_state(state)
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nExiting tail shipper...")
        save_state(state)


if __name__ == "__main__":
    main()

