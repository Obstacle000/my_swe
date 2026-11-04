"""微商城订单结算核心服务 (对应《01_USE_CASES.md》剧本一故障源)"""

from dataclasses import dataclass


@dataclass
class CouponRecord:
    """对应靶场数据库表 mall_coupons 的记录结构"""

    id: int
    user_id: int
    coupon_type: str  # 'FULL_REDUCE'(满减券) 或 'RATE'(折扣券)
    discount_amount: float | None  # 当 coupon_type='RATE' 时，数据库中此字段为 None
    discount_rate: float | None  # 当 coupon_type='RATE' 时，如 0.80 表示 8 折
    status: str = "UNUSED"


def validate_order_amount(total_price: float) -> None:
    """校验订单基础金额合法性"""
    if total_price <= 0:
        raise ValueError("订单总金额必须大于 0")


def check_coupon_status(coupon: CouponRecord) -> None:
    """校验优惠券状态是否可用"""
    if coupon.status != "UNUSED":
        raise ValueError(f"优惠券 {coupon.id} 状态不可用: {coupon.status}")


# =====================================================================
# 核心结算函数 (注意第 64 行的缺陷：未区分 RATE 折扣券与 FULL_REDUCE 满减券)
# =====================================================================


def checkout_order(
    user_id: int,
    total_price: float,
    coupon: CouponRecord | None = None,
) -> float:
    """
    执行用户订单结算并计算使用优惠券后的最终实付金额。
    参数:
        user_id: 下单用户 ID (例如 10086)
        total_price: 商品原始总价
        coupon: 用户选择的优惠券记录
    """
    validate_order_amount(total_price)

    # 若未使用优惠券，直接返回原价
    if coupon is None:
        return round(total_price, 2)

    check_coupon_status(coupon)

    # 校验优惠券归属权
    if coupon.user_id != user_id:
        raise PermissionError("无权使用他人名下的优惠券")

    # -----------------------------------------------------------------
    # 缺陷现场：当用户 10086 使用折扣券 (coupon_type == 'RATE') 时，
    # 数据库中 coupon.discount_amount 为 None，下一行直接相减会抛出：
    # TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'
    # -----------------------------------------------------------------
    final_price = total_price - coupon.discount_amount
    return round(max(final_price, 0.0), 2)
