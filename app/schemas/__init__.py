# encoding: utf-8
"""
统一导出 schemas

说明：
- 为避免某次打包/合并时遗漏 OrderFilter 导致启动崩溃，这里对 order 的导入做容错兜底。
"""

# auth
from .auth import *  # noqa

# user
from .user import *  # noqa

# customer/channel/field/finance 等
from .customer_channel import *  # noqa
from .field_config import *  # noqa
from .finance import *  # noqa

# ✅ ai_assistant（报价助手专项）
from .ai_assistant import *  # noqa

# order（带容错）
try:
    from .order import (  # noqa
        OrderCreate,
        OrderUpdate,
        OrderOut,
        OrderListResponse,
        OrderStatusUpdate,
        OrderFilter,
        OrderImageOut,
    )
except ImportError:
    # 兜底：至少保证服务能启动
    from .order import (  # noqa
        OrderCreate,
        OrderUpdate,
        OrderOut,
        OrderListResponse,
        OrderStatusUpdate,
        OrderImageOut,
    )

    class OrderFilter:  # type: ignore
        """Fallback OrderFilter: 仅用于避免启动期 ImportError（不建议依赖此兜底）"""

        pass
