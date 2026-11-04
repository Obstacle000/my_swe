"""数据库只读探针工具集 (DB_TOOLS)：带 SELECT 正则白名单锁与自动 LIMIT 保护"""

import json
import re
import sqlite3
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT

_FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|REPLACE|GRANT|CREATE|ATTACH|PRAGMA)\b",
    re.IGNORECASE,
)

_TABLE_DDL_DOCS: dict[str, str] = {
    "mall_users": (
        "CREATE TABLE mall_users (\n"
        "  id BIGINT PRIMARY KEY,          -- 用户ID (如 10086, 10087, 10088)\n"
        "  username VARCHAR(64) NOT NULL,  -- 用户名\n"
        "  vip_level INT NOT NULL,         -- 会员等级: 1(普通无折扣), 2(黄金会员8折), 3(钻石会员7折)\n"
        "  phone VARCHAR(32) NOT NULL,     -- 手机号\n"
        "  status VARCHAR(16) NOT NULL     -- 账户状态: ACTIVE / FROZEN\n"
        ");"
    ),
    "mall_coupons": (
        "CREATE TABLE mall_coupons (\n"
        "  coupon_id VARCHAR(32) PRIMARY KEY, -- 优惠券编号 (如 'CPN-100', 'CPN-999')\n"
        "  user_id BIGINT NOT NULL,           -- 归属用户ID\n"
        "  coupon_type VARCHAR(16) NOT NULL,  -- 券类型: 'CASH'(固定金额满减券) / 'RATE'(比率折扣券)\n"
        "  discount_amount DECIMAL(10,2) NULL,-- 固定立减金额 (注意: 当 coupon_type='RATE' 时该字段为 NULL!)\n"
        "  discount_rate DECIMAL(4,2) NULL,   -- 折扣率 (如 0.85 表示 85 折；当 coupon_type='CASH' 时为 NULL)\n"
        "  status VARCHAR(16) NOT NULL        -- 状态: UNUSED / USED / EXPIRED\n"
        ");"
    ),
    "mall_orders": (
        "CREATE TABLE mall_orders (\n"
        "  order_id VARCHAR(64) PRIMARY KEY,  -- 订单号 (如 'ORD-20261104-001')\n"
        "  user_id BIGINT NOT NULL,           -- 下单用户ID\n"
        "  sku_id VARCHAR(32) NOT NULL,       -- 商品SKU\n"
        "  base_price DECIMAL(10,2) NOT NULL, -- 商品原价\n"
        "  coupon_id VARCHAR(32) NULL,        -- 使用的优惠券ID\n"
        "  final_price DECIMAL(10,2) NOT NULL,-- 最终实付金额\n"
        "  order_status VARCHAR(32) NOT NULL, -- 订单状态: FAILED_500 / PAID_WRONG_PRICE / PAID\n"
        "  trace_id VARCHAR(64) NOT NULL      -- 关联的日志链路 trace_id\n"
        ");"
    ),
}


class InspectTableSchemaInput(BaseModel):
    table_name: str = Field(
        default="all",
        description="要查看表结构的表名，可选 'mall_users', 'mall_coupons', 'mall_orders' 或 'all'",
    )


class ExecuteReadonlySqlInput(BaseModel):
    sql_query: str = Field(
        ...,
        description="只读 SELECT 查询语句，如 \"SELECT * FROM mall_coupons WHERE coupon_id = 'CPN-999'\"",
    )


@tool("inspect_table_schema", args_schema=InspectTableSchemaInput)
def inspect_table_schema(table_name: str = "all") -> str:
    """查看被排查电商业务数据库的表结构定义（DDL 与字段注释），帮助编写准确的只读 SQL。"""
    t_name = table_name.strip().lower()
    if t_name in _TABLE_DDL_DOCS:
        return _TABLE_DDL_DOCS[t_name]
    return "\n\n".join(_TABLE_DDL_DOCS.values())


@tool("execute_readonly_sql", args_schema=ExecuteReadonlySqlInput)
def execute_readonly_sql(sql_query: str) -> str:
    """在被排查业务数据库上执行只读 SELECT 查询，核对特定用户、优惠券或订单的真实字段数据（如 NULL 值、状态等）。"""
    cleaned_sql = sql_query.strip().rstrip(";")

    # 1. 安全拦截：必须以 SELECT 开头，且严禁包含任何写操作或 DDL 关键字，禁止多语句执行
    if not re.match(r"^\s*SELECT\b", cleaned_sql, re.IGNORECASE):
        return "SQL 安全拦截：仅允许执行以 SELECT 开头的只读查询语句。"
    if ";" in cleaned_sql or _FORBIDDEN_SQL_PATTERN.search(cleaned_sql):
        return "SQL 安全拦截：检测到高危关键字或多语句分隔符，已拒绝执行。"

    # 2. 若未指定 LIMIT，自动追加 LIMIT 20 防止全表扫描撑爆内存
    if not re.search(r"\bLIMIT\s+\d+", cleaned_sql, re.IGNORECASE):
        cleaned_sql = f"{cleaned_sql} LIMIT 20"

    db_file = PROJECT_ROOT / "mall_target.db"
    if not db_file.exists():
        return "目标靶场数据库尚未初始化。"

    try:
        conn = sqlite3.connect(str(db_file), timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(cleaned_sql)
        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
    except Exception as exc:
        return f"SQL 执行异常: {exc}"

    if not rows:
        return f"查询成功，但返回 0 行记录 (SQL: {cleaned_sql})。"
    return json.dumps(rows, ensure_ascii=False, indent=2)


DB_TOOLS = [inspect_table_schema, execute_readonly_sql]
