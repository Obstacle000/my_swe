# My-SWE：基于 LangGraph 的企业级多源故障根因定位与代码自愈系统
![Uploading langgraph_architecture.png…]()


本项目为《基于 LangGraph 的企业级多源故障根因分析（RCA）与代码自愈平台》的完整工程实现，对齐体系化设计规格与工业级排障规范：

- [01_USE_CASES.md](docs/01_USE_CASES.md)：三大典型排障场景（空指针 500 崩溃、VIP 阶梯折扣逻辑缺陷、第三方超时历史故障复发）
- [02_STATE_SCHEMA_MODEL.md](docs/02_STATE_SCHEMA_MODEL.md)：1 主图 + 3 子图 `TypedDict` 状态机、8 大证据源契约与 ORM 数据流
- [03_TOOLS_GRAPH.md](docs/03_TOOLS_GRAPH.md)：四大隔离工具集、Send 并行探针、证据评估闭环、防幻觉验钞机与 HITL 人机审批
- [04_DATABASE.md](docs/04_DATABASE.md)：MySQL/SQLite、Elasticsearch (BM25) + Qdrant (Dense Vector) + **RRF (`k=60`) 倒数秩融合**混合检索
- [5_API _DOC.md](docs/5_API%20_DOC.md)：FastAPI + SSE (`text/event-stream`) 实时工作台与 `Command(resume=...)` 断点恢复接口
- [TODO.md](docs/TODO.md)：平台生产化演进规划（解耦内置靶场、引入 MCP 外部工具 Server、集成 Filebeat 日志流等）

---

## 1. 核心架构亮点

1. **证据驱动与闭环评估（Evidence Evaluation Loop）**：
   - 告别单轮盲排，引入独立的 `evidence_evaluate_node` 证据质量裁判，支持最多 2 轮动态回环补证。
   - 评估打回意见（`evaluate_retry_reason`）精准指引 Planner 生成定向假设，跨轮/跨特工共享已知事实（`known_facts`）。
2. **8 维细粒度证据溯源（8-Dimensional Evidence Sourcing）**：
   - 原生支持：`error_log`、`trace_log`、`source_code`、`code_search`、`git_history`、`db_schema`、`db_record`、`crag_runbook`。
   - 工具自动拦截脱敏、签发不可伪造短 SHA-256 证据指纹。
3. **代码级防幻觉验钞机与纯动态事实兜底**：
   - `verify_report_node` 严格比对全局白名单，拦截伪造 `evidence_id`，计算 64 位 SHA-256 幂等指纹。
   - 报告草稿与兜底生成完全解耦硬编码，纯粹基于有效证据池动态提炼事实。
4. **Git Worktree 隔离沙箱代码自愈与人机协同（HITL）**：
   - 临时沙箱创建、自动代码补丁与 pytest 执行自愈重试（上限 3 次）。
   - LangGraph `interrupt()` 挂起等待人类工程师在线审批，支持修改意见反馈与自演进归档。

---

## 2. 目录结构

```text
my_swe/
├── pyproject.toml                   # 项目依赖与 pytest 配置
├── requirements.txt                 # Python 依赖清单
├── .env.example                     # 环境变量模板 (支持配置 DeepSeek / Qwen API Key)
├── run_demo_cli.py                  # 全链路命令行演示脚本 (一键跑通三大剧本与 HITL 审批)
├── docs/                            # 平台设计与规范文档库
│   ├── 01_USE_CASES.md              # 业务场景与排障剧本
│   ├── 02_STATE_SCHEMA_MODEL.md     # 状态机定义与数据契约
│   ├── 03_TOOLS_GRAPH.md            # 工具集、LangGraph 拓扑与路由规则
│   ├── 04_DATABASE.md               # 存储底座与 RRF 双路检索设计
│   ├── 5_API _DOC.md                # RESTful API 与 SSE 流式通信
│   ├── langgraph_architecture.svg   # LangGraph 全局架构流转拓扑图 (最新 1620x1280)
│   ├── PROJECT_DESIGN.md            # 总体架构设计演进
│   └── TODO.md                      # 生产级解耦演进路线图
├── sql/
│   └── init_schema.sql              # MySQL 8.0 排障平台库建表与预置场景脚本
├── target_mall_app/                 # 被排查的电商订单/计价微服务源码及单测
│   ├── config/settings.yaml         # 短信网关配置
│   ├── app/api/order_router.py      # 靶场路由
│   ├── app/services/order_service.py# 业务服务源码
│   ├── app/services/pricing_service.py # 计价服务源码
│   └── tests/test_order_service.py  # 供沙箱自愈子图运行验证的单测试集
├── app/                             # 排障智能体平台核心源码
│   ├── main.py                      # FastAPI 主入口与 Lifespan 生命周期管理
│   ├── config.py                    # 全局环境配置与 LLM 工厂
│   ├── prompts/                     # Markdown + Jinja2 提示词模板 (分诊/规划/评估/探针/报告)
│   ├── schemas/                     # Pydantic v2 业务契约与 API 请求响应模型
│   ├── models/                      # SQLAlchemy 2.0 异步 ORM 模型
│   ├── repositories/                # 数据访问仓储层 (MySQL / ES / Qdrant)
│   ├── core/                        # 证据采集脱敏、防幻觉校验器、RRF 双路混合检索
│   ├── tools/                       # 四大隔离工具集 (日志 / 代码 / 数据库 / 沙箱)
│   ├── graphs/                      # LangGraph 主图 (main_graph.py) 与三大子图
│   ├── services/                    # 业务服务层 (IncidentService / RangeService / KnowledgeService)
│   └── api/                         # FastAPI 控制层路由 (排障会话 / 演练靶场)
└── tests/
    └── test_platform_workflow.py    # 平台全链路核心节点与工作流自动化单测试集
```

---

## 3. 快速运行指南

### 3.1 运行全链路命令行演示 (`run_demo_cli.py`)

```powershell
..\.venv\Scripts\python.exe run_demo_cli.py
```

### 3.2 运行平台自动化单元测试 (`pytest`)

```powershell
..\.venv\Scripts\pytest.exe tests/test_platform_workflow.py -v
```

### 3.3 启动 FastAPI 后端服务与在线接口文档

```powershell
..\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
```

启动后访问 `http://127.0.0.1:8000/docs` 即可在线体验 Swagger UI 接口。
