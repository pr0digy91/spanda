"""Stage 4 — SQLite storage.

Written one file at a time, never accumulating the codebase in memory. The
point of this layer is that a symbol keeps its identity across scans: if a
re-index mints fresh UUIDs, every symbol reads as removed-and-added and the
drift report — the only thing this project is ultimately for — becomes noise.
That identity lives in `symbol_key`, and everything else follows.

The index lives inside the codebase it describes, at `.spanda/YYYY-MM-DD.db`,
so a database and the code it indexes cannot drift apart or be mixed up.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
import uuid as uuid_module
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 5
INDEX_DIRNAME = ".spanda"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    scan_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    root_path           TEXT    NOT NULL,
    -- Which interpreter read the code. A file that failed to parse because
    -- the interpreter predates its syntax is a different problem from a file
    -- with a real syntax error, and this is the only way to tell them apart.
    python_version      TEXT,
    -- Set only when the working tree is clean. A dirty tree is NOT the commit
    -- it sits on: recording the hash anyway would let a scan of uncommitted
    -- work masquerade as a scan of that commit, and any later comparison
    -- across the two would be nonsense.
    git_commit_hash     TEXT,
    git_base_commit     TEXT,
    git_dirty           INTEGER,
    -- Fingerprint of every file hash in the scan. Two scans with the same
    -- value indexed byte-identical code, whatever git thinks.
    content_fingerprint TEXT,
    total_files         INTEGER DEFAULT 0,
    parsed_files        INTEGER DEFAULT 0,
    unparseable_files   INTEGER DEFAULT 0,
    skipped_files       INTEGER DEFAULT 0,
    total_symbols       INTEGER DEFAULT 0,
    ambiguous_symbols   INTEGER DEFAULT 0,
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
    last_seen_scan_id    INTEGER NOT NULL
);

-- Contiguous runs of scans in which a symbol was present. `first_seen` and
-- `last_seen` cannot answer "was it there at scan 4?" on their own: a symbol
-- deleted at scan 3 and restored at scan 5 has first_seen 1 and last_seen 5,
-- which wrongly implies it existed throughout. One row per unbroken run, so a
-- symbol that never disappears costs exactly one row for its whole life.
CREATE TABLE IF NOT EXISTS symbol_spans (
    symbol_uuid TEXT    NOT NULL,
    from_scan   INTEGER NOT NULL,
    to_scan     INTEGER NOT NULL,
    PRIMARY KEY (symbol_uuid, from_scan)
);

CREATE INDEX IF NOT EXISTS idx_spans_range ON symbol_spans (from_scan, to_scan);
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


class IndexError_(Exception):
    """Refusal to operate on an index that cannot be trusted."""


# --------------------------------------------------------------------------
# where the index lives
# --------------------------------------------------------------------------

def index_dir(root: Path) -> Path:
    """`.spanda/` inside the codebase being indexed.

    Co-locating the index with its code is the structural fix for indexing two
    different projects into one database: there is no shared default path to
    collide on.
    """
    return Path(root).resolve() / INDEX_DIRNAME


#: One authoritative index per codebase. Not one file per run: the `scans`
#: table already records when each run happened, against which commit, with
#: what content fingerprint — a richer history than filenames could carry, and
#: a queryable one. Several files would instead give several things each
#: claiming to describe the repository, with UUIDs and version history trapped
#: inside whichever one you happened to open.
DB_FILENAME = "index.db"

GITIGNORE_BODY = """# Spanda index files.
#
# Derived data: everything here is rebuildable from the source it describes,
# and the files are large and binary. Ignoring the whole directory, including
# this file, keeps generated indexes out of the repository's history.
*
"""


def db_path(root: Path) -> Path:
    """The one index for this codebase."""
    return index_dir(root) / DB_FILENAME


def ensure_index_dir(root: Path) -> Path:
    """Create `.spanda/`, self-ignoring so indexes never reach a commit."""
    directory = index_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_BODY)
    return directory


def prepare_db_path(root: Path) -> Path:
    """The index path for this codebase, with `.spanda/` created if needed."""
    ensure_index_dir(root)
    return db_path(root)


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
    """A SQLite-backed symbol index, written incrementally as files stream in.

    A scan is one transaction. An interrupted run leaves the index exactly as
    it was, rather than half-written — a half-written scan looks identical to
    a mass deletion, and would make a drift report confidently wrong.
    """

    def __init__(self, path: Path, codebase_root: Path | None = None) -> None:
        self.path = Path(path)
        fresh = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Wait rather than fail instantly if another run holds the write lock.
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self._open_scan: int | None = None

        if fresh:
            self.connection.executescript(SCHEMA)
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            if codebase_root is not None:
                self._set_meta("codebase_root", str(Path(codebase_root).resolve()))
        else:
            self._check_schema_version()
            self.connection.executescript(SCHEMA)  # additive only
            if codebase_root is not None:
                self._check_codebase(codebase_root)

    # -- guards -----------------------------------------------------------
    def _set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def meta(self, key: str) -> str | None:
        try:
            row = self.connection.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row["value"] if row else None

    def _check_schema_version(self) -> None:
        stored = self.meta("schema_version")
        if stored is None:
            raise IndexError_(
                f"{self.path} predates schema versioning and cannot be read "
                f"safely. Delete it and re-index.")
        if int(stored) != SCHEMA_VERSION:
            direction = "newer than" if int(stored) > SCHEMA_VERSION else "older than"
            raise IndexError_(
                f"{self.path} has schema version {stored}, {direction} this "
                f"build's version {SCHEMA_VERSION}. There are no migrations "
                f"yet: delete the file and re-index.")

    def _check_codebase(self, root: Path) -> None:
        stored = self.meta("codebase_root")
        resolved = str(Path(root).resolve())
        if stored is None:
            self._set_meta("codebase_root", resolved)
            return
        if stored != resolved:
            raise IndexError_(
                f"{self.path} indexes {stored}, not {resolved}. Indexing a "
                f"second codebase into it would report the first one's symbols "
                f"as deleted. Use a separate index.")

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        if self._open_scan is not None:
            self.abort_scan()
        self.connection.close()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, exc_type, *_) -> None:
        if exc_type is not None and self._open_scan is not None:
            self.abort_scan()
        self.close()

    # -- scans ------------------------------------------------------------
    def previous_scan_id(self, scan_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT MAX(scan_id) AS previous FROM scans WHERE scan_id < ?",
            (scan_id,)).fetchone()
        return row["previous"]

    def begin_scan(self, root: Path, skipped_files: int = 0,
                   record_root: Path | None = None,
                   commit_override: str | None = None,
                   dirty_override: bool | None = None,
                   timestamp_override: str | None = None) -> int:
        """Open a scan. `record_root` and the overrides exist for backfill,
        which extracts from a throwaway git worktree but must record the scan
        against the real repository, the commit it checked out, and — since
        the scan describes code as it was — that commit's own date rather than
        the moment the backfill happened to run."""
        if commit_override is not None:
            commit, dirty = commit_override, bool(dirty_override)
        else:
            commit, dirty = git_state(root)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower():
                raise IndexError_(
                    f"another index run is writing to {self.path}. "
                    f"Wait for it to finish rather than running two at once — "
                    f"there is one index per codebase by design.") from error
            raise
        self._open_scan = None
        cursor = self.connection.execute(
            "INSERT INTO scans (timestamp, root_path, python_version,"
            " git_commit_hash, git_base_commit, git_dirty, skipped_files)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (timestamp_override
             or datetime.now(timezone.utc).isoformat(timespec="seconds"),
             str(Path(record_root or root).resolve()),
             platform.python_version(),
             # A dirty tree is not the commit it sits on.
             None if dirty else commit,
             commit,
             None if dirty is None else int(dirty),
             skipped_files))
        self._open_scan = cursor.lastrowid
        return self._open_scan

    def abort_scan(self) -> None:
        """Discard an in-progress scan entirely."""
        self.connection.execute("ROLLBACK")
        self._open_scan = None

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

        digest = hashlib.sha256()
        for row in self.connection.execute(
                "SELECT file_path, file_hash FROM files WHERE scan_id = ?"
                " ORDER BY file_path", (scan_id,)):
            digest.update(f"{row['file_path']}:{row['file_hash']}\n".encode())

        self.connection.execute(
            "UPDATE scans SET total_files = ?, parsed_files = ?,"
            " unparseable_files = ?, total_symbols = ?, ambiguous_symbols = ?,"
            " content_fingerprint = ?, completed = 1 WHERE scan_id = ?",
            (totals["files"], totals["parsed"] or 0, totals["unparseable"] or 0,
             totals["symbols"], ambiguous, "sha256:" + digest.hexdigest()[:32],
             scan_id))
        self.connection.execute("COMMIT")
        self._open_scan = None
        return dict(self.scan(scan_id))

    def scan(self, scan_id: int) -> sqlite3.Row:
        return self.connection.execute(
            "SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()

    def scans(self, complete_only: bool = False) -> list[sqlite3.Row]:
        clause = " WHERE completed = 1" if complete_only else ""
        return self.connection.execute(
            f"SELECT * FROM scans{clause} ORDER BY scan_id").fetchall()

    def require_complete(self, scan_id: int) -> sqlite3.Row:
        """Fetch a scan, refusing one that never finished.

        An interrupted scan saw only part of the codebase, so everything it
        did not reach looks deleted. Comparing against one would produce a
        confidently wrong answer, which is worse than no answer.
        """
        row = self.scan(scan_id)
        if row is None:
            raise IndexError_(f"no scan {scan_id} in {self.path}")
        if not row["completed"]:
            raise IndexError_(
                f"scan {scan_id} never completed — it saw only part of the "
                f"codebase, so everything it did not reach would read as "
                f"deleted. Re-run the index instead of comparing against it.")
        return row

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
                    f"UPDATE symbols SET {assignments}, last_seen_scan_id = ?"
                    " WHERE uuid = ?", values + (scan_id, symbol_uuid))
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

            self._record_presence(symbol_uuid, scan_id)

            if changed:
                self.connection.execute(
                    "INSERT OR REPLACE INTO symbol_versions (symbol_uuid, scan_id,"
                    " content_hash, signature_hash, canonical_signature, line_start)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol_uuid, scan_id, definition["content_hash"],
                     definition["signature_hash"], definition["canonical_signature"],
                     definition["lines"][0]))

        return len(definitions)

    def _record_presence(self, symbol_uuid: str, scan_id: int) -> None:
        """Extend this symbol's current run of scans, or start a new one."""
        latest = self.connection.execute(
            "SELECT from_scan, to_scan FROM symbol_spans WHERE symbol_uuid = ?"
            " ORDER BY from_scan DESC LIMIT 1", (symbol_uuid,)).fetchone()
        if latest and latest["to_scan"] == scan_id:
            return
        if latest and latest["to_scan"] == self._previous_scan(scan_id):
            self.connection.execute(
                "UPDATE symbol_spans SET to_scan = ? WHERE symbol_uuid = ?"
                " AND from_scan = ?", (scan_id, symbol_uuid, latest["from_scan"]))
            return
        self.connection.execute(
            "INSERT OR REPLACE INTO symbol_spans (symbol_uuid, from_scan, to_scan)"
            " VALUES (?, ?, ?)", (symbol_uuid, scan_id, scan_id))

    def _previous_scan(self, scan_id: int) -> int | None:
        if getattr(self, "_prev_cache", (None, None))[0] != scan_id:
            self._prev_cache = (scan_id, self.previous_scan_id(scan_id))
        return self._prev_cache[1]

    # -- queries ----------------------------------------------------------
    def present_at(self, scan_id: int) -> set[str]:
        """UUIDs of every symbol that existed at a given scan."""
        return {r["symbol_uuid"] for r in self.connection.execute(
            "SELECT symbol_uuid FROM symbol_spans"
            " WHERE from_scan <= ? AND to_scan >= ?", (scan_id, scan_id))}

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

    def missing_at(self, scan_id: int) -> list[sqlite3.Row]:
        """Symbols the index knows about that a given scan did not see.

        Derived rather than stored. An `is_deleted` column would have to be
        maintained by every write path, and once Stage 5 stops re-reading
        unchanged files it would be maintained wrongly — a column that is
        always false is worse than no column, because queries come to trust it.
        """
        return self.connection.execute(
            "SELECT * FROM symbols WHERE last_seen_scan_id < ?"
            " ORDER BY file_path, line_start", (scan_id,)).fetchall()

    def version_at(self, symbol_uuid: str, scan_id: int) -> sqlite3.Row | None:
        """What a symbol looked like as of a given scan.

        The most recent recorded version at or before that scan: a symbol that
        did not change between scans has no row for the later one, and its
        earlier row is still what was true.
        """
        return self.connection.execute(
            "SELECT * FROM symbol_versions WHERE symbol_uuid = ? AND scan_id <= ?"
            " ORDER BY scan_id DESC LIMIT 1", (symbol_uuid, scan_id)).fetchone()
