# Contributing

Bug reports and pull requests are welcome.

The most valuable report this project can receive is a symbol it claims has no
callers that something actually calls. That is the exact failure the tool
exists to prevent, and every such case belongs in `fixtures/sample_pkg/` as a
new file that breaks one specific thing.

## Getting set up

Dependencies are managed with [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/pr0digy91/spanda
cd spanda
uv sync --all-extras       # pytest + ruff; there are no runtime dependencies
uv run spanda --help
```

`uv.lock` is committed and should be kept in step with `pyproject.toml` — run
`uv lock` after changing dependencies and include the result in your PR.

**Run it on the newest interpreter you have.** A parser cannot read syntax from
a release later than its own, so spanda indexing a codebase with a Python older
than that codebase's syntax will record files as syntax errors that are
perfectly valid. `.python-version` pins 3.14 for that reason. 3.9 is the
supported floor, not the recommended version.

## Tests

```sh
uv run python -m pytest tests/ -q

# a specific Python version, as CI does
UV_PYTHON=3.9 uv sync --frozen --all-extras
UV_PYTHON=3.9 uv run --frozen python -m pytest tests/ -q
```

Note that `UV_PYTHON` must be set for `uv run` as well as `uv sync` — on its
own `uv run` re-syncs to `.python-version` and quietly tests the wrong version.

### The fixture is the answer key

`fixtures/README.md` is not documentation of the fixture; it is the specification
the tests are transcribed from. Every count, every qualname, every decorator in
that table was worked out by reading the 21 files by hand. If the tests and that
document ever disagree, one of them is wrong, and finding out which is the whole
purpose of having it.

So: **change the answer key first**, then the tests, then the code. A change
that updates a test to match new output without a corresponding line in
`fixtures/README.md` explaining why the new output is correct is a change that
has quietly moved the goalposts.

`fixtures/sample_pkg/` is parsed and never imported — it contains a real
circular import and a file that does not parse, both deliberate. `pyproject.toml`
excludes it from pytest collection and from ruff for that reason.

### Golden files

`tests/golden/` holds frozen extractor output, one JSON record per source file.
Regenerate it deliberately, never to make a failing test pass:

```sh
uv run python -m spanda.cli parse fixtures --out tests/golden
```

Then read the diff. A golden diff is a change in what the tool says about code,
which is the thing under test.

### Output must be identical on every supported interpreter

An index built on one Python and read on another has to mean the same thing, so
CI runs the suite on 3.9, 3.11, 3.12 and 3.14 and diffs the extractor output
against the goldens.

The trap is real and has been hit once: `_body_hash` walks AST fields, and 3.12
added `type_params` to `FunctionDef` and `ClassDef`, so every function and class
hashed differently either side of that release. If you add anything that walks
`ast.iter_fields`, or hashes an interpreter-provided string, assume it varies by
version until CI says otherwise.

## Linting

```sh
uv run ruff check .
```

## Style

Match what is already there.

- **No runtime dependencies.** Anything that would add one has to earn it
  explicitly, in the PR description.
- **No LLM calls, ever, anywhere in this package.** A description layer would
  consume this engine's output; it does not live inside it.
- **Comments explain why, not what.** Most of the comments in this codebase
  record a wrong answer that a real codebase produced, and would look arbitrary
  without that. Keep them that way — and keep them repo-agnostic: no client or
  employer names, no statistics that identify a private codebase.
- **Every module says its job in its first line**, and does that one thing. The
  table in the README is the contract; a PR that blurs two modules together
  should say why.
- **Say "I cannot see" rather than guessing.** A new heuristic must be labelled
  as one, and must never become an edge in the graph. Reporting "0 callers" for
  something the framework calls is the failure mode this project was built to
  eliminate — a confident wrong answer is worse than no answer.

Please add a test with any behaviour change, and add the case to
`fixtures/sample_pkg/` if it is a shape the extractor could get wrong.
