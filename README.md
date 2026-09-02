<div align="center">

<img src="https://raw.githubusercontent.com/pr0digy91/spanda/main/docs/banner.png" alt="spanda" width="100%">

# spanda

### Find out who calls a function — and be told, plainly, when nobody can know.

**Deterministic static analysis for Python that reports its own blind spot, instead of reporting zero.**

[![CI](https://github.com/pr0digy91/spanda/actions/workflows/ci.yml/badge.svg)](https://github.com/pr0digy91/spanda/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/spanda-graph?color=blue)](https://pypi.org/project/spanda-graph/)
[![Python](https://img.shields.io/pypi/pyversions/spanda-graph)](https://pypi.org/project/spanda-graph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)

```sh
uv tool install spanda-graph
```

</div>

---

Every static analyser has a blind spot. Ask most of them who calls an event
hook, a route handler, or anything reached through `getattr`, and you get
`0 callers` — which reads as *safe to delete*. It isn't. It means the tool
couldn't see.

spanda reports that difference:

```console
$ spanda callers . _apply_rls_context

models.py:57  function _apply_rls_context

  no static callers found

  ...but this symbol is dispatched at runtime. Whatever calls it is not
     visible in the source, so the count above is not the whole story.
```

That is the whole idea. Everything else is in service of it.

No LLM calls, no network, no telemetry. Zero runtime dependencies — standard
library only. Same input, same output, every time.

## Install

```sh
uv tool install spanda-graph      # recommended: a standalone `spanda` on your PATH
pip install spanda-graph          # or into the current environment
```

The published distribution is `spanda-graph`; the command and the import
package are both `spanda`.

<details>
<summary>Other ways to install</summary>

```sh
uvx --from spanda-graph spanda gaps .        # try it without installing anything
uv add spanda-graph                          # as a project dependency
uv tool install git+https://github.com/pr0digy91/spanda    # from source, no release needed
pip install git+https://github.com/pr0digy91/spanda
```

To hack on it:

```sh
git clone https://github.com/pr0digy91/spanda && cd spanda
uv sync --all-extras && uv run spanda --help
```
</details>

> **Run it on the newest Python you have.** A parser cannot read syntax from a
> release later than its own, so an old interpreter records valid files as
> syntax errors. 3.9 is the supported floor, not the version to use — and on
> macOS the built-in `python3` is usually well behind. `uv tool install` and
> `uvx` fetch a current interpreter for you.

## Using it, in order

### 1. Index the codebase

```sh
spanda index .
```

Parses everything, stores it at `.spanda/index.db` inside the codebase itself,
and audits its own work: every name brought in by an import is traced to its
definition, and anything it could not place is reported rather than dropped.
Safe to re-run — symbols keep their identity across scans.

### 2. Find out what it cannot see

```sh
spanda gaps .
```

**Read this before you trust anything else.** It is the map of the blind spot,
grouped by *why* each symbol is invisible:

```console
Decorated with something that dispatches at runtime — the framework calls these,
  and no reference in this codebase names them:

  models.py:57
      _apply_rls_context
      @event.listens_for(Session, 'before_flush')
  (4)

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
      "on_created" names a symbol defined at handlers.py:9
  (3)
```

That last group is the one that makes dead-code detection dangerous:
`handlers.on_created` is called at runtime through a string in a dispatch
table. Nothing in the source names it. spanda will not call it dead, and will
not call it alive — it tells you where to look.

Add `--unreferenced` to also list symbols nothing references, split by whether
the silence is explained.

### 3. Ask about a specific symbol

```sh
spanda callers . create_invoice
```

Gives you the callers it can prove, plus anything that might call it but cannot
be proven to — with the reason attached, as in the example at the top.

### 4. Record what a person decides

Static analysis eventually runs out. When it does, someone looks at the symbol
and decides — and that decision belongs in the index, not in memory:

```sh
spanda vet . --alive "tasks.py::nightly_cleanup" --note "APScheduler, see config/jobs.py"
spanda vet .
```

Re-running `spanda vet` checks every recorded decision against the newest scan:
which verdicts the code now contradicts, which patterns the alive ones imply (a
decorator vetted alive three times belongs in `dynamic_dispatch.txt`), and what
to look at next. `--export` and `--from` move verdicts between indexes.

## What it refuses to guess

Four shapes make a caller invisible to any reader of the source. Each is
reported as such, never as an absence:

| shape | example | what spanda says |
|---|---|---|
| **Framework dispatch** | `@app.post(...)`, `@event.listens_for(...)` | dispatched at runtime; callers not in the source |
| **Runtime name assembly** | `getattr(mod, name)`, a dict of handler strings | the call site is certain, the target is not |
| **Inheritance from an absent base** | overriding a method from an installed library | maybe inherited, not absent |
| **Attribute on an unknown type** | `x.method()` where `x` has no annotation | unresolved, with the reason attached |

A heuristic stays labelled a heuristic and never becomes an edge in the graph.
Framework knowledge lives in `dynamic_dispatch.txt` as configuration, one glob
per line — and `spanda parse` ends with a census of every decorator your
codebase actually uses, so you grow that file from evidence rather than memory.

**If you find a symbol reported with no callers that something really does
call, please [file it](https://github.com/pr0digy91/spanda/issues/new/choose).**
That is the exact failure this project exists to eliminate.

## Other questions it answers

| command | what it answers |
|---|---|
| `spanda loops .` | where the loops are — including nesting that spans a function call, and database calls inside them |
| `spanda profile .` | what the code keeps doing: re-implemented names, annotation rates, churn |
| `spanda drift .` | what changed between two scans |
| `spanda backfill . --last 10` | replay past commits, so drift has real history to read today |
| `spanda imports .` | which file each import points at, and what imports circularly |
| `spanda find . "Order*"` | look up symbols by name |
| `spanda scans .` | every run, with its timestamp, commit and fingerprint |
| `spanda guide . --write` | a note on reading this index, with that index's own numbers in it |
| `spanda parse . --out out/` | one inspectable JSON record per source file, storing nothing |
| `spanda resolve . --reasons 3` | link every reference to a definition, listing the failures |

`spanda loops` reads nesting off the call graph, not just out of one file — a
one-loop function called from another loop is three deep, and no single file
shows that. Every line is a place to look, never a score: spanda computes no
complexity number, because a number invites a threshold and a threshold invites
gaming.

## What it does not do

- **No LLM calls, no network, no telemetry.** This is the deterministic layer.
  A description layer would consume its output, not live inside it.
- **Python only**, and no framework-specific parsing. Framework knowledge
  enters as configuration, never as code.
- **No type inference.** Annotations are used where the code provides them;
  where it does not, the reference is reported unresolved rather than guessed.
- **No refactoring.** spanda reads; it never edits your code.
- **No quality score or grade.**

<details>
<summary>Notes on the index</summary>

- Lives at `<path>/.spanda/index.db`, inside the codebase it describes — one
  authoritative index per codebase. `.spanda/` ignores itself with a
  `.gitignore` of `*`: an index is derived data and never belongs in a commit.
- **Re-indexing is incremental** in a git repository — only what changed is
  re-read, which is the difference between 52 seconds and 12 minutes over 425
  commits.
- **Memory depends on the largest file, not the codebase.** Indexing streams
  one file at a time: 680 files index in 52 MB and about 3 seconds.
- **A syntax error is recorded, not fatal.** The file is marked unparseable
  with the interpreter's message, and the run completes.
- History lives in the index, not in filenames. `spanda scans` lists every run
  with its timestamp, commit and content fingerprint.
- Nothing is async, deliberately: parsing is CPU-bound and never waits, so
  async would add machinery and no speed.
</details>

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
