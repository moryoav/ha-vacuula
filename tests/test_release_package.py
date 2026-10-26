"""Verify tagged release contents, reproducibility and package validation."""

import json
import subprocess
import zipfile

import pytest

from tools import build_hacs_release as release


@pytest.fixture
def tagged_repo(tmp_path, monkeypatch):
    """Create a small tagged repository separate from the real checkout."""
    root = tmp_path / "source"
    component = root / "custom_components" / "vacuula"
    component.mkdir(parents=True)
    (component / "__init__.py").write_text('"""Fixture integration."""\n')
    (component / "manifest.json").write_text(
        json.dumps({"domain": "vacuula", "version": "0.1.14"})
    )
    (component / "brand").mkdir()
    (component / "brand" / "NOTICE.txt").write_text("Artwork attribution\n")
    (component / "brand" / "LICENSE-AGPL-3.0.txt").write_text("Artwork license\n")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.1.14\n\n- Fixed schedules.\n\n## 0.1.13\n\n- Older.\n"
    )
    for args in (
        ["init", "--quiet"],
        ["config", "user.name", "Release test"],
        ["config", "user.email", "release-test@example.invalid"],
        ["add", "."],
        ["commit", "--quiet", "-m", "Fixture release"],
        ["tag", "v0.1.14"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    monkeypatch.setattr(release, "ROOT", root)
    return root


def test_package_uses_the_tag_and_preserves_licenses(tagged_repo, tmp_path):
    """Ignore working changes and caches while shipping flat files and notices."""
    component = tagged_repo / "custom_components" / "vacuula"
    (component / "manifest.json").write_text('{"version": "unreleased"}')
    (component / "private.private.txt").write_text("Untracked private fixture")
    (component / "__pycache__").mkdir()
    (component / "__pycache__" / "cache.pyc").write_bytes(b"untracked")
    asset = release.build("v0.1.14", tmp_path / "output")
    with zipfile.ZipFile(asset) as archive:
        assert set(archive.namelist()) == {
            "__init__.py",
            "manifest.json",
            "brand/NOTICE.txt",
            "brand/LICENSE-AGPL-3.0.txt",
        }
        assert json.loads(archive.read("manifest.json"))["version"] == "0.1.14"
        assert archive.read("brand/NOTICE.txt") == b"Artwork attribution\n"
        assert archive.testzip() is None
    assert (asset.parent / "release-notes.md").read_text() == "- Fixed schedules.\n"


def test_package_is_reproducible(tagged_repo, tmp_path):
    """Produce the same archive bytes in different output directories."""
    first = release.build("v0.1.14", tmp_path / "first").read_bytes()
    second = release.build("v0.1.14", tmp_path / "second").read_bytes()
    assert first == second


@pytest.mark.parametrize("tag", ["main", "v0.1", "v0.1.14;echo unsafe", "../v0.1.14"])
def test_package_rejects_nonrelease_refs(tagged_repo, tmp_path, tag):
    """Reject unvalidated refs before running Git or creating an artifact."""
    with pytest.raises(ValueError, match="stable vX.Y.Z"):
        release.build(tag, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_package_rejects_output_inside_the_workspace(tagged_repo):
    """Keep ZIPs outside Git even when the chosen directory does not yet exist."""
    with pytest.raises(ValueError, match="outside the Git workspace"):
        release.build("v0.1.14", tagged_repo / "artifacts")
    assert not (tagged_repo / "artifacts").exists()


def test_package_rejects_a_mismatched_tag_version(tagged_repo, tmp_path):
    """Refuse to label an older manifest as a newer release."""
    subprocess.run(["git", "tag", "v0.1.15"], cwd=tagged_repo, check=True)
    with pytest.raises(ValueError, match="does not match"):
        release.build("v0.1.15", tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "text", ["## 0.1.13\n- Older.\n", "## 0.1.14\n\n## 0.1.13\n- Older.\n"]
)
def test_changelog_requires_the_requested_release_notes(text):
    """Reject missing or empty notes instead of publishing another release's text."""
    with pytest.raises(ValueError, match="no release notes"):
        release.changelog_notes(text, "0.1.14")
