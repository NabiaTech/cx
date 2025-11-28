#!/usr/bin/env python3
"""
Codex PTY Tee Logger - Structured JSONL logging with tamper-evident capabilities
Usage: codex-tee.py [codex-args...]
Logs both raw and structured data for comprehensive audit trail
"""

import os
import sys
import pty
import select
import uuid
import time
import json
import errno
import pathlib
import shutil
import hashlib
import signal
from typing import Dict, Any, Optional

def now_iso() -> str:
    """Generate current timestamp in ISO format with milliseconds"""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int((t%1)*1000):03d}"

def ensure_log_paths() -> tuple[str, pathlib.Path, pathlib.Path, pathlib.Path]:
    """Create organized log directory structure and return file paths"""
    base = pathlib.Path.home() / ".codexlogs" / time.strftime("%Y/%m/%d")
    base.mkdir(parents=True, exist_ok=True)
    
    # Generate unique session ID
    sid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    
    raw_path = base / f"session-{sid}.raw.txt"
    jsonl_path = base / f"session-{sid}.jsonl"
    meta_path = base / f"session-{sid}.meta.json"
    
    return sid, raw_path, jsonl_path, meta_path

def write_meta(meta_path: pathlib.Path, cmd: list[str], env_snapshot: Dict[str, str]) -> Dict[str, Any]:
    """Write session metadata file"""
    meta = {
        "session_id": meta_path.stem.split("session-")[-1].replace(".meta", ""),
        "started_at": now_iso(),
        "cmd": cmd,
        "cwd": os.getcwd(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "user": os.environ.get("USER", "unknown"),
        "codex_version": get_codex_version(),
        "env_whitelist": env_snapshot,
        "version": 2,
        "logger": "codex-tee-python",
        "pid": os.getpid()
    }
    
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    
    return meta

def get_codex_version() -> str:
    """Get codex version safely"""
    try:
        import subprocess
        result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except:
        return "unknown"

def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hash of data"""
    return hashlib.sha256(data).hexdigest()

def append_jsonl(path: pathlib.Path, obj: Dict[str, Any], prev_hash: Optional[str] = None) -> str:
    """Append JSONL record with optional hash chaining for tamper evidence"""
    if prev_hash:
        obj["prev_hash"] = prev_hash
    
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    line_bytes = line.encode("utf-8")
    
    with open(path, "ab") as f:
        f.write(line_bytes)
    
    # Return hash of this line for chaining
    return compute_sha256(line_bytes)

def append_raw(path: pathlib.Path, data: bytes) -> None:
    """Append raw data to transcript file"""
    with open(path, "ab") as f:
        f.write(data)

def signal_handler(signum, frame):
    """Handle interruption signals gracefully"""
    print(f"\n[codex-tee] Received signal {signum}, exiting gracefully...")
    sys.exit(0)

def main():
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Check if codex is available
    if shutil.which("codex") is None:
        print("error: 'codex' not found on PATH", file=sys.stderr)
        sys.exit(127)
    
    # Set up logging paths
    session_id, raw_path, jsonl_path, meta_path = ensure_log_paths()
    
    # Keep metadata lean - only essential environment variables
    env_keep = ["SHELL", "TERM", "LANG", "LC_ALL", "PATH", "HOME"]
    env_snapshot = {k: os.environ.get(k, "") for k in env_keep if k in os.environ}
    
    # Write session metadata
    meta = write_meta(meta_path, ["codex"] + sys.argv[1:], env_snapshot)
    
    print(f"Starting structured Codex session...", file=sys.stderr)
    print(f"Session ID: {session_id}", file=sys.stderr)
    print(f"JSONL log: {jsonl_path}", file=sys.stderr)
    print(f"Raw log: {raw_path}", file=sys.stderr)
    print(f"Metadata: {meta_path}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    
    # Initialize hash chain
    prev_hash = None
    
    # Log session start
    prev_hash = append_jsonl(jsonl_path, {
        "ts": now_iso(),
        "session_id": session_id,
        "event": "session_started",
        "cmd": meta["cmd"],
        "cwd": meta["cwd"]
    }, prev_hash)
    
    # Fork PTY process
    try:
        pid, master_fd = pty.fork()
    except OSError as e:
        print(f"error: Failed to fork PTY: {e}", file=sys.stderr)
        sys.exit(1)
    
    if pid == 0:
        # Child process: exec codex
        try:
            os.execvp("codex", ["codex"] + sys.argv[1:])
        except OSError as e:
            print(f"error: Failed to exec codex: {e}", file=sys.stderr)
            os._exit(127)
    
    # Parent process: handle I/O multiplexing
    bytes_in = 0
    bytes_out = 0
    
    try:
        # Set stdin to non-blocking mode for better responsiveness
        import fcntl
        stdin_flags = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, stdin_flags | os.O_NONBLOCK)
        
        while True:
            # Use blocking select for efficiency, but handle non-blocking I/O
            ready, _, _ = select.select([master_fd, sys.stdin.fileno()], [], [])
            
            if master_fd in ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError as e:
                    if e.errno == errno.EIO:
                        # PTY closed (normal termination)
                        break
                    raise
                
                if not data:
                    break
                
                # Output to user's terminal immediately
                os.write(sys.stdout.fileno(), data)
                sys.stdout.flush()
                
                # Log output data
                bytes_out += len(data)
                append_raw(raw_path, data)
                prev_hash = append_jsonl(jsonl_path, {
                    "ts": now_iso(),
                    "session_id": session_id,
                    "direction": "out",
                    "bytes": len(data),
                    "text": data.decode(errors="replace"),
                    "total_bytes_out": bytes_out
                }, prev_hash)
            
            if sys.stdin.fileno() in ready:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue  # Non-blocking read, no data available
                    break
                
                if not data:
                    # EOF from user (Ctrl+D)
                    os.close(master_fd)
                    break
                
                # Send to child process immediately
                try:
                    os.write(master_fd, data)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise
                
                # Log input data
                bytes_in += len(data)
                append_raw(raw_path, data)
                prev_hash = append_jsonl(jsonl_path, {
                    "ts": now_iso(),
                    "session_id": session_id,
                    "direction": "in",
                    "bytes": len(data),
                    "text": data.decode(errors="replace"),
                    "total_bytes_in": bytes_in
                }, prev_hash)
    
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        print("\n[codex-tee] Interrupted by user", file=sys.stderr)
    
    except Exception as e:
        # Log any unexpected errors
        prev_hash = append_jsonl(jsonl_path, {
            "ts": now_iso(),
            "session_id": session_id,
            "event": "error",
            "error": str(e),
            "error_type": type(e).__name__
        }, prev_hash)
        raise
    
    finally:
        # Close PTY and finalize logs
        try:
            os.close(master_fd)
        except:
            pass
        
        # Wait for child process to complete
        try:
            _, exit_status = os.waitpid(pid, 0)
            exit_code = os.WEXITSTATUS(exit_status)
        except:
            exit_code = -1
        
        # Log session end with final statistics
        end_time = now_iso()
        prev_hash = append_jsonl(jsonl_path, {
            "ts": end_time,
            "session_id": session_id,
            "event": "session_ended",
            "exit_code": exit_code,
            "total_bytes_in": bytes_in,
            "total_bytes_out": bytes_out,
            "duration_estimate": "calculated_by_consumer"
        }, prev_hash)
        
        # Update metadata with final information
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            
            meta.update({
                "ended_at": end_time,
                "exit_code": exit_code,
                "total_bytes_in": bytes_in,
                "total_bytes_out": bytes_out,
                "raw_log_size": raw_path.stat().st_size if raw_path.exists() else 0,
                "jsonl_log_size": jsonl_path.stat().st_size if jsonl_path.exists() else 0,
                "final_hash": prev_hash
            })
            
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        
        except Exception as e:
            print(f"[codex-tee] Warning: Failed to update metadata: {e}", file=sys.stderr)
        
        # Print final summary
        print("\n" + "=" * 60, file=sys.stderr)
        print(f"[codex-tee] Session completed:", file=sys.stderr)
        print(f"  Session ID: {session_id}", file=sys.stderr)
        print(f"  Exit code: {exit_code}", file=sys.stderr)
        print(f"  Data in: {bytes_in} bytes", file=sys.stderr)
        print(f"  Data out: {bytes_out} bytes", file=sys.stderr)
        print(f"  Raw log: {raw_path} ({raw_path.stat().st_size if raw_path.exists() else 0} bytes)", file=sys.stderr)
        print(f"  JSONL log: {jsonl_path} ({jsonl_path.stat().st_size if jsonl_path.exists() else 0} bytes)", file=sys.stderr)
        print(f"  Metadata: {meta_path}", file=sys.stderr)
        print(f"  Chain hash: {prev_hash[:16]}...", file=sys.stderr)

if __name__ == "__main__":
    main()