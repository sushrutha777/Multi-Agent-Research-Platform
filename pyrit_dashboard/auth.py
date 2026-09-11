from __future__ import annotations

import hmac


def is_valid_dashboard_key(provided: str, expected: str, required: bool = False) -> bool:
    if required and not expected:
        return False
    if not expected:
        return True
    return hmac.compare_digest(provided, expected)
