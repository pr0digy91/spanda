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

SCHEMA_VERSION = 13
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
    -- The resolver's self-audit: imported names whose definition it could
    -- not find. Expected to be zero. Stored per scan so a rise between two
    -- commits is itself a drift signal — someone added a pattern the tool
    -- cannot follow.
    lost_trails         INTEGER DEFAULT 0,
    -- 1 when this scan read every file and so could compute the import
    -- graph. An incremental backfill scan reads a handful of files and
    -- cannot; drift must say "no cycle data at scan N", never "no cycles".
    cycles_recorded     INTEGER NOT NULL DEFAULT 0,
    completed           INTEGER NOT NULL DEFAULT 0
);

-- Circular-import groups as they stood at a scan, one row per member file.
-- Import edges are file-to-file, not symbol-to-symbol, so they do not fit the
-- edges table; the groups are the thing drift needs, so the groups are what
-- is kept.
CREATE TABLE IF NOT EXISTS scan_cycles (
    scan_id   INTEGER NOT NULL,
    group_id  INTEGER NOT NULL,
    file_path TEXT    NOT NULL,
    PRIMARY KEY (scan_id, group_id, file_path)
);

-- One row per file, not per file per scan. Storing the full listing every
-- scan cost 292,020 rows for 1,097 files across 425 scans — an unchanged
-- listing re-recorded 425 times. Symbols were already deduped this way; this
-- brings files into line, and takes the index from 80 MB to a fraction of it.
CREATE TABLE IF NOT EXISTS files (
    file_path           TEXT    PRIMARY KEY,
    module              TEXT,
    file_hash           TEXT,
    parse_status        TEXT    NOT NULL,
    parse_error_line    INTEGER,
    parse_error_message TEXT,
    symbol_count        INTEGER DEFAULT 0,
    first_seen_scan_id  INTEGER NOT NULL,
    last_seen_scan_id   INTEGER NOT NULL
);

-- Written only when a file's content or parse status actually changes.
CREATE TABLE IF NOT EXISTS file_versions (
    file_path    TEXT    NOT NULL,
    scan_id      INTEGER NOT NULL,
    file_hash    TEXT,
    parse_status TEXT,
    symbol_count INTEGER,
    PRIMARY KEY (file_path, scan_id)
);

-- Files a scan could not read. Rare, and needed exactly as they were at that
-- scan, so these are recorded per scan rather than deduped.
CREATE TABLE IF NOT EXISTS scan_problems (
    scan_id   INTEGER NOT NULL,
    file_path TEXT    NOT NULL,
    line      INTEGER,
    message   TEXT,
    PRIMARY KEY (scan_id, file_path)
);

-- What a scan chose not to read, so `skipped_files` on the scans row can be
-- explained from the index rather than remembered from a terminal. One row
-- per excluded directory (with a count) and one per git-ignored file.
CREATE TABLE IF NOT EXISTS scan_unread (
    scan_id INTEGER NOT NULL,
    path    TEXT    NOT NULL,   -- a file, or a directory name ending in /
    reason  TEXT    NOT NULL,   -- directory_excluded | ignored_by_git
    files   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (scan_id, path)
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
    body_hash           TEXT,
    -- Loop depth as of this version, so drift can say "deeper than it was".
    -- NULL on rows written before schema 13 whose body has since changed;
    -- the row for a body that is still current is filled at the next scan.
    loop_depth          INTEGER,
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
    -- Hash of the code with docstrings and string wording removed: what the
    -- symbol does rather than what it says. NULL only for a symbol recorded
    -- before schema 11 and not re-read since.
    body_hash            TEXT,
    -- Loops nested in the symbol's own body (for a variable: where the
    -- assignment sits). Syntactic. Says where loops are, not how they scale.
    loop_depth           INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_file_versions_scan ON file_versions (scan_id);
-- Resolved references. Deduped the way symbols are: an edge that survives a
-- scan keeps its identity rather than being rewritten every time.
CREATE TABLE IF NOT EXISTS edges (
    uuid               TEXT    PRIMARY KEY,
    edge_key           TEXT    NOT NULL UNIQUE,
    source_symbol_uuid TEXT,             -- NULL means module-level code
    source_file        TEXT    NOT NULL,
    target_symbol_uuid TEXT    NOT NULL,
    edge_type          TEXT    NOT NULL, -- calls | inherits | uses
    -- Deepest loop any site of this edge sits inside, as of its last sighting.
    loop_depth         INTEGER NOT NULL DEFAULT 0,
    first_seen_scan_id INTEGER NOT NULL,
    last_seen_scan_id  INTEGER NOT NULL
);

-- References that could have pointed at this codebase and did not resolve.
-- Held for the most recent scan only: this describes the code as it is now
-- rather than being a drift signal, and 34,000 rows per scan would dwarf
-- everything else in the index.
--
-- `attr_name` is the member being reached for. It is what makes "who else
-- might be calling this" answerable at all when the object's type is unknown.
CREATE TABLE IF NOT EXISTS unresolved_refs (
    scan_id            INTEGER NOT NULL,
    source_file        TEXT    NOT NULL,
    source_symbol_uuid TEXT,
    raw                TEXT,
    attr_name          TEXT,
    line               INTEGER,
    reason             TEXT    NOT NULL
);

-- Calls inside loops that the resolver could not follow — a database session
-- is an external type, so `session.execute` in a loop never becomes an edge,
-- and without this row the loop body would read as empty. Current scan only.
CREATE TABLE IF NOT EXISTS loop_calls (
    scan_id            INTEGER NOT NULL,
    source_file        TEXT    NOT NULL,
    source_symbol_uuid TEXT,
    raw                TEXT    NOT NULL,
    line               INTEGER,
    loop_depth         INTEGER NOT NULL,
    reason             TEXT    NOT NULL
);

-- The trails themselves, for the current scan, so the cause can be found.
CREATE TABLE IF NOT EXISTS lost_trails (
    scan_id       INTEGER NOT NULL,
    source_file   TEXT    NOT NULL,
    line          INTEGER,
    raw           TEXT,
    target_module TEXT,
    name          TEXT
);

CREATE INDEX IF NOT EXISTS idx_edges_target ON edges (target_symbol_uuid);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges (source_symbol_uuid);
CREATE INDEX IF NOT EXISTS idx_unresolved_attr ON unresolved_refs (attr_name);
CREATE INDEX IF NOT EXISTS idx_versions_scan ON symbol_versions (scan_id);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols (file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_carry ON symbols (file_path, last_seen_scan_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols (name);
CREATE INDEX IF NOT EXISTS idx_symbols_last_seen ON symbols (last_seen_scan_id);
CREATE INDEX IF NOT EXISTS idx_symbols_module ON symbols (module);
"""

#: Unresolved reasons worth storing: these could have pointed at this
#: codebase. Externals and builtins could not, and would only add bulk.
KEEPABLE_REASONS = frozenset({
    "attribute_on_unknown_type", "no_such_attribute", "not_found"})

SYMBOL_FIELDS = [
    "name", "qualname", "kind", "module", "file_path", "line_start",
    "line_end", "signature", "canonical_signature", "docstring", "decorators",
    "has_dynamic_dispatch", "definition_count", "signature_varies",
    "content_hash", "signature_hash", "body_hash", "loop_depth",
]

#: Additive changes an older index can be brought forward by, keyed by the
#: version they produce. Anything that cannot be expressed as one of these is
#: not a migration, and the guard below says so rather than guessing.
MIGRATIONS: dict[int, list[tuple[str, str, str]]] = {
    # (table, column, declaration) — applied only if the column is missing.
    11: [("symbols", "body_hash", "TEXT"),
         ("symbol_versions", "body_hash", "TEXT")],
    12: [("symbols", "loop_depth", "INTEGER NOT NULL DEFAULT 0"),
         ("edges", "loop_depth", "INTEGER NOT NULL DEFAULT 0")],
    13: [("symbol_versions", "loop_depth", "INTEGER")],
}


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
            bodies = "|".join(d["body_hash"] for d in group)
            first["body_hash"] = "sha256:" + hashlib.sha256(
                bodies.encode()).hexdigest()[:32]
            first["definition_count"] = len(group)
            first["signature_varies"] = len({d["signature_hash"] for d in group}) > 1
            first["loop_depth"] = max(d["loop_depth"] for d in group)
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
        #: Set when opening brought an older index forward, so a command can
        #: say so instead of leaving the user to wonder why a column is empty.
        self.migrated_from: int | None = None

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
        version = int(stored)
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise IndexError_(
                f"{self.path} has schema version {stored}, newer than this "
                f"build's version {SCHEMA_VERSION}. Upgrade spanda, or delete "
                f"the file and re-index.")
        if any(step not in MIGRATIONS for step in range(version + 1, SCHEMA_VERSION + 1)):
            raise IndexError_(
                f"{self.path} has schema version {stored}, older than this "
                f"build's version {SCHEMA_VERSION}, and no migration covers "
                f"the gap: delete the file and re-index.")
        self._migrate(version)

    def _migrate(self, version: int) -> None:
        """Bring an older index forward, one additive step at a time.

        Only columns are ever added, and the new column is NULL for every
        row that was written before it existed. That is the honest state: a
        migration cannot invent a hash for source it never read. The next
        `spanda index` fills the column for every symbol it re-reads.
        """
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for step in range(version + 1, SCHEMA_VERSION + 1):
                for table, column, declaration in MIGRATIONS[step]:
                    present = {row["name"] for row in self.connection.execute(
                        f"PRAGMA table_info({table})")}
                    if column not in present:
                        self.connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                self._set_meta("schema_version", str(step))
                self._set_meta(f"migrated_to_{step}", json.dumps({
                    "from": step - 1,
                    "when": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        self.migrated_from = version

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
        # A version row written before loop depth existed has NULL there. If
        # its body is the one this scan just read — same content hash — the
        # depth is known exactly, because depth is a function of the body.
        # Older bodies stay NULL: nothing read them, and a migration cannot
        # invent what it never saw.
        self.connection.execute(
            "UPDATE symbol_versions SET loop_depth = ("
            "  SELECT s.loop_depth FROM symbols s WHERE s.uuid = symbol_versions.symbol_uuid)"
            " WHERE loop_depth IS NULL AND EXISTS ("
            "  SELECT 1 FROM symbols s WHERE s.uuid = symbol_versions.symbol_uuid"
            "  AND s.content_hash = symbol_versions.content_hash"
            "  AND s.last_seen_scan_id = ?)", (scan_id,))
        # Derived from the files this scan says are present, not accumulated
        # as they streamed past. An incremental scan only opens the files that
        # changed, so an accumulator would describe the diff rather than the
        # codebase — and the fingerprint has to mean the same thing whether a
        # scan read 1,098 files or three.
        digest = hashlib.sha256()
        files = parsed = unparseable = symbols = 0
        for row in self.connection.execute(
                "SELECT file_path, file_hash, parse_status, symbol_count"
                " FROM files WHERE last_seen_scan_id = ? ORDER BY file_path",
                (scan_id,)):
            digest.update(f"{row['file_path']}:{row['file_hash']}\n".encode())
            files += 1
            parsed += row["parse_status"] == "ok"
            unparseable += row["parse_status"] != "ok"
            symbols += row["symbol_count"] or 0

        ambiguous = self.connection.execute(
            "SELECT COUNT(*) FROM symbols"
            " WHERE last_seen_scan_id = ? AND definition_count > 1",
            (scan_id,)).fetchone()[0]

        self.connection.execute(
            "UPDATE scans SET total_files = ?, parsed_files = ?,"
            " unparseable_files = ?, total_symbols = ?, ambiguous_symbols = ?,"
            " content_fingerprint = ?, completed = 1 WHERE scan_id = ?",
            (files, parsed, unparseable, symbols, ambiguous,
             "sha256:" + digest.hexdigest()[:32], scan_id))
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
        path, status = record["file"], record["parse_status"]

        known = self.connection.execute(
            "SELECT file_hash, parse_status FROM files WHERE file_path = ?",
            (path,)).fetchone()
        if known is None:
            self.connection.execute(
                "INSERT INTO files (file_path, module, file_hash, parse_status,"
                " parse_error_line, parse_error_message, symbol_count,"
                " first_seen_scan_id, last_seen_scan_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (path, record["module"], record["file_hash"], status,
                 error.get("line"), error.get("message"), len(definitions),
                 scan_id, scan_id))
            file_changed = True
        else:
            file_changed = (known["file_hash"] != record["file_hash"]
                            or known["parse_status"] != status)
            self.connection.execute(
                "UPDATE files SET module = ?, file_hash = ?, parse_status = ?,"
                " parse_error_line = ?, parse_error_message = ?,"
                " symbol_count = ?, last_seen_scan_id = ? WHERE file_path = ?",
                (record["module"], record["file_hash"], status,
                 error.get("line"), error.get("message"), len(definitions),
                 scan_id, path))

        if file_changed:
            self.connection.execute(
                "INSERT OR REPLACE INTO file_versions (file_path, scan_id,"
                " file_hash, parse_status, symbol_count) VALUES (?, ?, ?, ?, ?)",
                (path, scan_id, record["file_hash"], status, len(definitions)))

        if status != "ok":
            self.connection.execute(
                "INSERT OR REPLACE INTO scan_problems (scan_id, file_path, line,"
                " message) VALUES (?, ?, ?, ?)",
                (scan_id, path, error.get("line"), error.get("message")))

        for definition in definitions:
            key = symbol_key(record["file"], definition["qualname"], definition["kind"])
            dynamic = any(is_dynamic_dispatch(d["base"], patterns)
                          for d in definition["decorators"])
            # Built by column name and then ordered by SYMBOL_FIELDS, so a
            # column added to one and not the other fails here, loudly, not
            # by writing every value one slot to the left.
            by_field = {
                "name": definition["name"], "qualname": definition["qualname"],
                "kind": definition["kind"], "module": record["module"],
                "file_path": record["file"],
                "line_start": definition["lines"][0],
                "line_end": definition["lines"][1],
                "signature": (json.dumps(definition["signature"])
                              if definition["signature"] else None),
                "canonical_signature": definition["canonical_signature"],
                "docstring": definition["docstring"],
                "decorators": json.dumps(definition["decorators"]),
                "has_dynamic_dispatch": int(dynamic),
                "definition_count": definition["definition_count"],
                "signature_varies": int(definition["signature_varies"]),
                "content_hash": definition["content_hash"],
                "signature_hash": definition["signature_hash"],
                "body_hash": definition["body_hash"],
                "loop_depth": definition["loop_depth"],
            }
            assert set(by_field) == set(SYMBOL_FIELDS), "symbols columns drifted"
            values = tuple(by_field[f] for f in SYMBOL_FIELDS)

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
                    " content_hash, signature_hash, canonical_signature, line_start,"
                    " body_hash, loop_depth) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol_uuid, scan_id, definition["content_hash"],
                     definition["signature_hash"], definition["canonical_signature"],
                     definition["lines"][0], definition["body_hash"],
                     definition["loop_depth"]))

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

    def carry_forward(self, scan_id: int, present: set[str],
                      reparsed: set[str]) -> dict:
        """Mark everything the scan did not look at as still present.

        This is the step an incremental index cannot skip. A file that was not
        re-read has not disappeared — it simply was not looked at — and
        without this the first incremental run reports the whole unchanged
        codebase as deleted.

        It works from the files that exist *now*, not from the previous scan's
        id. Keying on "whatever was last seen at scan N-1" means a single
        missed carry-forward strands a file permanently: the next scan looks
        for N-1 and the file is at N-2, so it is never picked up again. That
        stranding is silent, and it is what a naive version of this does.
        """
        carry = sorted(present - reparsed)
        self.connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _carry"
            " (path TEXT PRIMARY KEY, last_seen INTEGER)")
        self.connection.execute("DELETE FROM _carry")
        if carry:
            placeholders = ",".join("?" * len(carry))
            self.connection.execute(
                f"INSERT INTO _carry (path, last_seen)"
                f" SELECT file_path, last_seen_scan_id FROM files"
                f" WHERE file_path IN ({placeholders})", tuple(carry))

        files = self.connection.execute(
            "UPDATE files SET last_seen_scan_id = ?"
            " WHERE file_path IN (SELECT path FROM _carry)", (scan_id,)).rowcount

        # Only symbols that were present the last time the file was actually
        # read. A symbol deleted back then must not be resurrected by a scan
        # that never opened the file.
        symbols = self.connection.execute(
            "UPDATE symbols SET last_seen_scan_id = ?"
            " WHERE EXISTS (SELECT 1 FROM _carry c WHERE c.path = symbols.file_path"
            "               AND c.last_seen = symbols.last_seen_scan_id)",
            (scan_id,)).rowcount

        # Extend each carried symbol's run of presence rather than starting a
        # new one, then open a run for any that had none reaching this scan.
        self.connection.execute(
            "UPDATE symbol_spans SET to_scan = ?"
            " WHERE symbol_uuid IN (SELECT uuid FROM symbols"
            "                       WHERE last_seen_scan_id = ?)"
            "   AND to_scan = (SELECT MAX(to_scan) FROM symbol_spans sp"
            "                  WHERE sp.symbol_uuid = symbol_spans.symbol_uuid)"
            "   AND to_scan < ?", (scan_id, scan_id, scan_id))

        self.connection.execute(
            "INSERT OR IGNORE INTO symbol_spans (symbol_uuid, from_scan, to_scan)"
            " SELECT uuid, ?, ? FROM symbols WHERE last_seen_scan_id = ?"
            "   AND NOT EXISTS (SELECT 1 FROM symbol_spans sp"
            "                   WHERE sp.symbol_uuid = symbols.uuid"
            "                     AND sp.to_scan >= ?)",
            (scan_id, scan_id, scan_id, scan_id))

        self.connection.execute(
            "UPDATE edges SET last_seen_scan_id = ?"
            " WHERE source_file IN (SELECT path FROM _carry)"
            "   AND last_seen_scan_id < ?", (scan_id, scan_id))

        return {"files": files, "symbols": symbols}

    # -- references -------------------------------------------------------
    def write_references(self, scan_id: int, references, keys_to_uuid: dict) -> dict:
        """Store resolved edges, and the unresolved references worth keeping."""
        counts = {"edges": 0, "unresolved": 0}
        self.connection.execute(
            "DELETE FROM unresolved_refs WHERE scan_id != ?", (scan_id,))
        self.connection.execute(
            "DELETE FROM loop_calls WHERE scan_id != ?", (scan_id,))
        # One edge can have several sites; it carries the deepest of them,
        # and the value is *this* scan's, so a call moved out of a loop
        # reads as out of it — not as the deepest it has ever been.
        depth_this_scan: dict[str, int] = {}

        for reference in references:
            source_uuid = (keys_to_uuid.get(reference.source_symbol)
                           if reference.source_symbol else None)

            if reference.target_symbol:
                target_uuid = keys_to_uuid.get(reference.target_symbol)
                if target_uuid is None:
                    continue
                key = (f"{reference.source_symbol or reference.source_file}"
                       f"->{reference.target_symbol}|{reference.edge_type}")
                depth = max(depth_this_scan.get(key, 0), reference.loop_depth)
                depth_this_scan[key] = depth
                existing = self.connection.execute(
                    "SELECT uuid FROM edges WHERE edge_key = ?", (key,)).fetchone()
                if existing:
                    self.connection.execute(
                        "UPDATE edges SET last_seen_scan_id = ?, loop_depth = ?"
                        " WHERE uuid = ?", (scan_id, depth, existing["uuid"]))
                else:
                    self.connection.execute(
                        "INSERT INTO edges (uuid, edge_key, source_symbol_uuid,"
                        " source_file, target_symbol_uuid, edge_type,"
                        " first_seen_scan_id, last_seen_scan_id, loop_depth)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (uuid_module.uuid4().hex, key, source_uuid,
                         reference.source_file, target_uuid,
                         reference.edge_type, scan_id, scan_id, depth))
                counts["edges"] += 1
                continue

            if reference.loop_depth > 0 and reference.edge_type == "calls" \
                    and reference.reason != "assignment_target":
                self.connection.execute(
                    "INSERT INTO loop_calls (scan_id, source_file, source_symbol_uuid,"
                    " raw, line, loop_depth, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (scan_id, reference.source_file, source_uuid, reference.raw,
                     reference.line, reference.loop_depth, reference.reason))
            if reference.reason in KEEPABLE_REASONS:
                self.connection.execute(
                    "INSERT INTO unresolved_refs (scan_id, source_file,"
                    " source_symbol_uuid, raw, attr_name, line, reason)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (scan_id, reference.source_file, source_uuid,
                     reference.raw, reference.raw.rpartition(".")[2],
                     reference.line, reference.reason))
                counts["unresolved"] += 1
        return counts

    def record_lost_trails(self, scan_id: int, lost) -> None:
        self.connection.execute(
            "DELETE FROM lost_trails WHERE scan_id != ?", (scan_id,))
        self.connection.executemany(
            "INSERT INTO lost_trails (scan_id, source_file, line, raw,"
            " target_module, name) VALUES (?, ?, ?, ?, ?, ?)",
            [(scan_id, t.source_file, t.line, t.raw, t.target_module, t.name)
             for t in lost])
        self.connection.execute(
            "UPDATE scans SET lost_trails = ? WHERE scan_id = ?",
            (len(lost), scan_id))

    def record_cycles(self, scan_id: int, groups: list[list[str]]) -> None:
        """Store this scan's circular-import groups, and mark that it could."""
        self.connection.executemany(
            "INSERT OR IGNORE INTO scan_cycles (scan_id, group_id, file_path)"
            " VALUES (?, ?, ?)",
            [(scan_id, n, f) for n, group in enumerate(groups) for f in group])
        self.connection.execute(
            "UPDATE scans SET cycles_recorded = 1 WHERE scan_id = ?", (scan_id,))

    def cycles_at(self, scan_id: int) -> set[frozenset[str]] | None:
        """Cycle groups at a scan, or None if that scan never computed them."""
        row = self.scan(scan_id)
        if row is None or not row["cycles_recorded"]:
            return None
        groups: dict[int, set[str]] = {}
        for r in self.connection.execute(
                "SELECT group_id, file_path FROM scan_cycles WHERE scan_id = ?",
                (scan_id,)):
            groups.setdefault(r["group_id"], set()).add(r["file_path"])
        return {frozenset(g) for g in groups.values()}

    def has_edge_data_at(self, scan_id: int) -> bool:
        """Whether any references had been resolved as of this scan.

        Backfill resolves references only at its newest commit, so an earlier
        backfilled scan has no edges at all. Diffing against it would report
        every edge as "added", which is a fact about the tool, not the code.
        """
        return self.connection.execute(
            "SELECT 1 FROM edges WHERE first_seen_scan_id <= ? LIMIT 1",
            (scan_id,)).fetchone() is not None

    def edges_at(self, scan_id: int) -> dict[str, sqlite3.Row]:
        """Edges present at a scan, by edge_key.

        An edge is taken as present between its first and last sighting, and
        only while both of its endpoint symbols were present. Edges carry no
        presence spans of their own, so an edge that vanished and returned
        inside that window reads as present throughout — narrower than the
        symbol case, since both endpoints are checked, but not exact.
        """
        present = self.present_at(scan_id)
        found: dict[str, sqlite3.Row] = {}
        for r in self.connection.execute(
                "SELECT e.*, s.qualname AS source_name, t.qualname AS target_name,"
                "       t.file_path AS target_file"
                " FROM edges e"
                " LEFT JOIN symbols s ON s.uuid = e.source_symbol_uuid"
                " JOIN symbols t ON t.uuid = e.target_symbol_uuid"
                " WHERE e.first_seen_scan_id <= ? AND e.last_seen_scan_id >= ?",
                (scan_id, scan_id)):
            if r["target_symbol_uuid"] not in present:
                continue
            if r["source_symbol_uuid"] and r["source_symbol_uuid"] not in present:
                continue
            found[r["edge_key"]] = r
        return found

    def symbol_uuids_by_key(self) -> dict[str, str]:
        return {r["symbol_key"]: r["uuid"] for r in
                self.connection.execute("SELECT symbol_key, uuid FROM symbols")}

    def callers_of(self, symbol_uuid: str) -> list[sqlite3.Row]:
        latest = self.connection.execute(
            "SELECT MAX(scan_id) AS s FROM scans WHERE completed = 1").fetchone()["s"]
        return self.connection.execute(
            "SELECT e.edge_type, e.source_file, s.qualname, s.kind, s.file_path,"
            "       s.line_start, s.has_dynamic_dispatch"
            " FROM edges e LEFT JOIN symbols s ON s.uuid = e.source_symbol_uuid"
            " WHERE e.target_symbol_uuid = ? AND e.last_seen_scan_id = ?"
            " ORDER BY s.file_path, s.line_start", (symbol_uuid, latest)).fetchall()

    def possible_callers_of(self, name: str) -> list[sqlite3.Row]:
        """Unresolved references reaching for this name.

        `obj.create_receipt()` where the type of `obj` is unknown may or may
        not be a call to this symbol. Reporting the count is the difference
        between "3 callers" and "3 callers, plus 12 places reaching for this
        name on something I could not identify".
        """
        return self.connection.execute(
            "SELECT source_file, line, raw, reason FROM unresolved_refs"
            " WHERE attr_name = ? ORDER BY source_file, line", (name,)).fetchall()

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

    def migrations(self) -> list[dict]:
        """Every time this index was brought forward, oldest first."""
        rows = self.connection.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'migrated_to_%'"
            " ORDER BY CAST(substr(key, 13) AS INTEGER)").fetchall()
        return [{"to": int(r["key"][12:]), **json.loads(r["value"])} for r in rows]

    def record_unread(self, scan_id: int, plan) -> None:
        """Write down what this scan is not going to read, before reading."""
        rows = [(scan_id, f"{name}/", "directory_excluded", len(paths))
                for name, paths in plan.skipped.items() if paths]
        rows += [(scan_id, path, "ignored_by_git", 1) for path in plan.ignored]
        self.connection.executemany(
            "INSERT OR REPLACE INTO scan_unread (scan_id, path, reason, files)"
            " VALUES (?, ?, ?, ?)", rows)

    def unread_at(self, scan_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT path, reason, files FROM scan_unread WHERE scan_id = ?"
            " ORDER BY reason, path", (scan_id,)).fetchall()

    def gone_since_previous(self, scan_id: int) -> list[sqlite3.Row]:
        """Symbols present in the completed scan before this one and absent now.

        `missing_at` answers "what has ever gone"; after a 425-scan backfill
        that is 1,500 rows, every one of them old news. This is the list a
        scan report should show: what this scan is the first not to see.
        """
        previous = self.connection.execute(
            "SELECT MAX(scan_id) FROM scans WHERE completed = 1 AND scan_id < ?",
            (scan_id,)).fetchone()[0]
        if previous is None:
            return []
        return self.connection.execute(
            "SELECT * FROM symbols WHERE last_seen_scan_id = ?"
            " ORDER BY file_path, line_start", (previous,)).fetchall()

    def version_at(self, symbol_uuid: str, scan_id: int) -> sqlite3.Row | None:
        """What a symbol looked like as of a given scan.

        The most recent recorded version at or before that scan: a symbol that
        did not change between scans has no row for the later one, and its
        earlier row is still what was true.
        """
        return self.connection.execute(
            "SELECT * FROM symbol_versions WHERE symbol_uuid = ? AND scan_id <= ?"
            " ORDER BY scan_id DESC LIMIT 1", (symbol_uuid, scan_id)).fetchone()
