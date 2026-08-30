"""Merge LLM-related env vars from ../comm-assistant/.env into this project's .env.

Never prints secret values. Safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LLM_KEYS = (
    "LLM_CHAIN",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_FALLBACKS",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
)

_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _parse_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(raw)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def _format_value(value: str) -> str:
    if re.search(r"\s|#", value) or value == "":
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def merge_llm_env(
    *,
    dest: Path,
    source: Path,
    example: Path | None = None,
) -> tuple[int, list[str]]:
    """
    Update/create dest .env with LLM keys from source.
    Returns (updated_count, list of key names touched).
    """
    if not source.is_file():
        raise FileNotFoundError(f"Source .env not found: {source}")

    src_vals = _parse_env(source)
    incoming = {k: src_vals[k] for k in LLM_KEYS if src_vals.get(k)}
    if not incoming:
        return 0, []

    if dest.is_file():
        text = dest.read_text(encoding="utf-8")
        lines = text.splitlines()
    elif example and example.is_file():
        lines = example.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    present: set[str] = set()
    new_lines: list[str] = []
    updated: list[str] = []

    for line in lines:
        match = _LINE_RE.match(line)
        if not match:
            new_lines.append(line)
            continue
        key = match.group(1)
        if key in incoming:
            present.add(key)
            new_val = incoming[key]
            new_lines.append(f"{key}={_format_value(new_val)}")
            updated.append(key)
        else:
            new_lines.append(line)

    missing = [k for k in incoming if k not in present]
    if missing:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# Synced from ../comm-assistant/.env (LLM only)")
        for key in missing:
            new_lines.append(f"{key}={_format_value(incoming[key])}")
            updated.append(key)

    dest.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    # de-dupe while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for key in updated:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return len(ordered), ordered


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=root.parent / "comm-assistant" / ".env",
    )
    parser.add_argument("--dest", type=Path, default=root / ".env")
    parser.add_argument("--example", type=Path, default=root / ".env.example")
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"SKIP: no source at {args.source}")
        return 0

    count, keys = merge_llm_env(
        dest=args.dest, source=args.source, example=args.example
    )
    if count == 0:
        print("OK: no LLM keys found in source to merge")
        return 0
    print(f"OK: merged {count} LLM env key(s): {', '.join(keys)}")
    print(f"DEST: {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
