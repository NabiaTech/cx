#!/usr/bin/env python3
"""
Codex Daily Rollup - Generate session summaries and cost estimates
Usage: codex-daily-rollup.py [options] [date]
Provides analytics for Codex usage (single day or multi-day ranges)
"""

import os
import sys
import json
import pathlib
import argparse
from datetime import datetime, date, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict, Counter
import sqlite3

class TokenEstimator:
    """Rough token estimation for cost tracking"""
    
    # Rough token/character ratios by model family
    TOKEN_RATIOS = {
        "gpt-4": 0.25,  # ~4 chars per token
        "gpt-3.5": 0.25,
        "claude": 0.24,  # Slightly more efficient
        "o1": 0.25,
        "o3": 0.25,
        "default": 0.25
    }
    
    # Rough cost per 1K tokens (input/output) - USD
    COSTS_PER_1K = {
        "gpt-4o": (0.005, 0.015),
        "gpt-4o-mini": (0.00015, 0.0006), 
        "gpt-4": (0.03, 0.06),
        "claude-3-opus": (0.015, 0.075),
        "claude-3-sonnet": (0.003, 0.015),
        "claude-3-haiku": (0.00025, 0.00125),
        "o1-preview": (0.015, 0.06),
        "o1-mini": (0.003, 0.012),
        "o3-mini": (0.003, 0.012),  # Estimated
        "default": (0.001, 0.003)  # Conservative fallback
    }
    
    def estimate_tokens(self, text: str, model: str = "default") -> int:
        """Estimate token count from text"""
        model_key = next((k for k in self.TOKEN_RATIOS if k in model.lower()), "default")
        ratio = self.TOKEN_RATIOS[model_key]
        return max(1, int(len(text) * ratio))
    
    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str = "default") -> float:
        """Estimate cost in USD"""
        model_key = next((k for k in self.COSTS_PER_1K if k in model.lower()), "default")
        input_cost, output_cost = self.COSTS_PER_1K[model_key]
        
        total_cost = (input_tokens / 1000.0 * input_cost) + (output_tokens / 1000.0 * output_cost)
        return round(total_cost, 6)

class SessionAnalyzer:
    """Analyze Codex session data"""
    
    def __init__(self):
        self.estimator = TokenEstimator()
        self.reset()
    
    def reset(self):
        self.sessions = {}
        self.daily_stats = {
            "total_sessions": 0,
            "successful_sessions": 0,
            "failed_sessions": 0,
            "total_duration_seconds": 0.0,
            "total_input_bytes": 0,
            "total_output_bytes": 0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "estimated_cost_usd": 0.0,
            "commands": Counter(),
            "models": Counter(),
            "exit_codes": Counter(),
            "hourly_distribution": Counter(),
            "session_lengths": []
        }
    
    def parse_jsonl_file(self, jsonl_path: pathlib.Path) -> bool:
        """Parse JSONL file and extract session data"""
        if not jsonl_path.exists():
            return False
        
        current_session = None
        
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                
                session_id = record.get("session_id")
                if not session_id:
                    continue
                
                if session_id not in self.sessions:
                    self.sessions[session_id] = {
                        "start_time": None,
                        "end_time": None,
                        "cmd": [],
                        "model": "unknown",
                        "exit_code": None,
                        "bytes_in": 0,
                        "bytes_out": 0,
                        "io_events": []
                    }
                
                session = self.sessions[session_id]
                
                if record.get("event") == "session_started":
                    session["start_time"] = record.get("ts")
                    session["cmd"] = record.get("cmd", [])
                    # Try to extract model from command
                    cmd = session["cmd"]
                    for i, arg in enumerate(cmd):
                        if arg in ["-m", "--model"] and i + 1 < len(cmd):
                            session["model"] = cmd[i + 1]
                            break
                
                elif record.get("event") == "session_ended":
                    session["end_time"] = record.get("ts")
                    session["exit_code"] = record.get("exit_code", -1)
                    session["bytes_in"] = record.get("total_bytes_in", 0)
                    session["bytes_out"] = record.get("total_bytes_out", 0)
                
                elif record.get("direction") in ["in", "out"]:
                    session["io_events"].append({
                        "direction": record["direction"],
                        "bytes": record.get("bytes", 0),
                        "text": record.get("text", "")
                    })
        
        return True
    
    def analyze_sessions(self) -> Dict[str, Any]:
        """Analyze all loaded sessions and generate statistics"""
        self.daily_stats["total_sessions"] = len(self.sessions)
        
        for session_id, session in self.sessions.items():
            # Session outcome
            exit_code = session.get("exit_code", -1)
            if exit_code == 0:
                self.daily_stats["successful_sessions"] += 1
            else:
                self.daily_stats["failed_sessions"] += 1
            
            self.daily_stats["exit_codes"][exit_code] += 1
            
            # Duration calculation
            start_time = session.get("start_time")
            end_time = session.get("end_time")
            duration = 0.0
            
            if start_time and end_time:
                try:
                    start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                    duration = (end_dt - start_dt).total_seconds()
                    
                    # Hourly distribution
                    hour = start_dt.hour
                    self.daily_stats["hourly_distribution"][hour] += 1
                    
                except:
                    pass
            
            self.daily_stats["total_duration_seconds"] += duration
            self.daily_stats["session_lengths"].append(duration)
            
            # Data volumes
            bytes_in = session.get("bytes_in", 0)
            bytes_out = session.get("bytes_out", 0)
            
            self.daily_stats["total_input_bytes"] += bytes_in
            self.daily_stats["total_output_bytes"] += bytes_out
            
            # Token estimation
            model = session.get("model", "default")
            self.daily_stats["models"][model] += 1
            
            # Estimate tokens from I/O events
            input_text = ""
            output_text = ""
            
            for event in session.get("io_events", []):
                if event["direction"] == "in":
                    input_text += event.get("text", "")
                else:
                    output_text += event.get("text", "")
            
            input_tokens = self.estimator.estimate_tokens(input_text, model)
            output_tokens = self.estimator.estimate_tokens(output_text, model)
            
            self.daily_stats["estimated_input_tokens"] += input_tokens
            self.daily_stats["estimated_output_tokens"] += output_tokens
            
            # Cost estimation
            cost = self.estimator.estimate_cost(input_tokens, output_tokens, model)
            self.daily_stats["estimated_cost_usd"] += cost
            
            # Command analysis
            cmd = session.get("cmd", [])
            if cmd:
                # Extract subcommand if present
                subcommand = "interactive"
                for arg in cmd[1:]:  # Skip 'codex'
                    if not arg.startswith("-"):
                        subcommand = arg
                        break
                self.daily_stats["commands"][subcommand] += 1
        
        # Calculate averages
        if self.daily_stats["total_sessions"] > 0:
            self.daily_stats["avg_session_duration"] = self.daily_stats["total_duration_seconds"] / self.daily_stats["total_sessions"]
            self.daily_stats["avg_cost_per_session"] = self.daily_stats["estimated_cost_usd"] / self.daily_stats["total_sessions"]
        else:
            self.daily_stats["avg_session_duration"] = 0.0
            self.daily_stats["avg_cost_per_session"] = 0.0
        
        return self.daily_stats

def find_log_files(log_date: date) -> List[pathlib.Path]:
    """Find all JSONL log files for a given date"""
    base_dir = pathlib.Path.home() / ".codexlogs"
    date_dir = base_dir / f"{log_date.year:04d}" / f"{log_date.month:02d}" / f"{log_date.day:02d}"
    
    if not date_dir.exists():
        return []
    
    return list(date_dir.glob("*.jsonl"))

def find_log_files_range(start_date: date, end_date: date) -> List[pathlib.Path]:
    """Find all JSONL log files in inclusive date range [start_date, end_date]."""
    files: List[pathlib.Path] = []
    current = start_date
    while current <= end_date:
        files.extend(find_log_files(current))
        current += timedelta(days=1)
    return files

def generate_report(stats: Dict[str, Any], report_date: Optional[date] = None, range_label: Optional[str] = None) -> str:
    """Generate human-readable report for a date or range."""
    title = (
        f"Codex Report - {report_date.strftime('%Y-%m-%d')}" if report_date
        else f"Codex Report - {range_label or 'range'}"
    )
    lines = [
        f"# {title}",
        "",
        "## Summary",
        f"- **Total Sessions**: {stats['total_sessions']}",
        f"- **Successful**: {stats['successful_sessions']} ({100*stats['successful_sessions']/max(1,stats['total_sessions']):.1f}%)",
        f"- **Failed**: {stats['failed_sessions']}",
        f"- **Total Duration**: {stats['total_duration_seconds']/3600:.2f} hours",
        f"- **Avg Session**: {stats['avg_session_duration']:.1f} seconds",
        "",
        "## Usage & Costs",
        f"- **Input**: {stats['total_input_bytes']:,} bytes (~{stats['estimated_input_tokens']:,} tokens)",
        f"- **Output**: {stats['total_output_bytes']:,} bytes (~{stats['estimated_output_tokens']:,} tokens)",
        f"- **Estimated Cost**: ${stats['estimated_cost_usd']:.4f}",
        f"- **Cost/Session**: ${stats['avg_cost_per_session']:.4f}",
        "",
        "## Commands Used"
    ]
    
    for cmd, count in stats['commands'].most_common():
        lines.append(f"- **{cmd}**: {count} sessions")
    
    lines.extend([
        "",
        "## Models Used"
    ])
    
    for model, count in stats['models'].most_common():
        lines.append(f"- **{model}**: {count} sessions")
    
    if stats['hourly_distribution']:
        lines.extend([
            "",
            "## Hourly Distribution"
        ])
        for hour in sorted(stats['hourly_distribution'].keys()):
            count = stats['hourly_distribution'][hour]
            bar = "█" * min(20, count)
            lines.append(f"- **{hour:02d}:00**: {count:2d} {bar}")
    
    return "\n".join(lines)

def save_to_federation(stats: Dict[str, Any], report_date: date) -> bool:
    """Save single-day rollup data to federation memchain if available"""
    federation_dir = pathlib.Path.home() / ".memchain"
    if not federation_dir.exists():
        return False
    
    rollup_file = federation_dir / f"codex-rollup-{report_date.strftime('%Y%m%d')}.json"
    
    try:
        with open(rollup_file, 'w') as f:
            json.dump({
                "date": report_date.isoformat(),
                "generated_at": datetime.now().isoformat(),
                "stats": stats
            }, f, indent=2)
        return True
    except:
        return False

def save_to_federation_range(stats: Dict[str, Any], start_date: date, end_date: date) -> bool:
    """Save multi-day rollup to federation with a range filename."""
    federation_dir = pathlib.Path.home() / ".memchain"
    if not federation_dir.exists():
        return False
    rollup_file = federation_dir / f"codex-rollup-{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.json"
    try:
        with open(rollup_file, 'w') as f:
            json.dump({
                "range": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat()
                },
                "generated_at": datetime.now().isoformat(),
                "stats": stats
            }, f, indent=2)
        return True
    except:
        return False

def main():
    parser = argparse.ArgumentParser(description="Generate Codex daily rollup")
    parser.add_argument("date", nargs="?", help="Date (YYYY-MM-DD), defaults to yesterday unless --days provided")
    parser.add_argument("--output", "-o", help="Output file for report")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--federation", action="store_true", help="Save to federation memchain")
    parser.add_argument("--days", type=int, help="Aggregate last N days ending today (e.g., 7)")
    
    args = parser.parse_args()
    
    # Determine scope: single date or multi-day range
    if args.days and args.days > 1:
        end_date = date.today()
        start_date = end_date - timedelta(days=args.days - 1)
        print(f"Generating rollup for {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        log_files = find_log_files_range(start_date, end_date)
        if not log_files:
            print(f"No log files found for range {start_date} to {end_date}")
            sys.exit(1)
        print(f"Found {len(log_files)} log files")
        analyzer = SessionAnalyzer()
        for log_file in log_files:
            print(f"Processing {log_file.name}")
            analyzer.parse_jsonl_file(log_file)
        stats = analyzer.analyze_sessions()
        if args.format == "json":
            output = json.dumps({
                "range": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat()
                },
                "stats": stats
            }, indent=2, default=str)
        else:
            output = generate_report(stats, None, f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        if args.output:
            with open(args.output, 'w') as f:
                f.write(output)
            print(f"Report saved to {args.output}")
        else:
            print(output)
        if args.federation:
            if save_to_federation_range(stats, start_date, end_date):
                print("Rollup saved to federation memchain (range)")
            else:
                print("Warning: Could not save to federation memchain", file=sys.stderr)
        return
    
    # Single-day flow
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date format: {args.date}. Use YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    else:
        target_date = date.today() - timedelta(days=1)
    print(f"Generating rollup for {target_date.strftime('%Y-%m-%d')}")
    log_files = find_log_files(target_date)
    if not log_files:
        print(f"No log files found for {target_date}")
        sys.exit(1)
    print(f"Found {len(log_files)} log files")
    analyzer = SessionAnalyzer()
    for log_file in log_files:
        print(f"Processing {log_file.name}")
        analyzer.parse_jsonl_file(log_file)
    stats = analyzer.analyze_sessions()
    if args.format == "json":
        output = json.dumps(stats, indent=2, default=str)
    else:
        output = generate_report(stats, target_date)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"Report saved to {args.output}")
    else:
        print(output)
    if args.federation:
        if save_to_federation(stats, target_date):
            print("Rollup saved to federation memchain")
        else:
            print("Warning: Could not save to federation memchain", file=sys.stderr)

if __name__ == "__main__":
    main()
