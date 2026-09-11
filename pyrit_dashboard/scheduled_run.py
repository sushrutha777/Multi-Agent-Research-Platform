"""One-shot entry point for the weekly EventBridge/ECS red-team task."""

from __future__ import annotations

import asyncio

from main import _run_selected, ATTACK_CONFIGS


if __name__ == "__main__":
    asyncio.run(_run_selected(list(ATTACK_CONFIGS)))
