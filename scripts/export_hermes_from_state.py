#!/usr/bin/env python3
"""Export all Hermes historical sessions from state.db to SGME L0 raw layer.

Hermes current schema:
- sessions table: id (session_id), started_at, title, ...
- messages table: id, session_id, role, content, timestamp, ...

Export to SGME raw/sessions/{session_id}.md with L0 format.
No LLM calls needed — pure format conversion.
"""

import argparse
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

def main():
    parser = argparse.ArgumentParser(description="Export Hermes sessions from state.db to SGME L0")
    parser.add_argument("--hermes-state-db", default=None, help="Hermes state database path（默认 %LOCALAPPDATA%/hermes/state.db）")
    parser.add_argument("--sgme-root", default=".", help="SGME project root")
    args = parser.parse_args()

    hermes_db_path = Path(args.hermes_state_db or os.environ.get("LOCALAPPDATA", "") + "/hermes/state.db")
    sgme_root = Path(args.sgme_root)
    raw_sessions_dir = sgme_root / "raw" / "sessions"
    raw_sessions_dir.mkdir(parents=True, exist_ok=True)

    if not hermes_db_path.exists():
        print(f"ERROR: Hermes state.db not found at {hermes_db_path}")
        return 1

    # Connect to Hermes DB
    conn = sqlite3.connect(hermes_db_path)
    cursor = conn.execute("""
        SELECT id, started_at, title FROM sessions
        ORDER BY started_at DESC
    """)

    exported = 0
    skipped = 0
    errors = 0

    for (session_id, started_at, title) in cursor.fetchall():
        # Get all messages in this session
        msg_cursor = conn.execute("""
            SELECT role, content, timestamp FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
        """, (session_id,))

        # Build SGME L0 format
        content = ""
        has_content = False
        for (role, content_row, ts) in msg_cursor.fetchall():
            if not content_row or content_row.strip() == "":
                continue
            # Convert timestamp to ISO 8601 UTC
            if ts:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                dt = datetime.fromtimestamp(started_at, tz=timezone.utc) if started_at else datetime.now(timezone.utc)
            iso_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            content += f"# {iso_ts} {role}\n{content_row}\n\n"
            has_content = True

        if not has_content:
            skipped += 1
            continue

        # Write to SGME raw/sessions/
        out_path = raw_sessions_dir / f"{session_id}.md"
        # SGME L0 frontmatter
        start_dt = datetime.fromtimestamp(started_at, tz=timezone.utc) if started_at else datetime.now(timezone.utc)
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

    conn.close()

    print(f"\n=== Export complete ===")
    print(f"Total sessions in Hermes DB: {exported + skipped}")
    print(f"Exported to SGME L0: {exported} sessions")
    print(f"Skipped empty: {skipped}")
    print(f"Errors: {errors}")
    print(f"\nNext steps:")
    print(f"1. Run register script to insert all new files into SGME wiki.db")
    print(f"2. Trigger full refine: POST /v1/admin/refine/trigger with limit=10000")

    return 0

if __name__ == "__main__":
    exit(main())