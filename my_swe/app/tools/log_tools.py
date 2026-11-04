"""日志探针工具集 (LOG_TOOLS)：供 LLM bind_tools 生成结构化调用指令，实际执行由 probe_tool_node 通过 runtime.context['runtime_log_es_repo'] 完成。"""

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class SearchErrorLogsInput(BaseModel):
    keyword: str = Field(
        default="",
        description="报错关键词或异常类名，如 'TypeError', 'ReadTimeoutError', 'pricing'",
    )
    api_path: str = Field(
        default="",
        description="报错接口路由，如 '/api/orders/checkout' 或 '/api/auth/send-sms'",
    )
    user_id: str = Field(
        default="",
        description="关联的用户ID，如 '10086', '10087', '10088'",
    )
    level: str = Field(
        default="",
        description="日志级别过滤，可选 'ERROR', 'INFO', 或留空查询全部级别",
    )


class GetTraceLogsInput(BaseModel):
    trace_id: str = Field(
        ...,
        description="全链路追踪编号，如 'tr-88910-a1'、'tr-88920-b2'",
    )


@tool("search_error_logs", args_schema=SearchErrorLogsInput)
def search_error_logs(
    keyword: str = "",
    api_path: str = "",
    user_id: str = "",
    level: str = "",
) -> str:
    """检索线上运行态 Elasticsearch 日志索引 (mall-runtime-logs)，提取匹配的异常堆栈、trace_id、报错文件名与行号。"""
    return ""


@tool("get_trace_logs", args_schema=GetTraceLogsInput)
def get_trace_logs(trace_id: str) -> str:
    """根据 trace_id 拉取单次请求在各微服务节点上的完整调用链上下文日志。"""
    return ""


LOG_TOOLS = [search_error_logs, get_trace_logs]
