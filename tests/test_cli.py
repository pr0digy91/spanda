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
