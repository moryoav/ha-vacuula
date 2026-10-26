"""Build a reproducible HACS ZIP and release notes from an existing Git tag."""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "vacuula"
ASSET_NAME = "vacuula.zip"
TAG_PATTERN = re.compile(r"v\d+\.\d+\.\d+")
PREFIX = f"custom_components/{DOMAIN}/"


def changelog_notes(text: str, version: str) -> str:
    """Extract only the requested version's nonempty changelog section."""
    heading = re.compile(rf"^## {re.escape(version)}(?:\s+-\s+.*)?$")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not heading.fullmatch(line):
            continue
        end = next(
            (i for i in range(index + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        notes = "\n".join(lines[index + 1 : end]).strip()
        if notes:
            return notes + "\n"
        break
    raise ValueError(f"CHANGELOG.md has no release notes for {version}")


def build(tag: str, output_dir: Path) -> Path:
    """Package tagged integration files at the ZIP root, outside the repository."""
    if TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError("Expected a stable vX.Y.Z release tag")
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(ROOT.resolve()):
        raise ValueError("Release output must be outside the Git workspace")
    result = subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            f"refs/tags/{tag}",
            f"custom_components/{DOMAIN}",
            "CHANGELOG.md",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    with zipfile.ZipFile(io.BytesIO(result.stdout)) as source:
        files = {
            info.filename.removeprefix(PREFIX): source.read(info)
            for info in source.infolist()
            if info.filename.startswith(PREFIX) and not info.is_dir()
        }
        if not {"__init__.py", "manifest.json"}.issubset(files):
            raise ValueError("Tagged integration is missing required files")
        manifest = json.loads(files["manifest.json"])
        if manifest.get("domain") != DOMAIN or manifest.get("version") != tag[1:]:
            raise ValueError("Tagged manifest domain or version does not match the tag")
        notes = changelog_notes(source.read("CHANGELOG.md").decode("utf-8"), tag[1:])
    output_dir.mkdir(parents=True, exist_ok=True)
    asset = output_dir / ASSET_NAME
    with zipfile.ZipFile(asset, "w") as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    with zipfile.ZipFile(asset) as archive:
        if archive.testzip() is not None:
            raise ValueError("Release ZIP integrity check failed")
    (output_dir / "release-notes.md").write_text(notes, encoding="utf-8")
    return asset


def main() -> int:
    """Build a tagged release from the command line with readable failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Existing stable vX.Y.Z release tag")
    parser.add_argument("output_dir", type=Path, help="Output outside the repository")
    args = parser.parse_args()
    try:
        print(build(args.tag, args.output_dir))
    except (
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as err:
        print(f"HACS release build failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
