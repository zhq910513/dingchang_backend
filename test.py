# test.py
# 目的：打印当前本地代码的 git commit + Base.metadata 中的表/列清单（用于 Step2：Model 真实字段快照）
from __future__ import annotations

import subprocess
import sys


def _git_short_head() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="replace").strip()
    except Exception as e:
        return f"<git_head_unavailable: {e}>"


def main() -> int:
    print(_git_short_head())
    try:
        from app.core.db import Base  # noqa
        import app.models.order  # noqa
        import app.models.order_info  # noqa
        import app.models.image_file  # noqa
        import app.models.ocr_task  # noqa
        import app.models.image_ocr_result  # noqa
        import app.models.ocr_image_cache  # noqa
        import app.models.finance  # noqa
    except Exception as e:
        print("IMPORT_ERROR:", repr(e))
        return 2

    print("===TABLES===")
    for t in Base.metadata.sorted_tables:
        print(t.name, [c.name for c in t.columns])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())