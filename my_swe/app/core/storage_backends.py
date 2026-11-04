"""双模异构存储与混合检索 (ES BM25 + Qdrant Vector + RRF 倒数秩融合) 核心引擎
支持开箱即用本地内存/SQLite 模式与生产级 Docker (Elasticsearch + Qdrant + MySQL) 模式无缝切换。
"""

import math
import re
from typing import Any
from sqlalchemy import select, text

from app.config import now_iso, settings
from app.models.database import (
    MallBase,
    MallSessionLocal,
    PlatformBase,
    PlatformSessionLocal,
    mall_engine,
    platform_engine,
)
from app.models.orm_models import (
    HistoricalRunbookModel,
    MallCouponModel,
    MallOrderModel,
    MallUserModel,
    WorkspaceModel,
)

# ============================================================================
# 1. 预置的线上运行态日志数据 (对应《01_USE_CASES.md》三大经典排障剧本)
# ============================================================================
INITIAL_RUNTIME_LOGS: list[dict[str, Any]] = [
    # --- 剧本一 (case_1_coupon_null_500): 优惠券 NoneType 导致结算 500 ---
    {
        "timestamp": "2026-11-04T10:15:01+08:00",
        "trace_id": "tr-88910-a1",
        "level": "INFO",
        "service": "order-service",
        "api_path": "/api/orders/checkout",
        "user_id": "10086",
        "file_line": "order_router.py:24",
        "message": "收到订单结算请求: user_id=10086, sku_id=SKU-PHONE-99, base_price=200.00, coupon_id=CPN-999",
        "stacktrace": "",
    },
    {
        "timestamp": "2026-11-04T10:15:02+08:00",
        "trace_id": "tr-88910-a1",
        "level": "ERROR",
        "service": "order-service",
        "api_path": "/api/orders/checkout",
        "user_id": "10086",
        "file_line": "order_service.py:64",
        "message": "订单结算异常 HTTP 500: TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'",
        "stacktrace": (
            "Traceback (most recent call last):\n"
            '  File "target_mall_app/app/api/order_router.py", line 28, in checkout_endpoint\n'
            "    return checkout_order(req.user_id, req.sku_id, req.base_price, req.coupon_id)\n"
            '  File "target_mall_app/app/services/order_service.py", line 64, in checkout_order\n'
            '    final_price = round(base_price - coupon["discount_amount"], 2)\n'
            "TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'\n"
            "Context Env: MYSQL_PASSWORD=root_secret_123456, coupon_id=CPN-999, coupon_type=RATE"
        ),
    },
    # --- 剧本二 (case_2_vip_discount_bug): VIP 2 级会员未打折静默逻辑缺陷 ---
    {
        "timestamp": "2026-11-04T10:20:11+08:00",
        "trace_id": "tr-88920-b2",
        "level": "INFO",
        "service": "pricing-service",
        "api_path": "/api/orders/checkout",
        "user_id": "10087",
        "file_line": "pricing_service.py:42",
        "message": "计算会员折扣完毕: user_id=10087, vip_level=2, base_price=200.00, applied_ratio=1.0, discounted_price=200.00 (未触发黄金会员8折)",
        "stacktrace": "",
    },
    # --- 剧本三 (case_3_sms_timeout_recurrence): 短信网关超时历史故障复发 ---
    {
        "timestamp": "2026-11-04T10:25:30+08:00",
        "trace_id": "tr-88930-c3",
        "level": "ERROR",
        "service": "auth-sms-service",
        "api_path": "/api/auth/send-sms",
        "user_id": "10088",
        "file_line": "sms_client.py:51",
        "message": "ReadTimeoutError: HTTPSConnectionPool(host='sms.aliyun-gateway.example.com', port=443): Read timed out. (read timeout=2.0)",
        "stacktrace": (
            "Traceback (most recent call last):\n"
            '  File "target_mall_app/app/services/sms_client.py", line 51, in send_verify_code\n'
            "    resp = httpx.post(SMS_PROVIDER_URL, timeout=2.0)\n"
            "httpx.ReadTimeout: ReadTimeoutError: HTTPSConnectionPool(host='sms.aliyun-gateway.example.com', port=443): Read timed out. (read timeout=2.0)\n"
            "phone=13812345678, api_key=sk-aliyunSecretKey99887766"
        ),
    },
]

# ============================================================================
# 2. 预置的历史故障知识库 (Runbooks，供 CRAG 双路混合检索 + RRF 融合使用)
# ============================================================================
INITIAL_RUNBOOKS: list[dict[str, Any]] = [
    {
        "incident_code": "INC-2026-089",
        "title": "大促高峰期阿里云主用短信网关限流引起 ReadTimeoutError (read timeout=2.0) 故障复盘",
        "symptoms": (
            "用户调用 /api/auth/send-sms 发送验证码接口大面积返回 HTTP 504，"
            "ES 日志密集报错 ReadTimeoutError: HTTPSConnectionPool(host='sms.aliyun-gateway.example.com', port=443): Read timed out. (read timeout=2.0)。"
        ),
        "root_cause": (
            "外部第三方主用短信网关 (aliyun_primary) 在并发超过 500 QPS 时触发上游流控队列堆积，"
            "本地 target_mall_app/config/settings.yaml 中 timeout_seconds=2.0 且 fallback_enabled=false 未开启自动降级。"
        ),
        "resolution": (
            "属于外部上游网关限流与配置开关问题（无需修改 Python 业务源码，need_code_patch=False）：\n"
            "1. 立即将 target_mall_app/config/settings.yaml 中的 sms_gateway.provider 从 'aliyun_primary' 切换为备用通道 'tencent_backup'；\n"
            "2. 开启 fallback_enabled: true 并将 timeout_seconds 调整为 1.5s 快速失败降级。"
        ),
        "service_name": "auth-sms-service",
        "error_keywords": "ReadTimeoutError sms.aliyun-gateway.example.com send-sms read timeout=2.0 HTTP 504",
    },
    {
        "incident_code": "INC-2026-042",
        "title": "订单秒杀场景 Redis 连接池耗尽 PoolExhaustedError 导致库存扣减失败",
        "symptoms": "高并发下单时 /api/orders/seckill 报错 redis.exceptions.ConnectionError: max_connections=10 exceeded。",
        "root_cause": "Redis 客户端连接池未正确释放游标，且 max_connections 配置过小。",
        "resolution": "在连接上下文中使用 async with 自动释放连接，并将 max_connections 提升至 100。",
        "service_name": "inventory-service",
        "error_keywords": "PoolExhaustedError redis ConnectionError max_connections seckill",
    },
    {
        "incident_code": "INC-2026-015",
        "title": "跨境商品汇率换算 Float 精度丢失引发对账差一分钱告警",
        "symptoms": "外币结算订单实付金额与支付网关回调金额偶发相差 0.01 元。",
        "root_cause": "使用二进制浮点数 float 直接相乘未经过 Decimal 四舍五入。",
        "resolution": "统一使用 decimal.Decimal 并指定 ROUND_HALF_UP。",
        "service_name": "payment-service",
        "error_keywords": "Decimal float rounding precision mismatch payment",
    },
]


def _tokenize(text_str: str) -> list[str]:
    """中英文混合轻量分词器（提取英文单词、代码标识符、数字与中文字符二元组）"""
    lowered = text_str.lower()
    ascii_tokens = re.findall(r"[a-z0-9_.\-/]+", lowered)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    cjk_bigrams = [cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)]
    return ascii_tokens + cjk_chars + cjk_bigrams


class HybridStorageManager:
    """统一管理运行态日志检索与历史故障知识库双路 RRF 混合检索"""

    def __init__(self) -> None:
        self.runtime_logs: list[dict[str, Any]] = list(INITIAL_RUNTIME_LOGS)
        self.runbooks: list[dict[str, Any]] = list(INITIAL_RUNBOOKS)

    def inject_scenario_fault(self, scenario_id: str) -> dict[str, Any]:
        """靶场故障注入器：向运行态日志流追加最新时间戳的故障日志并返回模拟报文"""
        ts = now_iso()
        if scenario_id == "case_1_coupon_null_500":
            trace_id = "tr-88910-a1"
            new_log = {
                **INITIAL_RUNTIME_LOGS[1],
                "timestamp": ts,
                "trace_id": trace_id,
            }
            self.runtime_logs.insert(0, new_log)
            return {
                "scenario_id": scenario_id,
                "injected_status": "success",
                "simulated_request": {
                    "method": "POST",
                    "url": "/api/orders/checkout",
                    "body": {"user_id": 10086, "sku_id": "SKU-PHONE-99", "base_price": 200.0, "coupon_id": "CPN-999"},
                },
                "simulated_response": {
                    "http_status": 500,
                    "error": "Internal Server Error: TypeError in checkout_order",
                    "trace_id": trace_id,
                },
                "es_log_written": True,
                "suggested_prompt": "用户 10086 刚刚反馈在商城结算订单（调用 /api/orders/checkout）时页面直接报 500 系统异常，麻烦帮我排查一下原因并修复。",
            }

        if scenario_id == "case_2_vip_discount_bug":
            trace_id = "tr-88920-b2"
            new_log = {
                **INITIAL_RUNTIME_LOGS[2],
                "timestamp": ts,
                "trace_id": trace_id,
            }
            self.runtime_logs.insert(0, new_log)
            return {
                "scenario_id": scenario_id,
                "injected_status": "success",
                "simulated_request": {
                    "method": "POST",
                    "url": "/api/orders/checkout",
                    "body": {"user_id": 10087, "sku_id": "SKU-LAPTOP-01", "base_price": 200.0, "coupon_id": None},
                },
                "simulated_response": {
                    "http_status": 200,
                    "final_price": 200.0,
                    "expected_price": 160.0,
                    "trace_id": trace_id,
                },
                "es_log_written": True,
                "suggested_prompt": "用户 10087 投诉他是刚升级的 VIP 2 级黄金会员，买 200 元的商品应该打 8 折收 160 元，但订单实付金额还是收了 200 元，后台也没报错，请排查。",
            }

        # 默认 case_3_sms_timeout_recurrence
        trace_id = "tr-88930-c3"
        new_log = {
            **INITIAL_RUNTIME_LOGS[3],
            "timestamp": ts,
            "trace_id": trace_id,
        }
        self.runtime_logs.insert(0, new_log)
        return {
            "scenario_id": "case_3_sms_timeout_recurrence",
            "injected_status": "success",
            "simulated_request": {
                "method": "POST",
                "url": "/api/auth/send-sms",
                "body": {"user_id": 10088, "phone": "13812345678"},
            },
            "simulated_response": {
                "http_status": 504,
                "error": "Gateway Timeout: ReadTimeoutError (read timeout=2.0)",
                "trace_id": trace_id,
            },
            "es_log_written": True,
            "suggested_prompt": "线上告警：登录发送验证码接口 /api/auth/send-sms 突然大面积报 ReadTimeoutError (read timeout=2.0)，请紧急排查是否历史故障复发。",
        }

    def search_runtime_logs(
        self,
        keyword: str = "",
        api_path: str = "",
        user_id: str = "",
        level: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """检索运行态日志（按匹配度与时间排序）"""
        scored: list[tuple[int, dict[str, Any]]] = []
        kw_lower = keyword.strip().lower()
        api_lower = api_path.strip().lower()
        uid_str = str(user_id).strip()
        lvl_upper = level.strip().upper()

        for log in self.runtime_logs:
            score = 0
            blob = f"{log['message']} {log['stacktrace']} {log['api_path']} {log['service']} {log['user_id']}".lower()
            if uid_str and uid_str == str(log.get("user_id", "")):
                score += 5
            if api_lower and api_lower in log.get("api_path", "").lower():
                score += 4
            if lvl_upper and lvl_upper == log.get("level", "").upper():
                score += 2
            if kw_lower:
                for token in kw_lower.split():
                    if token in blob:
                        score += 3
            if not (kw_lower or api_lower or uid_str or lvl_upper):
                score = 1
            if score > 0:
                scored.append((score, log))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    def get_logs_by_trace_id(self, trace_id: str) -> list[dict[str, Any]]:
        """按全链路 trace_id 提取完整调用链日志"""
        tid = trace_id.strip()
        return [log for log in self.runtime_logs if log.get("trace_id") == tid]

    def _bm25_rank_runbooks(self, query: str) -> list[tuple[str, float]]:
        """第一路：BM25 精确关键词检索排序 (优先匹配异常类名、报错码、接口路由)"""
        q_tokens = _tokenize(query)
        results: list[tuple[str, float]] = []
        N = max(len(self.runbooks), 1)

        for rb in self.runbooks:
            doc_text = f"{rb['incident_code']} {rb['title']} {rb['error_keywords']} {rb['symptoms']}"
            doc_tokens = _tokenize(doc_text)
            token_freq: dict[str, int] = {}
            for t in doc_tokens:
                token_freq[t] = token_freq.get(t, 0) + 1

            score = 0.0
            for qt in set(q_tokens):
                tf = token_freq.get(qt, 0)
                if tf > 0:
                    # 精确报错关键词加权
                    boost = 3.0 if qt in rb["error_keywords"].lower() else 1.0
                    score += boost * (1.0 + math.log(1 + tf)) * math.log(1 + N)
            results.append((rb["incident_code"], round(score, 4)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def _vector_rank_runbooks(self, query: str) -> list[tuple[str, float]]:
        """第二路：稠密语义检索排序 (基于症状、根因、解决方案的语义重合度模拟向量余弦相似度)"""
        q_set = set(_tokenize(query))
        results: list[tuple[str, float]] = []

        for rb in self.runbooks:
            sem_text = f"{rb['title']} {rb['symptoms']} {rb['root_cause']} {rb['resolution']}"
            doc_set = set(_tokenize(sem_text))
            if not q_set or not doc_set:
                sim = 0.0
            else:
                overlap = len(q_set & doc_set)
                sim = overlap / math.sqrt(len(q_set) * len(doc_set))
            results.append((rb["incident_code"], round(sim, 4)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def hybrid_search_rrf(self, query: str, top_k: int = 3, k_constant: int = 60) -> list[dict[str, Any]]:
        """执行 ES (BM25) + Qdrant (Vector) 双路并发检索与 RRF (倒数秩融合) 重排

        公式:
            RRF_Score(d) = 1 / (k + rank_bm25(d)) + 1 / (k + rank_vector(d)), 其中 k = 60
        """
        bm25_ranked = self._bm25_rank_runbooks(query)
        vec_ranked = self._vector_rank_runbooks(query)

        bm25_rank_map = {code: (rank, score) for rank, (code, score) in enumerate(bm25_ranked, start=1)}
        vec_rank_map = {code: (rank, score) for rank, (code, score) in enumerate(vec_ranked, start=1)}
        rb_map = {rb["incident_code"]: rb for rb in self.runbooks}

        fused_list: list[dict[str, Any]] = []
        for code, rb in rb_map.items():
            b_rank, b_score = bm25_rank_map.get(code, (999, 0.0))
            v_rank, v_score = vec_rank_map.get(code, (999, 0.0))
            rrf_score = (1.0 / (k_constant + b_rank)) + (1.0 / (k_constant + v_rank))

            # 若两路得分均为 0，说明完全无关，排在末尾
            effective_score = rrf_score if (b_score > 0 or v_score > 0) else 0.0
            fused_list.append(
                {
                    **rb,
                    "bm25_rank": b_rank,
                    "bm25_score": b_score,
                    "vector_rank": v_rank,
                    "vector_score": v_score,
                    "rrf_score": round(effective_score, 6),
                }
            )

        fused_list.sort(key=lambda item: (item["rrf_score"], item["bm25_score"]), reverse=True)
        return fused_list[:top_k]

    def add_runbook_memory(self, runbook_dict: dict[str, Any]) -> None:
        """经验自进化：将新结案的故障复盘同步写入内存/ES/Qdrant 检索库"""
        existing_codes = {r["incident_code"] for r in self.runbooks}
        if runbook_dict["incident_code"] not in existing_codes:
            self.runbooks.insert(0, runbook_dict)


# 全局单例存储管理器
storage_manager = HybridStorageManager()


async def seed_databases_if_empty() -> None:
    """应用启动时自动创建数据库表并写入预置的商城靶场故障数据与历史故障库"""
    async with platform_engine.begin() as conn:
        await conn.run_sync(PlatformBase.metadata.create_all)

    async with mall_engine.begin() as conn:
        await conn.run_sync(MallBase.metadata.create_all)

    # 1. 初始化排障平台库 (workspaces & historical_runbooks)
    async with PlatformSessionLocal() as session:
        ws_res = await session.execute(select(WorkspaceModel).where(WorkspaceModel.id == "ws_mall_prod"))
        if ws_res.scalar_one_or_none() is None:
            session.add(
                WorkspaceModel(
                    id="ws_mall_prod",
                    name="电商核心交易系统 (生产靶场)",
                    repo_path=str(settings.default_target_repo_path),
                    db_conn_uri=settings.target_mall_db_url,
                    es_index_name=settings.es_runtime_log_index,
                )
            )

        rb_res = await session.execute(select(HistoricalRunbookModel))
        if not rb_res.scalars().first():
            for rb in INITIAL_RUNBOOKS:
                session.add(
                    HistoricalRunbookModel(
                        incident_code=rb["incident_code"],
                        title=rb["title"],
                        symptoms=rb["symptoms"],
                        root_cause=rb["root_cause"],
                        resolution=rb["resolution"],
                        service_name=rb["service_name"],
                        error_keywords=rb["error_keywords"],
                        es_doc_id=f"es_{rb['incident_code']}",
                        qdrant_point_id=f"vec_{rb['incident_code']}",
                    )
                )
        await session.commit()

    # 2. 初始化被排查电商靶场库 (mall_users, mall_coupons, mall_orders)
    async with MallSessionLocal() as session:
        user_res = await session.execute(select(MallUserModel).where(MallUserModel.id == 10086))
        if user_res.scalar_one_or_none() is None:
            session.add_all(
                [
                    MallUserModel(id=10086, username="zhangsan_normal", vip_level=1, phone="13800138001", status="ACTIVE"),
                    MallUserModel(id=10087, username="lisi_gold_vip", vip_level=2, phone="13800138002", status="ACTIVE"),
                    MallUserModel(id=10088, username="wangwu_sms_user", vip_level=3, phone="13812345678", status="ACTIVE"),
                ]
            )
            session.add_all(
                [
                    MallCouponModel(
                        coupon_id="CPN-100",
                        user_id=10086,
                        coupon_type="CASH",
                        discount_amount=30.00,
                        discount_rate=None,
                        status="UNUSED",
                    ),
                    # 剧本一核心故障数据：RATE 折扣券的 discount_amount 为 NULL，而 discount_rate 为 0.85
                    MallCouponModel(
                        coupon_id="CPN-999",
                        user_id=10086,
                        coupon_type="RATE",
                        discount_amount=None,
                        discount_rate=0.85,
                        status="UNUSED",
                    ),
                ]
            )
            session.add_all(
                [
                    MallOrderModel(
                        order_id="ORD-20261104-001",
                        user_id=10086,
                        sku_id="SKU-PHONE-99",
                        base_price=200.00,
                        coupon_id="CPN-999",
                        final_price=0.00,
                        order_status="FAILED_500",
                        trace_id="tr-88910-a1",
                    ),
                    # 剧本二核心故障数据：VIP 2 级黄金会员买 200 元商品被错误收取了 200 元原价
                    MallOrderModel(
                        order_id="ORD-20261104-002",
                        user_id=10087,
                        sku_id="SKU-LAPTOP-01",
                        base_price=200.00,
                        coupon_id=None,
                        final_price=200.00,
                        order_status="PAID_WRONG_PRICE",
                        trace_id="tr-88920-b2",
                    ),
                ]
            )
            await session.commit()
