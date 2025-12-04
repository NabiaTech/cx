#!/usr/bin/env python3
"""
Codex Loki Shipper - Stream JSONL logs to Loki for federation monitoring
Usage: codex-loki-shipper.py [options] <jsonl-file>
Integrates with existing federation Loki infrastructure

Configuration via XDG-compliant TOML: ~/.config/nabi/cx/config.toml
"""

import os
import sys
import json
import time
import requests
import pathlib
import argparse
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone

# Import cx_config from same directory
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from cx_config import get_loki_config

def parse_jsonl_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a JSONL line safely"""
    try:
        return json.loads(line.strip())
    except json.JSONDecodeError:
        return None

def convert_to_loki_entry(record: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Convert JSONL record to Loki log entry format"""
    # Extract timestamp - Loki expects nanoseconds since epoch
    ts_str = record.get("ts", "")
    try:
        # Parse ISO timestamp and convert to nanoseconds
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        ts_nano = str(int(dt.timestamp() * 1_000_000_000))
    except:
        # Fallback to current time
        ts_nano = str(int(time.time() * 1_000_000_000))
    
    # Create labels for Loki indexing
    labels = {
        "job": config["job_name"],
        "instance": config["instance"],
        "federation_node": config["federation_node"],
        "session_id": record.get("session_id", "unknown"),
        "event_type": record.get("event", record.get("direction", "log"))
    }
    
    # Format labels for Loki
    label_str = "{" + ",".join([f'{k}="{v}"' for k, v in labels.items()]) + "}"
    
    # Create log line - strip sensitive data for federation sharing
    log_data = {
        "ts": record.get("ts"),
        "session_id": record.get("session_id"),
        "event": record.get("event"),
        "direction": record.get("direction"),
    }
    
    # Add specific fields based on event type
    if record.get("event") == "session_started":
        log_data.update({
            "cmd": record.get("cmd", []),
            "cwd": record.get("cwd")
        })
    elif record.get("event") == "session_ended":
        log_data.update({
            "exit_code": record.get("exit_code"),
            "total_bytes_in": record.get("total_bytes_in"),
            "total_bytes_out": record.get("total_bytes_out")
        })
    elif record.get("direction") in ["in", "out"]:
        log_data.update({
            "bytes": record.get("bytes"),
            "total_bytes_in": record.get("total_bytes_in"),
            "total_bytes_out": record.get("total_bytes_out")
        })
        # Don't include actual text content in federation logs for privacy
        # log_data["text_length"] = len(record.get("text", ""))
    
    return {
        "stream": labels,
        "values": [[ts_nano, json.dumps(log_data, separators=(',', ':'))]]
    }

def ship_to_loki(entries: List[Dict[str, Any]], config: Dict[str, Any]) -> bool:
    """Ship log entries to Loki"""
    if not entries:
        return True
    
    payload = {"streams": entries}
    loki_push_url = f"{config['loki_url']}/loki/api/v1/push"
    
    try:
        response = requests.post(
            loki_push_url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"Failed to ship to Loki: {e}", file=sys.stderr)
        return False

def process_jsonl_file(jsonl_path: pathlib.Path, config: Dict[str, Any], batch_size: int = 100) -> bool:
    """Process JSONL file and ship to Loki in batches"""
    if not jsonl_path.exists():
        print(f"Error: File {jsonl_path} does not exist", file=sys.stderr)
        return False
    
    print(f"Processing {jsonl_path} → Loki at {config['loki_url']}")
    
    entries = []
    processed = 0
    errors = 0
    
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            record = parse_jsonl_line(line)
            if record is None:
                print(f"Warning: Could not parse line {line_num}", file=sys.stderr)
                errors += 1
                continue
            
            entry = convert_to_loki_entry(record, config)
            entries.append(entry)
            processed += 1
            
            # Ship batch when full
            if len(entries) >= batch_size:
                if ship_to_loki(entries, config):
                    print(f"Shipped batch of {len(entries)} entries")
                else:
                    errors += len(entries)
                entries = []
    
    # Ship remaining entries
    if entries:
        if ship_to_loki(entries, config):
            print(f"Shipped final batch of {len(entries)} entries")
        else:
            errors += len(entries)
    
    print(f"Completed: {processed} processed, {errors} errors")
    return errors == 0

def main():
    parser = argparse.ArgumentParser(description="Ship Codex JSONL logs to Loki")
    parser.add_argument("jsonl_file", help="Path to JSONL log file")
    parser.add_argument("--loki-url", help="Loki URL override")
    parser.add_argument("--job", help="Job name override")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size for shipping")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without shipping")
    
    args = parser.parse_args()
    
    # Get configuration
    config = get_loki_config()
    if args.loki_url:
        config["loki_url"] = args.loki_url
    if args.job:
        config["job_name"] = args.job
    
    jsonl_path = pathlib.Path(args.jsonl_file)
    
    if args.dry_run:
        print("DRY RUN - Configuration:")
        print(json.dumps(config, indent=2))
        print(f"Would process: {jsonl_path}")
        return
    
    # Process and ship
    success = process_jsonl_file(jsonl_path, config, args.batch_size)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()