"""微商城订单 API 路由入口文件 (供代码探针 grep_code 搜索路由 URL 定位)"""

from fastapi import APIRouter
from pydantic import BaseModel
from target_mall_app.app.services.order_service import CouponRecord, checkout_order
from target_mall_app.app.services.pricing_service import UserRecord, calc_vip_discount

router = APIRouter(tags=["Target Mall Fault Range (被排查电商靶场原生接口)"])


class CheckoutRequestDTO(BaseModel):
    user_id: int = 10086
    total_price: float = 100.0
    coupon_type: str = "RATE"
    discount_amount: float | None = None
    discount_rate: float | None = 0.80


class PreviewRequestDTO(BaseModel):
    user_id: int = 10087
    username: str = "lisi_gold_vip"
    vip_level: int = 2
    amount: float = 200.0


@router.post("/api/orders/checkout")
async def post_orders_checkout_endpoint(dto: CheckoutRequestDTO) -> dict:
    """剧本一靶场接口：POST /api/orders/checkout"""
    coupon = CouponRecord(
        id=501,
        user_id=dto.user_id,
        coupon_type=dto.coupon_type,
        discount_amount=dto.discount_amount,
        discount_rate=dto.discount_rate,
    )
    final_amount = checkout_order(
        user_id=dto.user_id,
        total_price=dto.total_price,
        coupon=coupon,
    )
    return {"code": 200, "user_id": dto.user_id, "final_price": final_amount}


@router.post("/api/orders/preview")
async def post_orders_preview_endpoint(dto: PreviewRequestDTO) -> dict:
    """剧本二靶场接口：POST /api/orders/preview"""
    user = UserRecord(id=dto.user_id, username=dto.username, vip_level=dto.vip_level)
    preview_price = calc_vip_discount(user=user, amount=dto.amount)
    return {"code": 200, "vip_level": user.vip_level, "preview_price": preview_price}
