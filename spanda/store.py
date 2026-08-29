"""Stage 4 — SQLite storage.

Written one file at a time, never accumulating the codebase in memory. The
whole point of this layer is that a symbol keeps its identity across scans:
if a re-index mints fresh UUIDs, every symbol reads as removed-and-added and
the drift report — the only thing this project is ultimately for — becomes
noise. That identity lives in `symbol_key`, and everything else follows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import uuid as uuid_module
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    root_path           TEXT    NOT NULL,
    git_commit_hash     TEXT,
    git_dirty           INTEGER,
    total_files         INTEGER DEFAULT 0,
    parsed_files        INTEGER DEFAULT 0,
    unparseable_files   INTEGER DEFAULT 0,
    skipped_files       INTEGER DEFAULT 0,
    total_symbols       INTEGER DEFAULT 0,
    ambiguous_symbols   INTEGER DEFAULT 0,
    -- 0 until the run finishes. An interrupted scan must be visibly partial
    -- rather than quietly indistinguishable from a complete one.
    completed           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    scan_id             INTEGER NOT NULL,
    file_path           TEXT    NOT NULL,
    module              TEXT,
    file_hash           TEXT,
    parse_status        TEXT    NOT NULL,
    parse_error_line    INTEGER,
    parse_error_message TEXT,
    symbol_count        INTEGER DEFAULT 0,
    PRIMARY KEY (scan_id, file_path)
);

CREATE TABLE IF NOT EXISTS symbols (
    uuid                 TEXT    PRIMARY KEY,
    symbol_key           TEXT    NOT NULL UNIQUE,
    name                 TEXT    NOT NULL,
    qualname             TEXT    NOT NULL,
    kind                 TEXT    NOT NULL,
    module               TEXT,
    file_path            TEXT    NOT NULL,
    line_start           INTEGER,
    line_end             INTEGER,
    signature            TEXT,
    canonical_signature  TEXT,
    docstring            TEXT,
    decorators           TEXT,
    has_dynamic_dispatch INTEGER NOT NULL DEFAULT 0,
    -- >1 means the same name is defined more than once in one file, usually
    -- one branch per platform. Which one runs is not statically knowable, so
    -- the count is surfaced rather than a winner silently chosen.
    definition_count     INTEGER NOT NULL DEFAULT 1,
    signature_varies     INTEGER NOT NULL DEFAULT 0,
    content_hash         TEXT,
    signature_hash       TEXT,
    first_seen_scan_id   INTEGER NOT NULL,
    last_seen_scan_id    INTEGER NOT NULL,
    is_deleted           INTEGER NOT NULL DEFAULT 0
);

-- One row per scan in which a symbol's shape or content actually changed.
-- The symbols table holds only current values, so without this a second scan
-- overwrites the first and "did this signature change between scan 3 and scan
-- 7" — the question Stage 6 exists to answer — becomes unanswerable. Rows are
-- written only on an actual change, so an unchanged codebase adds nothing.
CREATE TABLE IF NOT EXISTS symbol_versions (
    symbol_uuid         TEXT    NOT NULL,
    scan_id             INTEGER NOT NULL,
    content_hash        TEXT,
    signature_hash      TEXT,
    canonical_signature TEXT,
    line_start          INTEGER,
    PRIMARY KEY (symbol_uuid, scan_id)
);

CREATE INDEX IF NOT EXISTS idx_versions_scan ON symbol_versions (scan_id);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols (file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols (name);
CREATE INDEX IF NOT EXISTS idx_symbols_last_seen ON symbols (last_seen_scan_id);
CREATE INDEX IF NOT EXISTS idx_symbols_module ON symbols (module);
"""

SYMBOL_FIELDS = [
    "name", "qualname", "kind", "module", "file_path", "line_start",
    "line_end", "signature", "canonical_signature", "docstring", "decorators",
    "has_dynamic_dispatch", "definition_count", "signature_varies",
    "content_hash", "signature_hash",
]


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def path_module(file_path: str) -> str:
    """Dotted form of a relative path: `app/models/order.py` -> `app.models.order`.

    Used for identity rather than the importable module name, because two
    scripts named `run.py` in different non-package directories share a module
    name but are not the same symbol.
    """
    module = file_path[:-3] if file_path.endswith(".py") else file_path
    module = module.replace("/", ".")
    return module[: -len(".__init__")] if module.endswith(".__init__") else module


def symbol_key(file_path: str, qualname: str, kind: str) -> str:
    """The stable identity of a symbol across scans.

    Deliberately excludes line numbers and content: a symbol that moves down
    the file or has its body rewritten is still the same symbol. It does
    include the file, so moving a function to another module reads as a
    removal plus an addition — accepted for v1, and documented.
    """
    return f"{path_module(file_path)}.{qualname}|{kind}"


def merge_duplicate_definitions(definitions: list[dict]) -> list[dict]:
    """Collapse definitions sharing one key into a single, honest row.

    A name defined several times in one file — one branch per platform, or a
    closure redefined in each arm of a conditional — cannot be split into
    stable identities: keying on line number would make every reordering look
    like a deletion. So they become one symbol carrying `definition_count`,
    and its content hash covers every variant, so editing any of them still
    registers as drift.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for definition in definitions:
        grouped.setdefault((definition["qualname"], definition["kind"]), []).append(definition)

    merged = []
    for group in grouped.values():
        first = dict(group[0])
        if len(group) > 1:
            first["lines"] = [min(d["lines"][0] for d in group),
                              max(d["lines"][1] for d in group)]
            combined = "|".join(d["content_hash"] for d in group)
            first["content_hash"] = "sha256:" + hashlib.sha256(
                combined.encode()).hexdigest()[:32]
            first["definition_count"] = len(group)
            first["signature_varies"] = len({d["signature_hash"] for d in group}) > 1
            first["docstring"] = next((d["docstring"] for d in group if d["docstring"]), None)
        else:
            first["definition_count"] = 1
            first["signature_varies"] = False
        merged.append(first)
    return merged


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git_state(root: Path) -> tuple[str | None, bool | None]:
    """Commit hash and whether the tree is dirty. Both None outside a repo."""
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(("git", "-C", str(root)) + args,
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    if commit is None:
        return None, None
    status = run("status", "--porcelain")
    return commit, bool(status) if status is not None else None


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------

class Index:
    """A SQLite-backed symbol index, written incrementally as files stream in."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- scans ------------------------------------------------------------
    def begin_scan(self, root: Path, skipped_files: int = 0) -> int:
        commit, dirty = git_state(root)
        cursor = self.connection.execute(
            "INSERT INTO scans (timestamp, root_path, git_commit_hash, git_dirty,"
            " skipped_files) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             str(root), commit, None if dirty is None else int(dirty), skipped_files))
        self.connection.commit()
        return cursor.lastrowid

    def finish_scan(self, scan_id: int) -> dict:
        totals = self.connection.execute(
            "SELECT COUNT(*) AS files,"
            "       SUM(parse_status = 'ok') AS parsed,"
            "       SUM(parse_status != 'ok') AS unparseable,"
            "       COALESCE(SUM(symbol_count), 0) AS symbols"
            " FROM files WHERE scan_id = ?", (scan_id,)).fetchone()
        ambiguous = self.connection.execute(
            "SELECT COUNT(*) FROM symbols"
            " WHERE last_seen_scan_id = ? AND definition_count > 1",
            (scan_id,)).fetchone()[0]
        self.connection.execute(
            "UPDATE scans SET total_files = ?, parsed_files = ?,"
            " unparseable_files = ?, total_symbols = ?, ambiguous_symbols = ?,"
            " completed = 1 WHERE scan_id = ?",
            (totals["files"], totals["parsed"] or 0, totals["unparseable"] or 0,
             totals["symbols"], ambiguous, scan_id))
        self.connection.commit()
        return dict(self.scan(scan_id))

    def scan(self, scan_id: int) -> sqlite3.Row:
        return self.connection.execute(
            "SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()

    def scans(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM scans ORDER BY scan_id").fetchall()

    # -- writing ----------------------------------------------------------
    def write_record(self, scan_id: int, record: dict, patterns: list[str]) -> int:
        """Store one file's worth of extraction. Returns symbols written."""
        from spanda.gaps import is_dynamic_dispatch

        definitions = merge_duplicate_definitions(record["definitions"])
        error = record.get("parse_error") or {}

        self.connection.execute(
            "INSERT OR REPLACE INTO files (scan_id, file_path, module, file_hash,"
            " parse_status, parse_error_line, parse_error_message, symbol_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, record["file"], record["module"], record["file_hash"],
             record["parse_status"], error.get("line"), error.get("message"),
             len(definitions)))

        for definition in definitions:
            key = symbol_key(record["file"], definition["qualname"], definition["kind"])
            dynamic = any(is_dynamic_dispatch(d["base"], patterns)
                          for d in definition["decorators"])
            values = (
                definition["name"], definition["qualname"], definition["kind"],
                record["module"], record["file"], definition["lines"][0],
                definition["lines"][1],
                json.dumps(definition["signature"]) if definition["signature"] else None,
                definition["canonical_signature"], definition["docstring"],
                json.dumps(definition["decorators"]), int(dynamic),
                definition["definition_count"], int(definition["signature_varies"]),
                definition["content_hash"], definition["signature_hash"],
            )

            existing = self.connection.execute(
                "SELECT uuid, content_hash, signature_hash FROM symbols"
                " WHERE symbol_key = ?", (key,)).fetchone()
            if existing:
                # Same symbol, seen again. Its UUID must not change, or every
                # scan would read as a wholesale replacement of the codebase.
                symbol_uuid = existing["uuid"]
                changed = (existing["content_hash"] != definition["content_hash"]
                           or existing["signature_hash"] != definition["signature_hash"])
                assignments = ", ".join(f"{f} = ?" for f in SYMBOL_FIELDS)
                self.connection.execute(
                    f"UPDATE symbols SET {assignments}, last_seen_scan_id = ?,"
                    " is_deleted = 0 WHERE uuid = ?",
                    values + (scan_id, symbol_uuid))
            else:
                symbol_uuid = uuid_module.uuid4().hex
                changed = True
                columns = ", ".join(SYMBOL_FIELDS)
                placeholders = ", ".join("?" * len(SYMBOL_FIELDS))
                self.connection.execute(
                    f"INSERT INTO symbols (uuid, symbol_key, {columns},"
                    " first_seen_scan_id, last_seen_scan_id)"
                    f" VALUES (?, ?, {placeholders}, ?, ?)",
                    (symbol_uuid, key) + values + (scan_id, scan_id))

            if changed:
                self.connection.execute(
                    "INSERT OR REPLACE INTO symbol_versions (symbol_uuid, scan_id,"
                    " content_hash, signature_hash, canonical_signature, line_start)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol_uuid, scan_id, definition["content_hash"],
                     definition["signature_hash"], definition["canonical_signature"],
                     definition["lines"][0]))

        return len(definitions)

    def commit(self) -> None:
        self.connection.commit()

    # -- queries ----------------------------------------------------------
    def symbol_by_key(self, key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM symbols WHERE symbol_key = ?", (key,)).fetchone()

    def symbols_in_file(self, file_path: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM symbols WHERE file_path = ? ORDER BY line_start",
            (file_path,)).fetchall()

    def find(self, pattern: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM symbols WHERE name LIKE ? OR qualname LIKE ?"
            " ORDER BY file_path, line_start LIMIT ?",
            (pattern, pattern, limit)).fetchall()

    def version_at(self, symbol_uuid: str, scan_id: int) -> sqlite3.Row | None:
        """What a symbol looked like as of a given scan.

        The most recent recorded version at or before that scan: a symbol that
        did not change between scans has no row for the later one, and its
        earlier row is still what was true.
        """
        return self.connection.execute(
            "SELECT * FROM symbol_versions WHERE symbol_uuid = ? AND scan_id <= ?"
            " ORDER BY scan_id DESC LIMIT 1", (symbol_uuid, scan_id)).fetchone()

    def missing_since(self, scan_id: int) -> list[sqlite3.Row]:
        """Symbols the index knows about that this scan did not see."""
        return self.connection.execute(
            "SELECT * FROM symbols WHERE last_seen_scan_id < ? ORDER BY file_path",
            (scan_id,)).fetchall()
