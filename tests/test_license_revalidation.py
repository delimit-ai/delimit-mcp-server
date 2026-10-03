"""Exercise the shipped Python fallback even when a native core is installed."""
import builtins
import importlib.util
from pathlib import Path
import urllib.error
from unittest.mock import patch

import pytest


@pytest.fixture
def license_fallback():
    real_import = builtins.__import__

    def without_native(name, *args, **kwargs):
        if name == "ai.license_core":
            raise ImportError("force the shipped fallback for regression coverage")
        return real_import(name, *args, **kwargs)

    source = Path(__file__).resolve().parents[1] / "gateway" / "ai" / "license.py"
    spec = importlib.util.spec_from_file_location("license_fallback_under_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch("builtins.__import__", side_effect=without_native):
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("status, expected", [
    (200, False), (204, False), (404, False),
    (429, None), (500, None), (503, None),
])
def test_valid_false_classification(license_fallback, status, expected):
    assert license_fallback._classify_ls_response(status, b'{"valid": false}') is expected


def test_network_error_is_unavailable(license_fallback):
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        assert license_fallback._ls_validate("synthetic-key", "synthetic-machine") == (None, None)


@pytest.mark.parametrize("verdict, expected_status, expected_valid", [
    (False, "expired", False), (None, "grace", True),
])
def test_only_unavailable_revalidation_gets_grace(
    license_fallback, tmp_path, monkeypatch, verdict, expected_status, expected_valid
):
    monkeypatch.setattr(license_fallback, "LICENSE_FILE", tmp_path / "license.json")
    monkeypatch.setattr(license_fallback.time, "time", lambda: 100 * 86400)
    monkeypatch.setattr(license_fallback, "_ls_validate", lambda *args: (verdict, 404 if verdict is False else 503))
    data = {"key": "synthetic-key", "tier": "pro", "valid": True,
            "last_validated_at": 69 * 86400}
    result = license_fallback.revalidate_license(data)
    assert result["status"] == expected_status
    assert result["updated_data"]["valid"] is expected_valid
