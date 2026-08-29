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
Next: M2, SQLite storage with stable symbol identity.

## Use

Zero dependencies. Any Python 3.11+.

```sh
# What is defined in this codebase, file by file
python -m spanda.cli parse <path>

# The same, dumped as one inspectable JSON record per source file
python -m spanda.cli parse <path> --out out/

# What static analysis cannot see here
python -m spanda.cli gaps <path>

# ...plus symbols nothing references, split by whether the silence is explained
python -m spanda.cli gaps <path> --unreferenced
```

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
