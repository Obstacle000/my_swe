# My-SWE：基于 LangGraph 的企业级多源故障根因定位与代码自愈系统

本目录为《基于 LangGraph 的企业级多源故障根因定位与代码自愈系统》的完整纯 Python 工程实现，严格对齐根目录下的五份核心设计文档：

- [01_USE_CASES.md](docs/01_USE_CASES.md)：三大典型排障剧本（空指针 500 崩溃、VIP 会员阶梯折扣静默逻辑缺陷、短信网关超时历史故障复发）
- [02_STATE_SCHEMA_MODEL.md](docs/02_STATE_SCHEMA_MODEL.md)：1 主图 + 3 子图 `TypedDict` 状态机、`Schema (Pydantic v2)` 与 `Model (SQLAlchemy 2.0 Async)` 双层数据架构
- [03_TOOLS_GRAPH.md](docs/03_TOOLS_GRAPH.md)：四大物理隔离工具集、`Send` 并行探针派发、`valid_refs` 防幻觉证据校验、SHA-256 指纹去重与 `interrupt()` 人工审批
- [04_DATABASE.md](docs/04_DATABASE.md)：MySQL/SQLite、Elasticsearch (BM25) + Qdrant (Vector) + **RRF (`k=60`) 倒数秩融合**双路混合检索
- [5_API _DOC.md](../5_API%20_DOC.md)：FastAPI + SSE (`text/event-stream`) 实时流式推送与 `Command(resume=...)` 人工审批接口

---

## 1. 目录结构

```text
my_swe/
├── pyproject.toml                   # 项目依赖与 pytest 配置
├── requirements.txt                 # Python 依赖清单
├── .env.example                     # 环境变量模板 (支持配置 DeepSeek / Qwen API Key)
├── run_demo_cli.py                  # 全链路命令行演示脚本 (一键跑通三大剧本与 HITL 审批)
├── sql/
│   └── init_schema.sql              # MySQL 8.0 排障平台库与商城靶场库建表及预置故障数据脚本
├── target_mall_app/                 # 内置被排查的电商订单/计价故障靶场项目
│   ├── config/settings.yaml         # 含剧本三短信网关超时未开启降级缺陷的配置
│   ├── app/api/order_router.py      # 靶场结算路由 (/api/orders/checkout)
│   ├── app/services/order_service.py   # 含剧本一 第64行 RATE 折扣券 NoneType 500 缺陷
│   ├── app/services/pricing_service.py # 含剧本二 第42行 vip_level > 2 边界缺陷
│   └── tests/test_order_service.py  # 供沙箱自愈子图运行 pytest 验证的单元测试集
├── app/                             # 排障智能体平台核心源码
│   ├── main.py                      # FastAPI 主入口
│   ├── config.py                    # 全局环境配置与 ChatOpenAI 工厂
│   ├── prompts/                     # Markdown + Jinja2 提示词模板与加载器
│   ├── schemas/                     # Pydantic v2 业务契约与 API 请求/响应 Schema
│   ├── models/                      # SQLAlchemy 2.0 异步 ORM 模型
│   ├── core/                        # 证据采集脱敏、防幻觉报告校验器、RRF 双路混合检索后端
│   ├── tools/                       # 四大隔离工具集 (LOG_TOOLS / CODE_TOOLS / DB_TOOLS / SANDBOX_TOOLS)
│   ├── graphs/                      # LangGraph 主图 (main_graph.py) 与三大子图实现
│   └── api/                         # FastAPI 路由层 (靶场注入、排障 SSE 流、HITL 审批、RRF 调试)
└── tests/
    └── test_platform_workflow.py    # 排障平台自动化单元测试
```

---

## 2. 快速运行指南

### 2.1 运行全链路命令行演示 (`run_demo_cli.py`)

```powershell
..\.venv\Scripts\python.exe run_demo_cli.py
```

### 2.2 运行平台自动化单元测试 (`pytest`)

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_platform_workflow.py -v
```

### 2.3 启动 FastAPI 后端服务与 Swagger 接口文档

```powershell
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

启动后访问 `http://127.0.0.1:8000/docs` 即可在线调试全部接口。
