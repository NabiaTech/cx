#!/usr/bin/env python3
"""
Codex PTY Tee Logger V2 - Improved terminal handling with proper escape sequence support
Usage: codex-tee-v2.py [codex-args...]
Fixes cursor position queries and terminal control sequences
"""

import os
import sys
import pty
import tty
import termios
import select
import uuid
import time
import json
import errno
import pathlib
import shutil
import hashlib
import signal
import fcntl
import subprocess
from typing import Dict, Any, Optional, Tuple

# Federation dual-write configuration
FEDERATION_ENABLED = os.environ.get("CX_FEDERATION_EVENTS", "1") == "1"
LOKI_SHIP_ENABLED = os.environ.get("CX_LOKI_SHIP", "0") == "1"  # Auto-ship on session end

# Regex pattern for Codex native UUID (UUIDv7 format: 019adb8e-f58d-7c02-ac81-091803b2fe90)
import re
CODEX_UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

def now_iso() -> str:
    """Generate current timestamp in ISO format with milliseconds"""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int((t%1)*1000):03d}"


def extract_codex_uuid(args: list[str]) -> Optional[str]:
    """
    Extract Codex native session UUID from command arguments.

    Codex uses UUIDv7 for session IDs (e.g., 019adb8e-f58d-7c02-ac81-091803b2fe90).
    This allows correlating cx sessions with Codex's internal session continuity,
    especially important for resumed sessions.

    Returns the UUID if found in args, None otherwise.
    """
    for arg in args:
        if CODEX_UUID_PATTERN.match(arg):
            return arg
    return None


def detect_resume_mode(args: list[str]) -> Tuple[bool, Optional[str]]:
    """
    Detect if this is a resume operation and extract the Codex UUID if present.

    Returns:
        (is_resume, codex_uuid) tuple
    """
    is_resume = "resume" in args
    codex_uuid = extract_codex_uuid(args) if is_resume else None
    return is_resume, codex_uuid

def ensure_log_paths() -> Tuple[str, pathlib.Path, pathlib.Path, pathlib.Path]:
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
        "logger": "codex-tee-v2",
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
    
    return compute_sha256(line_bytes)

def append_raw(path: pathlib.Path, data: bytes) -> None:
    """Append raw data to transcript file"""
    with open(path, "ab") as f:
        f.write(data)


def publish_federation_event(
    event_type: str,
    session_id: str,
    message: str,
    metadata: Dict[str, Any],
    severity: str = "info"
) -> bool:
    """
    Publish event to NabiOS federation via nabi events CLI.
    Dual-write pattern: Local JSONL + Federation Events (NATS + federation JSONL).

    This enables:
    - nabi-tui visibility (Events Tab)
    - Cross-session awareness via SCL
    - Federation-wide observability
    """
    if not FEDERATION_ENABLED:
        return False

    # Check if nabi CLI is available
    nabi_path = shutil.which("nabi")
    if nabi_path is None:
        # Try common locations
        for path in ["~/.local/share/nabi/bin/nabi", "~/.local/bin/nabi"]:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                nabi_path = expanded
                break

    if nabi_path is None:
        return False

    try:
        # Add standard metadata
        metadata.update({
            "event_type": event_type,
            "logger": "cx-tee-v2",
            "state": "ready",  # Makes it visible in nabi-tui "Ready Docs" filter
        })

        cmd = [
            nabi_path, "events", "publish",
            "--source", "codex-session",
            "--severity", severity,
            "--message", message,
            "--metadata", json.dumps(metadata)
        ]

        # Run async (don't block the PTY loop)
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return True
    except Exception:
        return False


def ship_to_loki(jsonl_path: pathlib.Path) -> bool:
    """
    Ship session JSONL to Loki for Grafana visibility.
    Runs async after session ends.
    """
    if not LOKI_SHIP_ENABLED:
        return False

    cx_path = shutil.which("cx")
    if cx_path is None:
        cx_path = os.path.expanduser("~/.cx/bin/cx")

    if not os.path.exists(cx_path):
        return False

    try:
        subprocess.Popen(
            [cx_path, "ship", str(jsonl_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return True
    except Exception:
        return False

def signal_handler(signum, frame):
    """Handle interruption signals gracefully"""
    print(f"\n[codex-tee] Received signal {signum}, exiting gracefully...", file=sys.stderr)
    sys.exit(0)

def copy_terminal_size(src_fd: int, dst_fd: int) -> None:
    """Copy terminal window size from source to destination"""
    try:
        import struct
        import fcntl
        import termios
        
        # Get terminal size from source
        winsize = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b'\0' * 8)
        # Set terminal size on destination
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, winsize)
    except:
        pass  # Ignore errors if not a terminal

def handle_winch(signum, frame):
    """Handle window size changes"""
    global master_fd
    if 'master_fd' in globals():
        copy_terminal_size(sys.stdin.fileno(), master_fd)

def main():
    global master_fd
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGWINCH, handle_winch)  # Handle terminal resize
    
    # Check if codex is available
    if shutil.which("codex") is None:
        print("error: 'codex' not found on PATH", file=sys.stderr)
        sys.exit(127)

    # Detect resume mode and extract Codex native UUID for correlation
    is_resume, codex_uuid = detect_resume_mode(sys.argv[1:])

    # Set up logging paths
    session_id, raw_path, jsonl_path, meta_path = ensure_log_paths()

    # Keep metadata lean - only essential environment variables
    env_keep = ["SHELL", "TERM", "LANG", "LC_ALL", "PATH", "HOME"]
    env_snapshot = {k: os.environ.get(k, "") for k in env_keep if k in os.environ}

    # Write session metadata
    meta = write_meta(meta_path, ["codex"] + sys.argv[1:], env_snapshot)

    # Add Codex UUID to metadata if this is a resume
    if codex_uuid:
        meta["codex_session_uuid"] = codex_uuid
        meta["is_resume"] = True
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    
    print(f"Starting structured Codex session...", file=sys.stderr)
    print(f"Session ID: {session_id}", file=sys.stderr)
    if codex_uuid:
        print(f"Codex UUID: {codex_uuid} (resume)", file=sys.stderr)
    print(f"JSONL log: {jsonl_path}", file=sys.stderr)
    print(f"Raw log: {raw_path}", file=sys.stderr)
    print(f"Metadata: {meta_path}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Initialize hash chain
    prev_hash = None

    # Build session start record
    session_start_record = {
        "ts": now_iso(),
        "session_id": session_id,
        "event": "session_started",
        "cmd": meta["cmd"],
        "cwd": meta["cwd"]
    }
    if codex_uuid:
        session_start_record["codex_session_uuid"] = codex_uuid
        session_start_record["is_resume"] = True

    # Log session start
    prev_hash = append_jsonl(jsonl_path, session_start_record, prev_hash)

    # Build federation event metadata
    federation_metadata = {
        "session_id": session_id,
        "cwd": meta["cwd"],
        "hostname": meta["hostname"],
        "codex_version": meta["codex_version"],
        "jsonl_path": str(jsonl_path),
    }
    if codex_uuid:
        federation_metadata["codex_session_uuid"] = codex_uuid
        federation_metadata["is_resume"] = True

    # Dual-write: Publish to federation events for nabi-tui visibility
    message = f"Codex session {'resumed' if is_resume else 'started'}: {session_id}"
    if codex_uuid:
        message += f" (codex:{codex_uuid[:8]}...)"

    publish_federation_event(
        event_type="session_started",
        session_id=session_id,
        message=message,
        metadata=federation_metadata
    )

    # Save original terminal settings
    stdin_fd = sys.stdin.fileno()
    old_tty_settings = None
    stdin_isatty = os.isatty(stdin_fd)
    
    if stdin_isatty:
        old_tty_settings = termios.tcgetattr(stdin_fd)
    
    try:
        # Fork PTY process with proper terminal size
        pid, master_fd = pty.fork()
        
        if pid == 0:
            # Child process: exec codex
            # The child inherits the PTY as its controlling terminal
            try:
                os.execvp("codex", ["codex"] + sys.argv[1:])
            except OSError as e:
                print(f"error: Failed to exec codex: {e}", file=sys.stderr)
                os._exit(127)
        
        # Parent process: handle I/O multiplexing
        
        # Copy initial terminal size
        copy_terminal_size(stdin_fd, master_fd)
        
        # Set terminal to raw mode for proper escape sequence handling
        if stdin_isatty:
            tty.setraw(stdin_fd)
        
        # Make master_fd non-blocking for better responsiveness
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        
        bytes_in = 0
        bytes_out = 0
        
        while True:
            try:
                # Use select with no timeout for immediate response
                rlist, _, _ = select.select([master_fd, stdin_fd], [], [])
                
                if stdin_fd in rlist:
                    # Read from stdin and forward to PTY
                    try:
                        data = os.read(stdin_fd, 1024)
                        if not data:
                            break
                        
                        # Write to PTY master
                        os.write(master_fd, data)
                        
                        # Log input
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
                    except OSError:
                        break
                
                if master_fd in rlist:
                    # Read from PTY and forward to stdout
                    try:
                        data = os.read(master_fd, 65536)
                        if not data:
                            break
                        
                        # Write to stdout immediately
                        os.write(sys.stdout.fileno(), data)
                        sys.stdout.flush()
                        
                        # Log output
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
                    except OSError as e:
                        if e.errno in (errno.EIO, errno.EAGAIN, errno.EWOULDBLOCK):
                            # Check if child process has exited
                            try:
                                pid_result, status = os.waitpid(pid, os.WNOHANG)
                                if pid_result == pid:
                                    # Child has exited
                                    break
                            except:
                                pass
                            
                            if e.errno == errno.EIO:
                                break
                            # For EAGAIN/EWOULDBLOCK, continue
                            continue
                        else:
                            raise
            
            except KeyboardInterrupt:
                # Send Ctrl+C to the child process
                os.write(master_fd, b'\x03')
                continue
            except:
                break
    
    finally:
        # Restore original terminal settings
        if stdin_isatty and old_tty_settings:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty_settings)
        
        # Wait for child process to complete
        try:
            _, exit_status = os.waitpid(pid, 0)
            exit_code = os.WEXITSTATUS(exit_status) if os.WIFEXITED(exit_status) else -1
        except:
            exit_code = -1
        
        # Log session end
        # Build session end record
        end_time = now_iso()
        session_end_record = {
            "ts": end_time,
            "session_id": session_id,
            "event": "session_ended",
            "exit_code": exit_code,
            "total_bytes_in": bytes_in,
            "total_bytes_out": bytes_out,
            "duration_estimate": "calculated_by_consumer"
        }
        if codex_uuid:
            session_end_record["codex_session_uuid"] = codex_uuid

        prev_hash = append_jsonl(jsonl_path, session_end_record, prev_hash)

        # Update metadata
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

        # Build federation event metadata for session_ended
        end_federation_metadata = {
            "session_id": session_id,
            "exit_code": exit_code,
            "total_bytes_in": bytes_in,
            "total_bytes_out": bytes_out,
            "jsonl_path": str(jsonl_path),
            "final_hash": prev_hash[:16] if prev_hash else None,
        }
        if codex_uuid:
            end_federation_metadata["codex_session_uuid"] = codex_uuid

        # Dual-write: Publish session_ended to federation
        end_message = f"Codex session ended: {session_id} (exit={exit_code}, {bytes_out} bytes)"
        if codex_uuid:
            end_message += f" [codex:{codex_uuid[:8]}]"

        publish_federation_event(
            event_type="session_ended",
            session_id=session_id,
            message=end_message,
            metadata=end_federation_metadata
        )

        # Optional: Auto-ship to Loki if enabled
        if LOKI_SHIP_ENABLED:
            ship_to_loki(jsonl_path)

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
        if FEDERATION_ENABLED:
            print(f"  Federation: events published ✓", file=sys.stderr)

if __name__ == "__main__":
    main()