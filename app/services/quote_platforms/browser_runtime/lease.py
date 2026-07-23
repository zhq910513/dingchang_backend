# encoding: utf-8
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class BrowserLease:
    platform_code: str
    account_id: int
    container_name: str
    image: str
    cdp_url: str
    http_url: str
    profile_dir: Path
    artifact_dir: Path
    internal_port: int
    host_port: int
    reused: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_safe_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["profile_dir"] = str(self.profile_dir)
        data["artifact_dir"] = str(self.artifact_dir)
        return data

    @property
    def playwright_connect_url(self) -> str:
        return self.cdp_url

    @property
    def browser_profile_path(self) -> str:
        return str(self.profile_dir)

    @property
    def login_artifact_path(self) -> str:
        return str(self.artifact_dir)

