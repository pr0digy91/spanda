# spanda

[![CI](https://github.com/pr0digy91/spanda/actions/workflows/ci.yml/badge.svg)](https://github.com/pr0digy91/spanda/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

A deterministic static-analysis indexing engine for Python codebases.

It parses a codebase, records what is defined and what is referenced, and —
the part that matters — states plainly where it cannot see. A tool that
reports "0 callers" for a function the framework calls on every request is
worse than no tool, because "0 callers" reads as "safe to change".

No LLM calls anywhere. Zero runtime dependencies: standard library only.

## Install

The quickest look, with nothing installed permanently — this needs
[uv](https://docs.astral.sh/uv/):

```sh
uvx --from spanda-graph spanda gaps path/to/codebase
```

To keep it:

```sh
uv tool install spanda-graph     # a standalone `spanda` on your PATH
pip install spanda-graph         # or into the current environment
uv add spanda-graph              # or as a project dependency
```

The published distribution is `spanda-graph`; the command and the import
package are both `spanda`. (`spanda` on PyPI is an unrelated soil-spectroscopy
library that got there first.)

Straight from the repository, no release needed:

```sh
uv tool install git+https://github.com/pr0digy91/spanda
pip install git+https://github.com/pr0digy91/spanda
```

Or from a clone, which is what you want if you intend to change anything:

```sh
git clone https://github.com/pr0digy91/spanda
cd spanda
uv sync --all-extras
uv run spanda --help
```

**Run it on the newest Python you have.** A parser cannot read syntax from a
release later than its own, so an old interpreter will record perfectly valid
files as syntax errors. 3.9 is the supported floor, not the version to use; on
macOS the built-in `python3` is usually well behind. `uv tool install` and
`uvx` fetch a current interpreter themselves, which is the easy way out of
this. The output is identical on every supported version — CI checks that on
each commit, across 3.9, 3.11, 3.12 and 3.14.

## Status

M1 complete: per-file extraction (Stage 1) and gap reporting.
M2 complete: SQLite storage with stable symbol identity across scans.
M3 complete: drift reporting and backfill of past commits.
M4 complete: import resolution, processing order, circular import groups.
M5/M6 complete: reference resolution, edges, and `spanda callers`.
M7 complete: incremental re-index — 425 commits in 52 seconds, not 12 minutes.
M8 complete: drift over reference edges and circular-import groups.
Since then: `body_hash`, `spanda profile`, `spanda loops`, and loop depth in drift.

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
| `verdicts.py` | human decisions kept in the index, and the loop that turns a miss into a pattern line |
| `guide.py` | the index described from the index |
| `cli.py` | arguments in, one command run, text out; reads no source itself |

## What it refuses to guess

The distinction the whole tool is built around is between *nothing references
this* and *I cannot see who references this*. They look identical in the
output of most tools, and only one of them means a symbol is safe to delete.

Four shapes make a caller invisible to any reader of the source, and each is
reported as such rather than as an absence:

- **Framework dispatch.** A decorator hands the symbol to something that will
  call it later — an HTTP route, an ORM event hook, a signal receiver, a
  scheduled job. `spanda/dynamic_dispatch.txt` is the list of decorators that
  mean this, one glob per line. It is configuration, grown from the decorator
  census that `spanda parse` prints, not from guesswork.
- **Name assembly at runtime.** `getattr(module, name)`, a handler looked up
  in a dict of strings, `importlib.import_module`. The identifier-shaped
  string literals are recorded as hints; they are never turned into edges.
- **Inheritance from a base the codebase does not contain.** An override of a
  method defined in an installed library is *maybe inherited*, not absent.
- **Attribute access on a value of unknown type.** Where an annotation exists
  it is used; where it does not, the reference is recorded unresolved with the
  reason attached.

Each of those is a labelled reason on the reference, countable in `spanda
gaps`. A heuristic stays a heuristic and is never promoted to an edge in the
graph. If you find a symbol reported with no callers that something really
does call, that is a bug worth filing — it is the exact failure this project
exists to eliminate.

## Use

```sh
# What is defined in this codebase, file by file
spanda parse <path>

# The same, dumped as one inspectable JSON record per source file
spanda parse <path> --out out/

# What static analysis cannot see here
spanda gaps <path>

# ...plus symbols nothing references, split by whether the silence is explained
spanda gaps <path> --unreferenced

# Store it. Re-running is safe: symbols keep their identity across scans.
spanda index <path>
spanda scans <path>
spanda find <path> "Order*"

# What the code keeps doing: names re-implemented across files (and whether
# they are verbatim copies), parameter naming and annotation rates, docstrings,
# decorators, and which symbols never settle
spanda profile <path>

# Where the loops are: nested in one body, nested across calls, recursive,
# and database calls inside them. Places to look, never a complexity.
spanda loops <path>

# The verdicts loop. Record a person's decision in the index, then check
# every recorded decision against the newest scan: the pattern lines that
# alive verdicts imply, the verdicts the code now contradicts, and the next
# candidates to vet. --export / --from move verdicts between indexes.
spanda vet <path> --alive file.py::Class.method --note "why"
spanda vet <path>

# A note on reading the index, with that index's own numbers in it
spanda guide <path> --write

# What calls this symbol, and what might but cannot be proven to
spanda callers <path> create_goods_receipt

# Link every reference to a definition, with reasons for the ones that fail.
# Ends with a self-audit: imports the resolver could not place. Expect zero.
spanda resolve <path> --reasons 3

# Which file does each import point at, and what imports circularly
spanda imports <path>
spanda imports <path> --order

# Replay past commits, so drift has real history to read today
spanda backfill <path> --last 10

# What changed between two scans (defaults to the last two)
spanda drift <path>
spanda drift <path> 3 7 --brief
```

From a clone, put `uv run` in front of each: `uv run spanda gaps <path>`.

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
uv run python -m pytest tests/ -q
```

Tests are transcribed from `fixtures/README.md`, the answer key for the sample
codebase in `fixtures/sample_pkg/`. If the two ever disagree, one of them is
wrong — that is what the fixture is for. `tests/golden/` holds the frozen
extractor output; regenerate it deliberately with:

```sh
uv run python -m spanda.cli parse fixtures --out tests/golden
```

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md)
for the setup, the rule that the fixture answer key changes before the tests do,
and what the project deliberately will not do.

## License

MIT — see [LICENSE](LICENSE).
