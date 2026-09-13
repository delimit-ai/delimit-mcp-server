"""Bounded Free-core upgrade recovery; synthetic state only, no providers."""

import hashlib
import json
import pytest

from ai import session_continuity as sc


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    nested = repo / "packages" / "api"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    store = tmp_path / "souls"
    monkeypatch.setattr(sc, "SOULS_BASE_DIR", store)
    old_hash = hashlib.sha256(str(nested.resolve()).encode()).hexdigest()[:12]
    old_file = store / old_hash / "latest.json"
    old_file.parent.mkdir(parents=True)
    saved = {"soul_id": "legacy123", "project_path": str(nested.resolve()),
             "active_task": "Preserve completed work", "decisions": ["No repeat send"]}
    old_file.write_text(json.dumps(saved))
    return repo, nested, old_file, saved


@pytest.mark.parametrize("implicit", [False, True])
def test_exact_nested_path_recovers_without_mutating_saved_state(legacy, monkeypatch, implicit):
    repo, nested, old_file, saved = legacy
    before = old_file.read_bytes()
    monkeypatch.chdir(nested)
    result = sc.revive_basic("" if implicit else str(nested))
    assert result["status"] == "revived"
    assert result["legacy_path_recovery"] is True
    assert result["soul"] == saved
    assert old_file.read_bytes() == before
    assert not (sc._project_dir(str(repo)) / "latest.json").exists()


def test_canonical_latest_takes_precedence(legacy):
    repo, nested, _, _ = legacy
    current = sc.capture_soul_core(project_path=str(repo), active_task="Current work")
    result = sc.revive_basic(str(nested))
    assert result["soul"]["soul_id"] == current.soul_id
    assert "legacy_path_recovery" not in result


@pytest.mark.parametrize("recorded_path", ["other_repo", "sibling", "relative", "missing"])
def test_legacy_embedded_identity_must_match_exact_requested_path(legacy, recorded_path):
    repo, nested, old_file, saved = legacy
    saved["project_path"] = {
        "other_repo": str(repo.parent / "other-repo"),
        "sibling": str(repo / "packages" / "other"),
        "relative": "packages/api", "missing": None,
    }[recorded_path]
    old_file.write_text(json.dumps(saved))
    assert sc.revive_basic(str(nested))["status"] == "not_found"


def test_root_or_other_project_does_not_scan_for_old_subdirectory_state(legacy):
    repo, _, _, _ = legacy
    assert sc.revive_basic(str(repo))["status"] == "not_found"
    assert sc.revive_basic(str(repo.parent / "other-repo"))["status"] == "not_found"


def test_legacy_symlink_is_not_followed(legacy):
    _, nested, old_file, _ = legacy
    target = old_file.with_name("preserved.json")
    old_file.rename(target)
    old_file.symlink_to(target)
    assert sc.revive_basic(str(nested))["status"] == "not_found"
