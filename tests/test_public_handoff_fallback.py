"""No installed private Phoenix module is needed for these regression tests."""
import builtins
from unittest import mock

import pytest
from ai import ledger_manager as lm
from ai import session_continuity as sc


@pytest.mark.parametrize("dependency_error", [False, True])
def test_broken_phoenix_is_not_silently_replaced_by_free_capture(tmp_path, monkeypatch, dependency_error):
    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "ai.session_phoenix":
            if dependency_error:
                raise ModuleNotFoundError("synthetic dependency missing", name="synthetic_dependency")
            raise ImportError("synthetic incompatible Phoenix interface")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    monkeypatch.setattr(lm, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sc, "SOULS_BASE_DIR", tmp_path / "souls")
    with mock.patch.object(sc, "capture_soul_core") as fallback:
        result = lm.session_handoff(summary="synthetic public handoff", project_path=str(tmp_path))
    assert result["saved"]
    assert result["soul_refresh_status"] == "failed"
    assert "soul_id" not in result
    fallback.assert_not_called()
