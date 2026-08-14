#!/usr/bin/env python3
"""Export all Hermes historical sessions to SGME L0 raw layer.

Hermes v0.x stores sessions as individual files in ./sessions/<session-id>.json
"""

import argparse
import os
import json
from pathlib import Path
from datetime import datetime, timezone

def main():
    parser = argparse.ArgumentParser(description="Export Hermes sessions to SGME L0")
    parser.add_argument("--hermes-sessions-dir", default=None, help="Hermes sessions directory path（默认 %LOCALAPPDATA%/hermes/sessions）")
    parser.add_argument("--sgme-root", default=".", help="SGME project root (where raw/sessions/ will be created)")
    args = parser.parse_args()

    hermes_sessions_dir = Path(args.hermes_sessions_dir or os.environ.get("LOCALAPPDATA", "") + "/hermes/sessions")
    sgme_root = Path(args.sgme_root)
    raw_sessions_dir = sgme_root / "raw" / "sessions"
    raw_sessions_dir.mkdir(parents=True, exist_ok=True)

    if not hermes_sessions_dir.exists():
        print(f"ERROR: Hermes sessions directory not found at {hermes_sessions_dir}")
        return 1

    exported = 0
    skipped = 0
    errors = 0

    # Iterate over all .json files in sessions directory
    for json_file in hermes_sessions_dir.glob("*.json"):
        session_id = json_file.stem
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"ERROR reading {json_file}: {e}")
            errors += 1
            continue

        # Extract messages
        messages = data.get("messages", [])
        if not messages:
            skipped += 1
            continue

        created_at = data.get("created_at")
        if not created_at:
            # Fallback to file modification time
            created_at = int(json_file.stat().st_mtime)

        # Build SGME L0 format
        content = ""
        has_content = False
        for msg in messages:
            role = msg.get("role", "user")
            content_row = msg.get("content", "")
            if not content_row or content_row.strip() == "":
                continue
            # Get timestamp
            ts = msg.get("timestamp") or created_at
            dt = datetime.fromtimestamp(float(ts) / 1000 if ts > 1e12 else float(ts), tz=timezone.utc)
            iso_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            content += f"# {iso_ts} {role}\n{content_row}\n\n"
            has_content = True

        if not has_content:
            skipped += 1
            continue

        # Write to SGME raw/sessions/
        out_path = raw_sessions_dir / f"{session_id}.md"
        # SGME L0 frontmatter
        start_dt = datetime.fromtimestamp(float(created_at) / 1000 if created_at > 1e12 else float(created_at), tz=timezone.utc)
        frontmatter = f"""---
format_version: 1
file_id: {session_id}
session_key: hermes-{session_id[:12]}
agent_id: default
source_type: session
started_at: {start_dt.isoformat().replace('+00:00', 'Z')}
---
"""
        full_content = frontmatter + content
        out_path.write_text(full_content, encoding="utf-8")
        exported += 1

    print(f"\n=== Export complete ===")
    print(f"Exported: {exported} sessions")
    print(f"Skipped empty: {skipped}")
    print(f"Errors: {errors}")
    print(f"\nNext steps:")
    print(f"1. Go to SGME project root and register all files in wiki.db")
    print(f"2. Trigger full refine: POST /v1/admin/refine/trigger with limit=10000")

    return 0

if __name__ == "__main__":
    exit(main())
