# cx

A lightweight adapter for OpenAI Codex CLI with structured logging, session management, and analytics.

## Install

```bash
# One-liner
curl -fsSL https://raw.githubusercontent.com/NabiaTech/cx/main/bootstrap.sh | bash

# Or clone
git clone https://github.com/NabiaTech/cx.git ~/.cx
~/.cx/bootstrap.sh
```

Requires: `git`, `python3`, and [codex CLI](https://github.com/openai/codex)

## Usage

```bash
# Interactive session with logging (default)
cx

# Specify model
cx -m gpt-4o
cx -m o1

# Resume previous session
cx resume           # Picker
cx resume --last    # Most recent

# Headless (non-interactive)
cx headless "explain this codebase"
cx headless -m o1 "refactor for performance"

# List sessions
cx sessions
cx sessions --today

# Analytics
cx analyze --today
cx analyze --last-week
cx rollup --days 7

# Cleanup old logs
cx cleanup              # 90 days default
cx cleanup --days 30
cx cleanup --dry-run
```

## What It Does

**Transparent logging** - Wraps codex with PTY-based I/O capture. Codex runs normally; cx logs everything to `~/.codexlogs/`.

**Structured JSONL** - Each session produces:
- `session-*.jsonl` - Timestamped events with hash chain
- `session-*.raw.txt` - Raw terminal bytes
- `session-*.meta.json` - Session metadata

**Analytics** - Daily/weekly rollups with token estimates and cost analysis.

**Federation** - Ship logs to Loki or any HTTP endpoint for centralized monitoring.

## Commands

| Command | Description |
|---------|-------------|
| `cx` | Interactive session with logging |
| `cx resume` | Resume previous session |
| `cx headless <prompt>` | Non-interactive execution |
| `cx sessions` | List logged sessions |
| `cx analyze` | Quick analytics (--today, --yesterday, --last-week) |
| `cx rollup` | Detailed usage reports |
| `cx cleanup` | Prune old logs |
| `cx status` | Show suite status |
| `cx ship` | Ship logs to Loki |
| `cx ship-generic` | Ship to HTTP endpoint |
| `cx gateway` | Run local HTTP ingest server |
| `cx tail-shipper` | Continuous log follower |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CX_INSTALL_DIR` | `~/.cx` | Installation location |
| `CODEX_LOGS_DIR` | `~/.codexlogs` | Log storage |
| `LOKI_URL` | `localhost:3100` | Loki endpoint |

## Architecture

```
~/.cx/
├── bin/cx                    # Main CLI
└── lib/
    ├── codex-tee-v2.py      # PTY logger
    ├── codex-daily-rollup.py # Analytics
    ├── codex-gateway.py     # HTTP ingest
    └── ...

~/.codexlogs/
└── YYYY/MM/DD/
    ├── session-*.jsonl      # Structured logs
    ├── session-*.raw.txt    # Raw capture
    └── session-*.meta.json  # Metadata
```

## How It Works

The `tee` logger (named after Unix `tee` - T-pipe splitter):

1. Forks a PTY (pseudo-terminal)
2. Runs `codex` in the child process
3. Intercepts all I/O bidirectionally
4. Passes everything through to your terminal (transparent)
5. Writes structured JSONL with timestamps and hash chains

Hash chains: Each JSONL record includes SHA-256 of the previous record for tamper detection.

## License

MIT

## Links

- [OpenAI Codex CLI](https://github.com/openai/codex)
- [NabiaTech](https://github.com/NabiaTech)
