"""排障平台核心表与电商靶场业务表 SQLAlchemy 2.0 ORM 模型定义 (对应《04_DATABASE.md》)"""

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.config import now_iso
from app.models.base import Base


class WorkspaceModel(Base):
    """表 1: 被监控目标项目配置表 (workspaces)"""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="目标项目ID")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    repo_path: Mapped[str] = mapped_column(String(500), nullable=False)
    db_conn_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    es_index_name: Mapped[str] = mapped_column(String(128), nullable=False)
    default_test_cmd: Mapped[str] = mapped_column(String(255), default="pytest")
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso, onupdate=now_iso)


class IncidentModel(Base):
    """表 2: 排障会话主表 (incidents) —— 主键 id 直接兼任 LangGraph thread_id"""

    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="会话ID (兼任 thread_id)")
    workspace_id: Mapped[str] = mapped_column(String(64), index=True, default="ws_mall_prod")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(32), default="user_chat")
    status: Mapped[str] = mapped_column(String(32), index=True, default="investigating")
    latest_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    report_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patch_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso, onupdate=now_iso)


class IncidentMessageModel(Base):
    """表 3: 会话多轮消息历史表 (incident_messages)"""

    __tablename__ = "incident_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, default=1)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    msg_type: Mapped[str] = mapped_column(String(32), default="text")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class EvidenceModel(Base):
    """表 4: 真实证据账本明细表 (incident_evidences)"""

    __tablename__ = "incident_evidences"

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    query_args: Mapped[str] = mapped_column(String(1000), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class PatchRecordModel(Base):
    """表 5: 代码补丁与沙箱自愈审批表 (patch_records)"""

    __tablename__ = "patch_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    target_file: Mapped[str] = mapped_column(String(500), nullable=False)
    patch_diff: Mapped[str] = mapped_column(Text, nullable=False)
    test_command: Mapped[str] = mapped_column(String(255), nullable=False)
    test_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    test_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    self_heal_retries: Mapped[int] = mapped_column(Integer, default=0)
    approval_status: Mapped[str] = mapped_column(String(32), default="pending")
    human_feedback: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso, onupdate=now_iso)


class HistoricalRunbookModel(Base):
    """表 6: 历史故障复盘知识库主表 (historical_runbooks) —— 与 ES 和 Qdrant 保持逻辑主键对齐"""

    __tablename__ = "historical_runbooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    symptoms: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(Text, nullable=False)
    service_name: Mapped[str] = mapped_column(String(64), default="order-service")
    error_keywords: Mapped[str] = mapped_column(String(500), default="")
    es_doc_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qdrant_point_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)


# 兼容别名
KnowledgeRunbookModel = HistoricalRunbookModel


# ============================================================================
# 被排查电商靶场业务表 (mall_users, mall_coupons, mall_orders)
# ============================================================================
class MallUserModel(Base):
    """电商靶场用户表 (mall_users)"""

    __tablename__ = "mall_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    vip_level: Mapped[int] = mapped_column(Integer, default=1)
    phone: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")


class MallCouponModel(Base):
    """电商靶场优惠券表 (mall_coupons)"""

    __tablename__ = "mall_coupons"

    coupon_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    coupon_type: Mapped[str] = mapped_column(String(16), nullable=False)  # CASH / RATE
    discount_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="UNUSED")


class MallOrderModel(Base):
    """电商靶场订单流水表 (mall_orders)"""

    __tablename__ = "mall_orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    sku_id: Mapped[str] = mapped_column(String(32), nullable=False)
    base_price: Mapped[float] = mapped_column(Float, nullable=False)
    coupon_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    final_price: Mapped[float] = mapped_column(Float, nullable=False)
    order_status: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
