"""全局配置与大模型 (LLM) 实例工厂模块（桥接 app/conf/app_config.py）"""

from datetime import datetime, timezone
from langchain_openai import ChatOpenAI

from app.conf.app_config import PROJECT_ROOT, app_config


def now_iso() -> str:
    """生成当前时间的 ISO-8601 字符串"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class _SettingsCompat:
    """兼容属性访问层（底层数据全部来自 app_config）"""

    @property
    def llm_api_key(self) -> str:
        return app_config.llm.api_key

    @property
    def llm_base_url(self) -> str:
        return app_config.llm.base_url

    @property
    def llm_model_name(self) -> str:
        return app_config.llm.model

    @property
    def llm_temperature(self) -> float:
        return app_config.llm.temperature

    @property
    def default_target_repo_path(self):
        return app_config.target_project.resolved_path


settings = _SettingsCompat()


def get_llm(temperature: float | None = None) -> ChatOpenAI:
    """创建并返回配置好的 ChatOpenAI 大模型实例"""
    return ChatOpenAI(
        api_key=app_config.llm.api_key or "sk-placeholder",
        base_url=app_config.llm.base_url,
        model=app_config.llm.model,
        temperature=app_config.llm.temperature if temperature is None else temperature,
    )
