# encoding: utf-8
from __future__ import annotations

from pathlib import Path


def account_profile_dir(*, storage_root: str | Path, platform_code: str, account_id: int) -> Path:
    root = Path(storage_root)
    code = str(platform_code or "stub").strip().lower() or "stub"
    return root / "quote_browser_profiles" / code / str(int(account_id))
