# encoding: utf-8
"""
全局常量定义

说明：
- 订单/结算等枚举仅作为展示/统计文案使用，核心业务使用布尔字段：
  * is_finished: 是否完成
  * is_rebate: 是否返点
  * is_paid: 是否回款
"""

# ------------------------
# 订单 / 结算状态（仅展示用）
# ------------------------

# 订单状态：0=未完成 1=已完成
ORDER_STATUS = {
    0: "未完成",
    1: "已完成",
}

# 通用结算状态
SETTLE_STATUS = {
    0: "未结算",
    1: "已结算",
}

# 客户结算状态
CUSTOMER_SETTLE_STATUS = {
    0: "客户未结算",
    1: "客户已结算",
}

# 渠道结算状态
CHANNEL_SETTLE_STATUS = {
    0: "渠道未结算",
    1: "渠道已结算",
}

# ------------------------
# 账号角色
# ------------------------

ROLE_SUPER_ADMIN = "super_admin"
ROLE_MANAGER = "manager"
ROLE_SALES = "sales"
ROLE_FINANCE = "finance"

ROLE_ALL = (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_SALES,
    ROLE_FINANCE,
)

# ✅ 去兼容版：按你现在的业务规则
# - 超级账号 -> 可创建 经理 / 业务 / 财务
# - 经理账号 -> 可创建 业务 / 财务
ROLE_CHILD_CREATABLE_MAP = {
    ROLE_SUPER_ADMIN: (ROLE_MANAGER, ROLE_SALES, ROLE_FINANCE),
    ROLE_MANAGER: (ROLE_SALES, ROLE_FINANCE),
}

ROLE_LABEL_MAP = {
    ROLE_SUPER_ADMIN: "超级账号",
    ROLE_MANAGER: "经理账号",
    ROLE_SALES: "业务账号",
    ROLE_FINANCE: "财务账号",
}

__all__ = [
    "ORDER_STATUS",
    "SETTLE_STATUS",
    "CUSTOMER_SETTLE_STATUS",
    "CHANNEL_SETTLE_STATUS",
    "ROLE_SUPER_ADMIN",
    "ROLE_MANAGER",
    "ROLE_SALES",
    "ROLE_FINANCE",
    "ROLE_ALL",
    "ROLE_CHILD_CREATABLE_MAP",
    "ROLE_LABEL_MAP",
]
