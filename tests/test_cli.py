"""The command line itself: the parts with no module of their own."""

import pytest

from spanda import __version__
from spanda.cli import main


def test_version_flag_reports_the_package_version(capsys):
    """`--version` is the first thing a bug report is asked for, so it has to
    work without a subcommand — which the parser otherwise requires."""
    for flag in ("--version", "-V"):
        with pytest.raises(SystemExit) as exit_info:
            main([flag])
        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith(f"spanda {__version__} ")
        # The interpreter is in there too: a parser cannot read syntax newer
        # than its own release, so it is half the answer to "why is this file
        # reported as broken".
        assert "Python" in out


def test_version_matches_the_installed_distribution():
    """`__init__.py` is the single source of truth and `pyproject.toml` reads
    it. If that wiring breaks, an installed spanda reports one version and its
    metadata another."""
    metadata = pytest.importorskip("importlib.metadata")
    try:
        installed = metadata.version("spanda-graph")
    except metadata.PackageNotFoundError:
        pytest.skip("not installed; running from a bare checkout")
    assert installed == __version__


def _fixture_index(tmp_path, monkeypatch):
    """Index the fixtures into a copy under tmp_path and chdir into it."""
    import shutil
    from pathlib import Path
    root = tmp_path / "fixtures"
    shutil.copytree(Path(__file__).parent.parent / "fixtures", root)
    monkeypatch.chdir(root)
    assert main(["index"]) == 0
    assert main(["index"]) == 0
    return root


def test_every_command_defaults_to_the_current_directory(tmp_path, monkeypatch, capsys):
    """`spanda find slugify` from inside a repo, no path — the shape a person
    types first, and the one the guide's own examples use."""
    _fixture_index(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["find", "slugify"]) == 0
    assert "sample_pkg/helpers.py" in capsys.readouterr().out
    assert main(["callers", "slugify"]) == 0
    assert "slugify" in capsys.readouterr().out


def test_drift_takes_two_scan_numbers_without_a_path(tmp_path, monkeypatch, capsys):
    """`spanda drift 1 2`: the first number would land in the optional path
    argument; it is a scan number unless a directory of that name exists."""
    _fixture_index(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["drift", "1", "2"]) == 0
    bare = capsys.readouterr().out
    assert main(["drift", ".", "1", "2"]) == 0
    with_path = capsys.readouterr().out
    assert bare.splitlines()[0] == with_path.splitlines()[0]
    assert bare.splitlines()[0].startswith("scan 1 ")


def test_guide_writes_the_readme_by_default(tmp_path, monkeypatch, capsys):
    root = _fixture_index(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["guide"]) == 0
    readme = root / ".spanda" / "README.md"
    assert readme.exists()
    text = readme.read_text()
    # commands in the guide are written as run from the repo root: no folder
    # name where a path belongs, which is the mistake a copied example causes
    assert "spanda vet --alive" in text
    assert f"spanda vet {root.name}" not in text
    assert main(["guide", "--print"]) == 0
    assert "# Reading the fixtures index" in capsys.readouterr().out
