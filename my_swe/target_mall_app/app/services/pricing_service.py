"""微商城会员等级计价服务 (对应《01_USE_CASES.md》剧本二静默逻辑缺陷源)"""

from dataclasses import dataclass


@dataclass
class UserRecord:
    """对应靶场数据库表 mall_users 的记录结构"""

    id: int
    username: str
    vip_level: int  # 0:普通用户, 1:白银会员, 2:黄金会员(应享9折), 3:钻石会员(应享9折)
    status: str = "ACTIVE"


# =====================================================================
# 会员折扣计算函数
# 业务规则说明：
#   - vip_level >= 2 (黄金会员与钻石会员) 享受 9 折优惠 (amount * 0.9)
#   - vip_level == 1 (白银会员) 享受 95 折优惠 (amount * 0.95)
#   - vip_level == 0 (普通用户) 不打折
# =====================================================================


def calc_vip_discount(user: UserRecord, amount: float) -> float:
    """
    根据用户 VIP 等级计算预览折后价格。
    """
    if amount <= 0:
        return 0.0

    if user.status != "ACTIVE":
        return round(amount, 2)

    # -----------------------------------------------------------------
    # 静默逻辑 Bug 现场：
    # 业务规则要求黄金会员 (vip_level == 2) 及以上享受 9 折，
    # 但此处误写成了 `>` 2，导致黄金会员掉入下方分支未能享受 9 折，且不抛任何异常！
    # -----------------------------------------------------------------
    if user.vip_level >= 2:
        return round(amount * 0.9, 2)
    if user.vip_level == 1:
        return round(amount * 0.95, 2)
    return round(amount, 2)
