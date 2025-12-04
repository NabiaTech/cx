#!/usr/bin/env python3
"""
cx Configuration Loader - XDG-compliant configuration management

Resolution order (first wins):
  1. Environment variables (emergency overrides)
  2. ~/.config/nabi/cx/config.toml (XDG canonical)
  3. ~/.memchain/loki.json (legacy federation bootstrap - deprecated)
  4. Hardcoded defaults (fallback)

Usage:
    from cx_config import load_config
    config = load_config()
    loki_url = config["loki"]["url"]
"""

import os
import json
import pathlib
from typing import Dict, Any, Optional

# Try tomllib (Python 3.11+) or fall back to tomli
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # Will use defaults only


def get_xdg_config_home() -> pathlib.Path:
    """Get XDG_CONFIG_HOME, defaulting to ~/.config"""
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()


def get_xdg_data_home() -> pathlib.Path:
    """Get XDG_DATA_HOME, defaulting to ~/.local/share"""
    return pathlib.Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()


def get_default_config() -> Dict[str, Any]:
    """Return hardcoded default configuration"""
    hostname = os.uname().nodename if hasattr(os, "uname") else "unknown"

    return {
        "logging": {
            "base_dir": str(pathlib.Path.home() / ".codexlogs"),
            "retention_days": 90,
        },
        "federation": {
            "enabled": True,
            "node_id": f"codex-{hostname}",
            "auto_ship_loki": False,
        },
        "loki": {
            "url": "http://localhost:3100",
            "job_name": "codex-sessions",
            "push_path": "/loki/api/v1/push",
            "timeout_seconds": 10,
            "batch_size": 100,
            "labels": {
                "component": "cx-wrapper",
                "layer": "federation",
            },
        },
        "gateway": {
            "host": "0.0.0.0",
            "port": 8080,
            "endpoint": "/ingest",
        },
        "tail_shipper": {
            "poll_interval_seconds": 2.0,
            "batch_size": 200,
            "include_text": False,
        },
        "nats": {
            "url": "nats://localhost:4222",
            "stream_name": "FEDERATION_EVENTS",
            "subject_prefix": "federation.events.codex",
        },
    }


def deep_merge(base: Dict, overlay: Dict) -> Dict:
    """Deep merge overlay into base, returning new dict"""
    result = base.copy()
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_toml_config() -> Optional[Dict[str, Any]]:
    """Load configuration from XDG config path"""
    if tomllib is None:
        return None

    config_path = get_xdg_config_home() / "nabi" / "cx" / "config.toml"
    if not config_path.exists():
        return None

    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"[cx_config] Warning: Failed to load {config_path}: {e}")
        return None


def load_memchain_config() -> Optional[Dict[str, Any]]:
    """Load legacy federation config from ~/.memchain/loki.json (deprecated)"""
    memchain_path = pathlib.Path.home() / ".memchain" / "loki.json"
    if not memchain_path.exists():
        return None

    # Emit deprecation warning
    xdg_path = get_xdg_config_home() / "nabi" / "cx" / "config.toml"
    print(f"[cx_config] DEPRECATION WARNING: ~/.memchain/loki.json is deprecated.", file=__import__("sys").stderr)
    print(f"[cx_config] Please migrate to: {xdg_path}", file=__import__("sys").stderr)
    print(f"[cx_config] Run: python3 -m cx_config --migrate  # to auto-migrate", file=__import__("sys").stderr)

    try:
        with open(memchain_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Extract codex-specific config and map to new schema
            codex_config = data.get("codex", {})
            if codex_config:
                return {
                    "loki": {
                        "url": codex_config.get("loki_url"),
                        "job_name": codex_config.get("job_name"),
                        "labels": codex_config.get("labels", {}),
                    },
                    "federation": {
                        "node_id": codex_config.get("federation_node"),
                    },
                }
    except Exception as e:
        print(f"[cx_config] Warning: Failed to load {memchain_path}: {e}")

    return None


def apply_env_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    """Apply environment variable overrides (highest priority)

    Only applies if env var is set AND non-empty.
    This allows unsetting via empty string while keeping TOML values.
    """

    # LOKI_URL -> loki.url
    if os.environ.get("LOKI_URL"):
        config["loki"]["url"] = os.environ["LOKI_URL"]

    # CX_FEDERATION_EVENTS -> federation.enabled
    if os.environ.get("CX_FEDERATION_EVENTS"):
        config["federation"]["enabled"] = os.environ["CX_FEDERATION_EVENTS"] == "1"

    # CX_LOKI_SHIP -> federation.auto_ship_loki
    if os.environ.get("CX_LOKI_SHIP"):
        config["federation"]["auto_ship_loki"] = os.environ["CX_LOKI_SHIP"] == "1"

    # FEDERATION_NODE_ID -> federation.node_id
    if os.environ.get("FEDERATION_NODE_ID"):
        config["federation"]["node_id"] = os.environ["FEDERATION_NODE_ID"]

    # CODEX_LOGS_DIR -> logging.base_dir
    if os.environ.get("CODEX_LOGS_DIR"):
        config["logging"]["base_dir"] = os.environ["CODEX_LOGS_DIR"]

    return config


def load_config() -> Dict[str, Any]:
    """
    Load cx configuration with proper precedence.

    Resolution order (first wins):
      1. Environment variables
      2. ~/.config/nabi/cx/config.toml (XDG canonical)
      3. ~/.memchain/loki.json (legacy - deprecated)
      4. Hardcoded defaults

    Returns:
        Complete configuration dictionary
    """
    # Start with defaults
    config = get_default_config()

    # Layer 3: Legacy memchain config (deprecated)
    memchain = load_memchain_config()
    if memchain:
        config = deep_merge(config, memchain)

    # Layer 2: XDG TOML config (canonical)
    toml_config = load_toml_config()
    if toml_config:
        config = deep_merge(config, toml_config)

    # Layer 1: Environment variable overrides (highest priority)
    config = apply_env_overrides(config)

    return config


def get_loki_config() -> Dict[str, Any]:
    """Get Loki-specific configuration (backward compatible API)"""
    config = load_config()
    return {
        "loki_url": config["loki"]["url"],
        "job_name": config["loki"]["job_name"],
        "instance": os.environ.get("HOSTNAME", "unknown"),
        "federation_node": config["federation"]["node_id"],
        "labels": config["loki"].get("labels", {}),
    }


def migrate_memchain_to_xdg() -> bool:
    """Migrate ~/.memchain/loki.json codex config to XDG TOML"""
    memchain_path = pathlib.Path.home() / ".memchain" / "loki.json"
    xdg_path = get_xdg_config_home() / "nabi" / "cx" / "config.toml"

    if not memchain_path.exists():
        print("[cx_config] No ~/.memchain/loki.json found - nothing to migrate")
        return False

    if xdg_path.exists():
        print(f"[cx_config] XDG config already exists at {xdg_path}")
        print("[cx_config] Manual merge may be needed - skipping auto-migration")
        return False

    try:
        with open(memchain_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            codex_config = data.get("codex", {})

        if not codex_config:
            print("[cx_config] No 'codex' section in memchain config")
            return False

        # Build TOML content
        toml_lines = [
            "# cx (Codex Suite) Configuration",
            "# Migrated from ~/.memchain/loki.json",
            f"# Migration date: {__import__('datetime').datetime.now().isoformat()}",
            "",
            "[loki]",
        ]

        if codex_config.get("loki_url"):
            toml_lines.append(f'url = "{codex_config["loki_url"]}"')
        if codex_config.get("job_name"):
            toml_lines.append(f'job_name = "{codex_config["job_name"]}"')

        if codex_config.get("labels"):
            toml_lines.append("")
            toml_lines.append("[loki.labels]")
            for k, v in codex_config["labels"].items():
                toml_lines.append(f'{k} = "{v}"')

        if codex_config.get("federation_node"):
            toml_lines.append("")
            toml_lines.append("[federation]")
            toml_lines.append(f'node_id = "{codex_config["federation_node"]}"')

        # Write XDG config
        xdg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(xdg_path, "w", encoding="utf-8") as f:
            f.write("\n".join(toml_lines) + "\n")

        print(f"[cx_config] Successfully migrated to {xdg_path}")
        print("[cx_config] You may now safely remove ~/.memchain/loki.json")
        return True

    except Exception as e:
        print(f"[cx_config] Migration failed: {e}")
        return False


if __name__ == "__main__":
    import argparse
    import json as json_mod

    parser = argparse.ArgumentParser(description="cx configuration utility")
    parser.add_argument("--migrate", action="store_true",
                        help="Migrate ~/.memchain/loki.json to XDG TOML")
    parser.add_argument("--show", action="store_true",
                        help="Show resolved configuration")
    args = parser.parse_args()

    if args.migrate:
        migrate_memchain_to_xdg()
    elif args.show or not any(vars(args).values()):
        # Default: show config
        config = load_config()
        print(json_mod.dumps(config, indent=2))
