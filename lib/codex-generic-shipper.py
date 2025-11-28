#!/usr/bin/env python3
"""
Codex Generic Shipper - Send JSONL logs to a generic HTTP endpoint
Usage: codex-generic-shipper.py [options] <jsonl-file>
No platform-specific assumptions; uses stdlib HTTP client.
"""

import sys
import os
import json
import pathlib
import argparse
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib import request, error


def parse_jsonl(path: pathlib.Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def to_generic_event(record: Dict[str, Any], include_text: bool = False) -> Dict[str, Any]:
    evt: Dict[str, Any] = {
        "ts": record.get("ts"),
        "session_id": record.get("session_id"),
        "kind": record.get("event") or record.get("direction") or "log",
        "metadata": {}
    }
    # Include common metadata without sensitive content by default
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


def main():
    parser = argparse.ArgumentParser(description="Ship Codex JSONL logs to a generic endpoint")
    parser.add_argument("jsonl_file", help="Path to JSONL log file")
    parser.add_argument("--endpoint", required=True, help="HTTP endpoint to POST events (e.g., http://localhost:8080/ingest)")
    parser.add_argument("--batch-size", type=int, default=200, help="Batch size (default 200)")
    parser.add_argument("--include-text", action="store_true", help="Include raw text content (default false)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without posting")

    args = parser.parse_args()

    path = pathlib.Path(args.jsonl_file)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading {path}")
    records = parse_jsonl(path)
    print(f"Parsed {len(records)} records")

    # Convert to generic events
    events = [to_generic_event(r, include_text=args.include_text) for r in records]

    # Ship in batches
    sent = 0
    failed = 0
    for i in range(0, len(events), args.batch_size):
        batch = events[i:i + args.batch_size]
        payload = {"source": "codex", "generated_at": datetime.utcnow().isoformat() + "Z", "events": batch}
        if args.dry_run:
            print(f"DRY RUN: would POST {len(batch)} events to {args.endpoint}")
            continue
        ok = post_json(args.endpoint, payload)
        if ok:
            print(f"Posted batch of {len(batch)} events")
            sent += len(batch)
        else:
            print(f"Failed to post batch of {len(batch)} events", file=sys.stderr)
            failed += len(batch)

    print(f"Completed: {sent} sent, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

