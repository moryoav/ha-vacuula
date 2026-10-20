"""Require nonempty docstrings on every Python definition in the repository.

The audit includes modules, classes, functions, methods, constructors and nested
callbacks in integration code, tests and tools. Only generated, cache, virtual
environment and version-control directories are excluded. AST inspection avoids
importing project code or contacting Home Assistant or a robot. Symbolic links
are rejected so a linked source directory cannot silently escape the audit.
"""

import argparse
import ast
import os
import sys
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "venv",
    }
)
DOCUMENTABLE_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True, slots=True)
class DocstringReport:
    """Count documented definitions and retain locations of every missing entry."""

    files: int
    total: int
    documented: int
    missing: tuple[str, ...]

    @property
    def percentage(self) -> float:
        """Return presence coverage, treating an empty scan as zero coverage."""
        return 100 * self.documented / self.total if self.total else 0.0


def _raise_walk_error(error: OSError) -> None:
    """Fail the audit when a directory cannot be read instead of skipping it."""
    raise error


def python_files(root: Path) -> Iterator[Path]:
    """Find Python source recursively, excluding only named artifact directories."""
    if not root.is_dir():
        raise NotADirectoryError(root)
    for directory, subdirectories, files in os.walk(root, onerror=_raise_walk_error):
        subdirectories[:] = sorted(
            name for name in subdirectories if name not in EXCLUDED_DIRECTORIES
        )
        for name in subdirectories:
            path = Path(directory) / name
            if path.is_symlink():
                raise OSError(f"Cannot audit symbolic-link directory: {path}")
        for name in sorted(files):
            if name.endswith(".py"):
                path = Path(directory) / name
                if path.is_symlink():
                    raise OSError(f"Cannot audit symbolic-link source: {path}")
                yield path


def audit_docstrings(root: Path) -> DocstringReport:
    """Count nonempty module and definition docstrings without importing source."""
    files = total = documented = 0
    missing: list[str] = []
    for path in python_files(root):
        with tokenize.open(path) as source:
            tree = ast.parse(source.read(), filename=str(path))
        files += 1
        for node in ast.walk(tree):
            if not isinstance(node, DOCUMENTABLE_NODES):
                continue
            total += 1
            if (ast.get_docstring(node) or "").strip():
                documented += 1
                continue
            name = getattr(node, "name", "<module>")
            line = getattr(node, "lineno", 1)
            missing.append(f"{path.relative_to(root)}:{line}: {name}")
    return DocstringReport(files, total, documented, tuple(sorted(missing)))


def main(argv: list[str] | None = None) -> int:
    """Print the audit and fail unless every definition has a nonempty docstring."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root; defaults to the parent of this script's directory",
    )
    arguments = parser.parse_args(argv)
    try:
        report = audit_docstrings(arguments.root)
    except (OSError, SyntaxError, UnicodeError) as error:
        print(f"Docstring audit failed: {error}", file=sys.stderr)
        return 2
    for location in report.missing:
        print(f"Missing docstring: {location}")
    print(
        f"Docstring coverage: {report.percentage:.2f}% "
        f"({report.documented}/{report.total} definitions in {report.files} files)"
    )
    return 0 if report.total and not report.missing else 1


if __name__ == "__main__":
    sys.exit(main())
