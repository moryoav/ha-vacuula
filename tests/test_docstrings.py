"""Enforce repository-wide docstrings and verify the audit cannot silently pass gaps."""

from pathlib import Path

import pytest

from tools.check_docstrings import audit_docstrings, main

COMPLETE_SOURCE = '''"""A documented sample module."""

class Example:
    """Exercise class, constructor, property and private-method documentation."""

    def __init__(self):
        """Initialize the sample without performing external work."""
        pass

    @property
    def value(self):
        """Return the fixed example value."""
        return 1

    async def _operation(self):
        """Exercise asynchronous methods with a documented nested callback."""
        def callback():
            """Return the callback result without touching a real device."""
            return 1
        return callback()
'''


def test_project_has_complete_docstrings() -> None:
    """Require docstrings on every Python definition, including this test."""
    report = audit_docstrings(Path(__file__).resolve().parents[1])
    assert report.total > 0
    assert not report.missing, "Missing docstrings:\n" + "\n".join(report.missing)
    assert report.documented == report.total


def test_audit_includes_every_definition(tmp_path: Path) -> None:
    """Count modules, classes, constructors, properties, async methods and callbacks."""
    (tmp_path / "sample.py").write_text(COMPLETE_SOURCE, encoding="utf-8")
    report = audit_docstrings(tmp_path)
    assert (report.files, report.documented, report.total) == (1, 6, 6)
    assert report.percentage == 100
    assert report.missing == ()
    assert main([str(tmp_path)]) == 0


@pytest.mark.parametrize(
    "docstring",
    [
        '"""A documented sample module."""',
        '"""Exercise class, constructor, property and private-method documentation."""',
        '"""Initialize the sample without performing external work."""',
        '"""Return the fixed example value."""',
        '"""Exercise asynchronous methods with a documented nested callback."""',
        '"""Return the callback result without touching a real device."""',
    ],
)
def test_missing_docstring_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], docstring: str
) -> None:
    """Fail for a missing docstring at every supported kind of definition."""
    source = COMPLETE_SOURCE.replace(docstring, "# Missing documentation")
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    report = audit_docstrings(tmp_path)
    assert (report.documented, report.total) == (5, 6)
    assert len(report.missing) == 1
    assert "sample.py:" in report.missing[0]
    assert main([str(tmp_path)]) == 1
    assert "Missing docstring: sample.py:" in capsys.readouterr().out


def test_whitespace_docstring_fails(tmp_path: Path) -> None:
    """Reject a whitespace-only string rather than counting it as documentation."""
    (tmp_path / "empty.py").write_text('"""   """\n', encoding="utf-8")
    report = audit_docstrings(tmp_path)
    assert report.documented == 0
    assert report.total == 1
    assert main([str(tmp_path)]) == 1


def test_unlisted_source_is_audited(tmp_path: Path) -> None:
    """Discover a new Python file outside known integration, test and tool paths."""
    directory = tmp_path / "future_package"
    directory.mkdir()
    (directory / "new.py").write_text("value = 1\n", encoding="utf-8")
    assert len(audit_docstrings(tmp_path).missing) == 1
    assert main([str(tmp_path)]) == 1


def test_generated_files_are_excluded(tmp_path: Path) -> None:
    """Skip virtual environments and caches without skipping project source."""
    (tmp_path / "sample.py").write_text(COMPLETE_SOURCE, encoding="utf-8")
    for name in (".venv", ".git", "__pycache__", "build", "dist"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "generated.py").write_text("invalid source (", encoding="utf-8")
    assert audit_docstrings(tmp_path).files == 1
    assert main([str(tmp_path)]) == 0


@pytest.mark.parametrize("source", ["def broken(", "if True print('bad')"])
def test_invalid_source_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    """Return an audit error for invalid Python rather than reporting full coverage."""
    (tmp_path / "broken.py").write_text(source, encoding="utf-8")
    assert main([str(tmp_path)]) == 2
    assert "Docstring audit failed:" in capsys.readouterr().err


def test_empty_root_fails(tmp_path: Path) -> None:
    """Prevent a wrong or empty scan directory from producing a successful check."""
    assert audit_docstrings(tmp_path).percentage == 0
    assert main([str(tmp_path)]) == 1


def test_missing_root_fails(tmp_path: Path) -> None:
    """Report an inaccessible root as an audit error instead of an empty success."""
    assert main([str(tmp_path / "missing")]) == 2


@pytest.mark.parametrize("is_directory", [True, False])
def test_linked_source_fails(tmp_path: Path, is_directory: bool) -> None:
    """Reject linked source files and packages instead of silently skipping them."""
    root = tmp_path / "project"
    root.mkdir()
    target = tmp_path / "external.py"
    if is_directory:
        target.mkdir()
        (target / "missing.py").write_text("value = 1\n", encoding="utf-8")
    else:
        target.write_text("value = 1\n", encoding="utf-8")
    link = root / "linked.py"
    link.symlink_to(target, target_is_directory=is_directory)
    assert main([str(root)]) == 2
