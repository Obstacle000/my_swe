"""数据库只读探针工具集 (DB_TOOLS)：供 LLM bind_tools 生成结构化调用指令，实际执行由 probe_tool_node 通过 runtime.context['mall_probe_repo'] 完成。"""

from langchain_core.tools import tool
from pydantic import BaseModel, Field


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
    return ""


@tool("execute_readonly_sql", args_schema=ExecuteReadonlySqlInput)
def execute_readonly_sql(sql_query: str) -> str:
    """在被排查业务数据库上执行只读 SELECT 查询，核对特定用户、优惠券或订单的真实字段数据（如 NULL 值、状态等）。"""
    return ""


DB_TOOLS = [inspect_table_schema, execute_readonly_sql]
