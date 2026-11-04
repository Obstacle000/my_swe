"""靶场单元测试用例 (供子图 3 PatchSelfHealSubState 执行 pytest 验证补丁是否正确)"""

import sys
from pathlib import Path

# 确保在隔离沙箱根目录执行 pytest 时能直接导入沙箱内的 app.services 模块
_SANDBOX_ROOT = Path(__file__).resolve().parent.parent
if str(_SANDBOX_ROOT) not in sys.path:
    sys.path.insert(0, str(_SANDBOX_ROOT))

from app.services.order_service import CouponRecord, checkout_order  # noqa: E402
from app.services.pricing_service import UserRecord, calc_vip_discount  # noqa: E402


def test_checkout_with_full_reduce_coupon():
    """测试普通满减券结算"""
    coupon = CouponRecord(
        id=502,
        user_id=10087,
        coupon_type="FULL_REDUCE",
        discount_amount=20.0,
        discount_rate=None,
    )
    assert checkout_order(user_id=10087, total_price=100.0, coupon=coupon) == 80.0


def test_checkout_with_rate_discount_coupon():
    """测试剧本一：用户 10086 使用 RATE 折扣券 (discount_amount 为 None, discount_rate=0.80)"""
    coupon = CouponRecord(
        id=501,
        user_id=10086,
        coupon_type="RATE",
        discount_amount=None,
        discount_rate=0.80,
    )
    # 100 元打 8 折应为 80.0 元，修复前此处会抛出 TypeError
    assert checkout_order(user_id=10086, total_price=100.0, coupon=coupon) == 80.0


def test_vip_level_2_gold_member_discount():
    """测试剧本二：vip_level=2 的黄金会员应享受 9 折优惠"""
    gold_user = UserRecord(id=10086, username="chen_vip_user", vip_level=2)
    assert calc_vip_discount(user=gold_user, amount=200.0) == 180.0
