# spanda

A deterministic static-analysis indexing engine for Python codebases.

It parses a codebase, records what is defined and what is referenced, and —
the part that matters — states plainly where it cannot see. A tool that
reports "0 callers" for a function the framework calls on every request is
worse than no tool, because "0 callers" reads as "safe to change".

No LLM calls anywhere. See `PROJECT_INSTRUCTIONS.md` for the design and the
milestone plan.

## Status

M1 complete: per-file extraction (Stage 1) and gap reporting.
M2 complete: SQLite storage with stable symbol identity across scans.
M3 complete: drift reporting and backfill of past commits.
M4 complete: import resolution, processing order, circular import groups.
M5/M6 complete: reference resolution, edges, and `spanda callers`.
M7 complete: incremental re-index — 425 commits in 52 seconds, not 12 minutes.
M8 complete: drift over reference edges and circular-import groups.

All eight planned milestones built, plus `gaps`, `profile` and `loops`, which
were not in the original plan.

Each module does one thing, and says which in its first line:

| module | role |
|---|---|
| `extract.py` | one file to one record: definitions, references, imports, hashes, hints |
| `modules.py` | dotted names to files; import graph; cycle groups |
| `resolve.py` | a reference to the definition it means, or a reason it cannot be |
| `scan.py` | one scan into an open index, full or incremental; all the git calls |
| `store.py` | the SQLite index: identity across scans, presence, versions, migrations |
| `drift.py` | two scans compared |
| `gaps.py` | what the extractor cannot see, made explicit |
| `profile.py` | what the corpus keeps doing |
| `loops.py` | where the loops are, and what runs inside them |
| `guide.py` | the index described from the index |
| `cli.py` | arguments in, one command run, text out; reads no source itself |

## Use

Zero dependencies. Needs Python 3.9+, but run it on the newest interpreter you
have: a parser cannot read syntax newer than itself, and the output is
identical across versions.

On macOS, `python3` is usually Apple's older build. Check with
`python3 --version`; if it is behind the code you want to read, use a newer one
explicitly. The commands below use this project's virtual environment, which is
built on the newest interpreter found at setup:

```sh
python3.14 -m venv .venv && .venv/bin/pip install -q pytest
.venv/bin/python --version
```

```sh
# What is defined in this codebase, file by file
.venv/bin/python -m spanda.cli parse <path>

# The same, dumped as one inspectable JSON record per source file
.venv/bin/python -m spanda.cli parse <path> --out out/

# What static analysis cannot see here
.venv/bin/python -m spanda.cli gaps <path>

# ...plus symbols nothing references, split by whether the silence is explained
.venv/bin/python -m spanda.cli gaps <path> --unreferenced

# Store it. Re-running is safe: symbols keep their identity across scans.
.venv/bin/python -m spanda.cli index <path>
.venv/bin/python -m spanda.cli scans <path>
.venv/bin/python -m spanda.cli find <path> "Order*"

# What the code keeps doing: names re-implemented across files (and whether
# they are verbatim copies), parameter naming and annotation rates, docstrings,
# decorators, and which symbols never settle
.venv/bin/python -m spanda.cli profile <path>

# Where the loops are: nested in one body, nested across calls, recursive,
# and database calls inside them. Places to look, never a complexity.
.venv/bin/python -m spanda.cli loops <path>

# A note on reading the index, with that index's own numbers in it
.venv/bin/python -m spanda.cli guide <path> --write

# What calls this symbol, and what might but cannot be proven to
.venv/bin/python -m spanda.cli callers <path> create_goods_receipt

# Link every reference to a definition, with reasons for the ones that fail.
# Ends with a self-audit: imports the resolver could not place. Expect zero.
.venv/bin/python -m spanda.cli resolve <path> --reasons 3

# Which file does each import point at, and what imports circularly
.venv/bin/python -m spanda.cli imports <path>
.venv/bin/python -m spanda.cli imports <path> --order

# Replay past commits, so drift has real history to read today
.venv/bin/python -m spanda.cli backfill <path> --last 10

# What changed between two scans (defaults to the last two)
.venv/bin/python -m spanda.cli drift <path>
.venv/bin/python -m spanda.cli drift <path> 3 7 --brief
```

The index lives at `<path>/.spanda/index.db` — one authoritative index per
codebase, inside the codebase it describes. `.spanda/` ignores itself via a
`.gitignore` of `*`: indexes are derived data and never belong in a commit.

Run history is in the index, not in filenames — `spanda scans <path>` lists
every run with its timestamp, commit and content fingerprint.

Indexing streams one file at a time, so peak memory depends on the largest
single file rather than the size of the codebase — 680 files index in 52 MB
and about 3 seconds. Nothing here is async, deliberately: parsing is
CPU-bound and never waits, so async would add machinery and no speed.

`parse` ends with a census of every decorator in use. That census is the input
for `spanda/dynamic_dispatch.txt`, the list of decorators that make a symbol's
callers unknowable. Grow that file from the census rather than guessing.

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
```

Tests are transcribed from `fixtures/README.md`, the answer key for the sample
codebase in `fixtures/sample_pkg/`. If the two ever disagree, one of them is
wrong — that is what the fixture is for. `tests/golden/` holds the frozen
extractor output; regenerate it deliberately with:

```sh
.venv/bin/python -m spanda.cli parse fixtures --out tests/golden
```
