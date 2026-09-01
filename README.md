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
Next: evaluate whether the drift reports change a decision worth making.

## Use

Zero dependencies. Needs Python 3.9+, but run it on the newest
interpreter you have: a parser cannot read syntax newer than itself, and
the output is identical across versions.

```sh
# What is defined in this codebase, file by file
python -m spanda.cli parse <path>

# The same, dumped as one inspectable JSON record per source file
python -m spanda.cli parse <path> --out out/

# What static analysis cannot see here
python -m spanda.cli gaps <path>

# ...plus symbols nothing references, split by whether the silence is explained
python -m spanda.cli gaps <path> --unreferenced

# Store it. Re-running is safe: symbols keep their identity across scans.
python -m spanda.cli index <path>
python -m spanda.cli scans <path>
python -m spanda.cli find <path> "Order*"

# Replay past commits, so drift has real history to read today
python -m spanda.cli backfill <path> --last 10

# What changed between two scans (defaults to the last two)
python -m spanda.cli drift <path>
python -m spanda.cli drift <path> 3 7 --brief
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
python -m venv .venv && .venv/bin/pip install -q pytest
.venv/bin/python -m pytest tests/ -q
```

Tests are transcribed from `fixtures/README.md`, the answer key for the sample
codebase in `fixtures/sample_pkg/`. If the two ever disagree, one of them is
wrong — that is what the fixture is for. `tests/golden/` holds the frozen
extractor output; regenerate it deliberately with:

```sh
python -m spanda.cli parse fixtures --out tests/golden
```
