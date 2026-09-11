from __future__ import annotations

from pyrit_dashboard.auth import is_valid_dashboard_key


def test_pyrit_dashboard_authentication():
    assert is_valid_dashboard_key("secret", "secret") is True
    assert is_valid_dashboard_key("wrong", "secret") is False
    assert is_valid_dashboard_key("", "") is True
    assert is_valid_dashboard_key("", "", required=True) is False
