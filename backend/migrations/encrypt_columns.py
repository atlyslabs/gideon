"""
One-shot migration: encrypt designated columns in place.

Usage:
    DATABASE_URL=postgresql://... poetry run python migrations/encrypt_columns.py

The encryption key is fetched from GCP Secret Manager (GIDEON_ENCRYPTION_KEY)
via the project's config loader — the runner must be authenticated as a
principal allowed to read the secret (e.g. via `gcloud auth application-default
login` locally, or workload identity in-cluster).

Safe to re-run: rows that are already Fernet-encrypted ("gAAAAA...") are skipped.
Resumable: if interrupted, rerun picks up where it stopped for the current table.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import psycopg
from cryptography.fernet import Fernet

FERNET_PREFIX = "gAAAAA"
BATCH_SIZE = 500

# JSONB columns to convert to TEXT before encryption
JSONB_TO_TEXT = [
    ("messages", "tool_calls"),
    ("messages", "tool_results"),
    ("messages", "mentions"),
    ("activity_log", "metadata"),
    ("meeting_preps", "attendees"),
    ("todos", "delegated_to"),
]

# Columns to encrypt: (table, column, is_json)
# is_json=True means: read as JSONB/TEXT, json.dumps, then encrypt
ENCRYPT_COLUMNS: list[tuple[str, str, bool]] = [
    # messages
    ("messages", "content", False),
    ("messages", "thinking", False),
    ("messages", "feedback_text", False),
    ("messages", "tool_calls", True),
    ("messages", "tool_results", True),
    ("messages", "mentions", True),
    # conversations
    ("conversations", "summary", False),
    # activity_log
    ("activity_log", "title", False),
    ("activity_log", "summary", False),
    ("activity_log", "metadata", True),
    # activity_topics
    ("activity_topics", "title", False),
    ("activity_topics", "summary", False),
    # email_auto_drafts
    ("email_auto_drafts", "subject", False),
    ("email_auto_drafts", "sender_name", False),
    ("email_auto_drafts", "sender_email", False),
    ("email_auto_drafts", "email_body", False),
    ("email_auto_drafts", "draft_body", False),
    # meeting_preps
    ("meeting_preps", "title", False),
    ("meeting_preps", "summary", False),
    ("meeting_preps", "prep_content", False),
    ("meeting_preps", "attendees", True),
    # skills
    ("skills", "memory_content", False),
    # todos
    ("todos", "text", False),  # DB column is "text", ORM attr is "content"
    ("todos", "delegated_to", True),
    # todo_updates
    ("todo_updates", "author_name", False),
    ("todo_updates", "author_email", False),
    ("todo_updates", "content", False),
    # todo_suggestions
    ("todo_suggestions", "text", False),  # DB column is "text"
    ("todo_suggestions", "detail", False),
    # uploaded_files
    ("uploaded_files", "extracted_text", False),
    ("uploaded_files", "image_base64", False),
    # workflows
    ("workflows", "name", False),
    ("workflows", "description", False),
    ("workflows", "prompt", False),
    # workflow_runs
    ("workflow_runs", "output", False),
    ("workflow_runs", "error", False),
    # users
    ("users", "persona_text", False),
    ("users", "background_info", False),
    ("users", "linkedin_url", False),
    # trigger_conversations
    ("trigger_conversations", "source_email", False),
]


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(FERNET_PREFIX)


def alter_jsonb_to_text(conn: psycopg.Connection) -> None:
    """Convert JSONB columns to TEXT (idempotent)."""
    _log("=== Phase 1: Convert JSONB -> TEXT ===")
    for table, column in JSONB_TO_TEXT:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_name = %s AND column_name = %s
                """,
                (table, column),
            )
            row = cur.fetchone()
            if not row:
                _log(f"  skip {table}.{column} (column missing)")
                continue
            current_type = row[0]
            if current_type.lower() == "text":
                _log(f"  skip {table}.{column} (already TEXT)")
                continue
            _log(f"  ALTER {table}.{column}: {current_type} -> TEXT")
            cur.execute(
                f'ALTER TABLE {table} ALTER COLUMN "{column}" TYPE TEXT USING "{column}"::TEXT'
            )
        conn.commit()


def _serialize_for_encrypt(value: Any, is_json: bool) -> str:
    """Convert a row value to the plaintext string we'll feed to Fernet."""
    if is_json:
        import json
        if isinstance(value, str):
            # Already serialized (e.g. after JSONB -> TEXT conversion)
            return value
        return json.dumps(value)
    if not isinstance(value, str):
        return str(value)
    return value


def encrypt_table_column(
    conn: psycopg.Connection,
    fernet: Fernet,
    table: str,
    column: str,
    is_json: bool,
) -> None:
    """Encrypt one column in one table, batched."""
    quoted = f'"{column}"'
    # Count remaining plaintext rows
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) FROM {table}
            WHERE {quoted} IS NOT NULL
              AND {quoted}::text NOT LIKE %s
            """,
            (f"{FERNET_PREFIX}%",),
        )
        total = cur.fetchone()[0]

    if total == 0:
        _log(f"  {table}.{column}: already migrated")
        return

    _log(f"  {table}.{column}: {total} rows to encrypt")
    processed = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, {quoted} FROM {table}
                WHERE {quoted} IS NOT NULL
                  AND {quoted}::text NOT LIKE %s
                LIMIT %s
                """,
                (f"{FERNET_PREFIX}%", BATCH_SIZE),
            )
            rows = cur.fetchall()
        if not rows:
            break

        updates: list[tuple[str, Any]] = []
        for row_id, value in rows:
            if _is_encrypted(value):
                continue  # defensive: should not happen given WHERE clause
            plaintext = _serialize_for_encrypt(value, is_json)
            ciphertext = fernet.encrypt(plaintext.encode()).decode()
            updates.append((ciphertext, row_id))

        if updates:
            with conn.cursor() as cur:
                cur.executemany(
                    f"UPDATE {table} SET {quoted} = %s WHERE id = %s",
                    updates,
                )
            conn.commit()

        processed += len(rows)
        _log(f"    {table}.{column}: {processed}/{total}")

    _log(f"  {table}.{column}: done")


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1

    from config import ENCRYPTION_KEY
    key = ENCRYPTION_KEY
    if not key:
        print(
            "ERROR: ENCRYPTION_KEY unresolved — set ENV=production with GCP ADC "
            "for GIDEON_ENCRYPTION_KEY, or export ENCRYPTION_KEY for local runs",
            file=sys.stderr,
        )
        return 1

    fernet = Fernet(key.encode())

    _log(f"connecting to {db_url.split('@')[-1]}")
    with psycopg.connect(db_url) as conn:
        alter_jsonb_to_text(conn)
        _log("=== Phase 2: Encrypt column values ===")
        for table, column, is_json in ENCRYPT_COLUMNS:
            encrypt_table_column(conn, fernet, table, column, is_json)

    _log("all done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
