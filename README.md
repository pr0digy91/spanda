# spanda

[![CI](https://github.com/pr0digy91/spanda/actions/workflows/ci.yml/badge.svg)](https://github.com/pr0digy91/spanda/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

**Find out who calls a function — and be told, plainly, when nobody can know.**

spanda reads a Python codebase, records every definition and every reference,
and stores the result as a queryable index. It is a static analyser, so there
is a limit to what it can see. The difference from most tools is that it tells
you where that limit is instead of rounding it down to zero.

Ask most tools who calls an event hook and you get `0 callers`, which reads as
*safe to delete*. Ask spanda:

```console
$ spanda callers . _apply_rls_context

models.py:57  function _apply_rls_context
    (positional:session,positional:flush_context,positional:instances)->None

  no static callers found

  ...but this symbol is dispatched at runtime. Whatever calls it is not
     visible in the source, so the count above is not the whole story.
```

No LLM calls anywhere. No network access. Zero runtime dependencies —
standard library only. The same input produces the same output every time.

## Install

The quickest look, with nothing installed permanently — needs
[uv](https://docs.astral.sh/uv/):

```sh
uvx --from spanda-graph spanda gaps path/to/your/code
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

**Run it on the newest Python you have.** A parser cannot read syntax from a
release later than its own, so an old interpreter records perfectly valid files
as syntax errors. 3.9 is the supported floor, not the version to use; on macOS
the built-in `python3` is usually well behind. `uv tool install` and `uvx`
fetch a current interpreter for you, which is the easy way out of this. The
output is identical on every supported version, and CI checks that on each
commit across 3.9, 3.11, 3.12 and 3.14.

## Quick start

Point it at a codebase. Nothing is written outside the index.

```sh
spanda index .          # parse everything and store it
spanda gaps .           # what static analysis cannot see here — read this first
spanda callers . my_function
```

`spanda index` prints what it did, and ends by auditing itself:

```console
$ spanda index .
index: /path/to/code/.spanda/index.db
scan 1: 21 files under /path/to/code

scan 1 complete
  20 files parsed, 1 unparseable, 0 not looked at
  98 symbols (0 defined more than once in their file)
  39 references resolved to a definition, 14 could not be
  self-audit: every name brought in by an import statement, at the top of a file or
  inside a function, was traced to its definition
  content fingerprint sha256:7abd2b9a9dd315ebda28ac8edde10191
  at commit 4c09b067b16e (clean tree)
```

That "could not be" number is not swept up. `spanda gaps` breaks it down, and
every entry carries the reason:

```console
$ spanda gaps .
21 files, 98 symbols, 0 files not looked at

Decorated with something that dispatches at runtime — the framework calls these,
  and no reference in this codebase names them:

  models.py:57
      _apply_rls_context
      @event.listens_for(Session, 'before_flush')
  (4)

Methods a framework calls by name on a subclass of its own base — no decorator
  marks them, nothing here calls them, and the base is outside this codebase:

  middleware.py:41
      RequestLogger.dispatch
      overrides dispatch on BaseHTTPMiddleware
  (1)

Decorated with something on neither list, and nothing names them. Not a claim
  that a framework calls these — a statement that spanda does not know. Vet, then
  add a line to dynamic_dispatch.txt either way:

  middleware.py:45
      nightly_cleanup
      @scheduler.scheduled_job('cron', hour=3)
  (1)

String literals that spell the name of a symbol defined elsewhere (heuristic —
  a name match is not a call, and must never become an edge):

  dynamic.py:14
      <module>
      "on_created" names a symbol defined at handlers.py:9
  (3)
```

The last one is the shape that makes dead-code detection dangerous:
`handlers.on_created` is called at runtime through a string in a dispatch
table. No reference in the source names it. spanda will not tell you it is
dead, and it will not tell you it is alive either — it tells you where to look.

## What it refuses to guess

The distinction the whole tool is built around is between *nothing references
this* and *I cannot see who references this*. They look identical in the output
of most tools, and only one of them means a symbol is safe to delete.

Four shapes make a caller invisible to any reader of the source, and each is
reported as such rather than as an absence:

- **Framework dispatch.** A decorator hands the symbol to something that will
  call it later — an HTTP route, an ORM event hook, a signal receiver, a
  scheduled job. `spanda/dynamic_dispatch.txt` lists the decorators that mean
  this, one glob per line. It is configuration, not code: `spanda parse` ends
  with a census of every decorator your codebase actually uses, and that census
  is what you grow the file from.
- **Name assembly at runtime.** `getattr(module, name)`, a handler looked up in
  a dict of strings, `importlib.import_module`. Identifier-shaped string
  literals are reported as hints and never become edges.
- **Inheritance from a base the codebase does not contain.** An override of a
  method defined in an installed library is *maybe inherited*, not absent.
- **Attribute access on a value of unknown type.** Where an annotation exists it
  is used; where it does not, the reference is recorded unresolved, with the
  reason attached.

A heuristic stays labelled as a heuristic and is never promoted to an edge in
the graph. If you find a symbol reported with no callers that something really
does call, that is a bug worth filing — it is the exact failure this project
exists to eliminate.

## Commands

| command | what it answers |
|---|---|
| `spanda index <path>` | parse the codebase and store it; safe to re-run |
| `spanda gaps <path>` | what static analysis cannot see here, with reasons |
| `spanda gaps <path> --unreferenced` | ...plus symbols nothing references, split by whether the silence is explained |
| `spanda callers <path> <name>` | what calls this symbol, and what might but cannot be proven to |
| `spanda find <path> "Order*"` | look up symbols by name |
| `spanda parse <path>` | definitions and references per file, without storing anything |
| `spanda parse <path> --out out/` | the same, as one inspectable JSON record per source file |
| `spanda resolve <path> --reasons 3` | link every reference to a definition, listing the failures |
| `spanda imports <path>` | which file each import points at, and what imports circularly |
| `spanda imports <path> --order` | the order files must be processed in |
| `spanda profile <path>` | what the code keeps doing: re-implemented names, annotation rates, churn |
| `spanda loops <path>` | where the loops are, including nesting that spans a function call |
| `spanda drift <path>` | what changed between two scans |
| `spanda backfill <path> --last 10` | replay past commits, so drift has real history to read today |
| `spanda scans <path>` | every run, with its timestamp, commit and fingerprint |
| `spanda vet <path>` | record human decisions in the index, and check them against the newest scan |
| `spanda guide <path> --write` | a note on reading this index, with that index's own numbers in it |

`spanda loops` reads nesting off the call graph, not just out of one file:

```console
$ spanda loops .
LOOPS NESTED IN ONE BODY — for/while/comprehensions, counted syntactically
  2 deep   batch.py:20  pair_up

LOOPS NESTED ACROSS CALLS — a loop calling a function that loops
  3 deep (own 1)   batch.py:30  pair_all_groups   via pair_up

DATABASE CALLS INSIDE LOOPS — matched by name against database_calls.txt
  1 deep   batch.py:44  in load_each:  session.get

NOT SEEN INTO — 5 calls inside loops the resolver could not follow:
       4  attribute_on_unknown_type
       1  builtin
  Whatever those do per iteration is outside this report.
```

Every one of those is a place to look, never a score. spanda does not compute
a complexity number, because a number invites a threshold and a threshold
invites gaming.

## Recording what a person decides

Static analysis eventually runs out. When it does, a human looks at the symbol
and decides — and that decision belongs in the index, not in someone's memory:

```sh
spanda vet . --alive "tasks.py::nightly_cleanup" --note "APScheduler, see config/jobs.py"
spanda vet .
```

Re-running `spanda vet` checks every recorded decision against the newest scan:
which verdicts the code now contradicts, which patterns the alive ones imply
(a decorator vetted alive three times is a line that belongs in
`dynamic_dispatch.txt`), and which symbols are next to look at. `--export` and
`--from` move verdicts between indexes.

## The index

The index lives at `<path>/.spanda/index.db`, inside the codebase it describes
— one authoritative index per codebase. `.spanda/` ignores itself with a
`.gitignore` of `*`: an index is derived data and never belongs in a commit.

- **Re-running is safe.** Symbols keep their identity across scans, matched on
  a deterministic key, so a second scan does not read as everything being
  deleted and re-added.
- **History lives in the index, not in filenames.** `spanda scans` lists every
  run with its timestamp, commit and content fingerprint.
- **Re-indexing is incremental** in a git repository: only what changed is
  re-read, which is the difference between 52 seconds and 12 minutes over 425
  commits.
- **Memory depends on the largest file, not the codebase.** Indexing streams
  one file at a time — 680 files index in 52 MB and about 3 seconds.
- **A syntax error is recorded, not fatal.** The file is marked unparseable
  with the interpreter's message, and the run completes.

Nothing here is async, deliberately: parsing is CPU-bound and never waits, so
async would add machinery and no speed.

## What it does not do

- **No LLM calls, no network, no telemetry.** This is the deterministic layer.
  A description or summarisation layer would consume its output; it does not
  live inside it.
- **Python only**, and no framework-specific parsing — no route extraction, no
  URL maps. Framework knowledge enters as configuration in
  `dynamic_dispatch.txt`, never as code.
- **No type inference.** Annotations are used where the code provides them.
  Where it does not, the reference is reported unresolved rather than guessed.
- **No refactoring, no rewriting.** spanda reads; it never edits your code.
- **No complexity score, no quality grade, no threshold to pass.**

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md)
for the development setup, the rule that the fixture answer key changes before
the tests do, and what the project deliberately will not do.

The single most valuable report is a symbol spanda claims has no callers that
something actually calls.

## License

MIT — see [LICENSE](LICENSE).
