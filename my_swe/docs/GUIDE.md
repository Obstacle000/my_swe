# 智能故障根因分析（RCA）与代码自愈 Agent —— 源码研读与系统实现指南

> **项目定位**：2027 届春招 AI 应用工程师核心主打项目 + 软件工程专业本科毕业设计  
> **核心技术栈**：纯 Python 后端与 AI 引擎 (LangGraph / FastAPI / SQLAlchemy / MCP / Tree-sitter) + Vue3 + Elasticsearch + Qdrant + MySQL + Redis + MongoDB

---

## 第一部分：`open-swe` 源码精准研读清单（预计耗时：1 天）

本地源码路径：`C:\Users\Chen\Desktop\develope_note\py+ai大模型\my_swe\open-swe`

`open-swe` 仓库中包含大量与 SaaS 商业托管（Managed Deep Agents）、Slack 机器人、GitHub App OAuth 相关的外围业务代码（占比约 80%），这些部分无需阅读。请仅围绕以下 **5 个核心文件** 进行精读，重点吸收其 **排障 SOP 设计、证据链防幻觉机制、以及本地文件操作工具原语**。

### 1. 排障标准作业流程（SOP）与防幻觉提示词
* **文件路径**：`open-swe/openswe/resources/prompts/incidents/report.md`（共 48 行，建议全文精读）
* **重点研读内容**：
  1. **第 8~16 行（区分现象与根因）**：如何约束大模型区分“用户报告的表象（Reported Symptoms）”、“观测到的指标/日志（Observed Telemetry）”、“表面相关性（Correlation）”与“已证实的根因（Established Cause）”。
  2. **第 19~23 行（强制证据绑定）**：要求每一条结论（Claim）、影响面评估和修复建议都必须附带工具返回的 `evidence_ids`；没有任何证据支撑的推论只能放入 `questions`（待确认问题），严禁作为结论输出。
  3. **第 25~32 行（五步排障法）**：
     * Step 1：明确触发告警的服务、接口及指标含义；
     * Step 2：调用 `search_incidents` 检索历史相似故障（若命中历史复盘，直接参考历史修复方案）；
     * Step 3：评估故障影响范围；
     * Step 4：提出假设并验证根因；
     * Step 5：输出具体的修复步骤与代码补丁。

### 2. 结构化排障报告与假设验证数据模型
* **文件路径**：`open-swe/openswe/incidents/models.py`（重点看第 34~64 行）
* **重点研读内容**：
  1. `Evidence` 类（第 34~40 行）：记录单条证据的唯一 ID（`id`）、来源（`source`，如日志、代码、数据库）、摘要（`summary`）与查询语句（`query`）。
  2. `Hypothesis` 类（第 43~46 行）：记录排障假设标题、验证状态（`supported` 已证实 / `plausible` 待定 / `rejected` 已排除）以及支撑该假设的 `evidence_ids` 列表。
  3. `IncidentReport` 类（第 49~64 行）：最终产出的结构化排障报告 Schema。

### 3. 代码级“防大模型幻觉”拦截器实现
* **文件路径**：`open-swe/openswe/incidents/report.py`（重点看第 30~45 行、第 106~155 行）
* **重点研读内容**：
  * 精读 `finalize_report` 函数（第 106~155 行）：观察它如何取出当前会话收集到的所有真实证据 ID 集合（`known_ids`），并对大模型提交的 `ReportDraft` 进行逐项核验（`valid_refs`）。如果大模型编造了不存在的 `evidence_id` 或未提供证据，代码层直接拦截并剔除该条结论。

### 4. 故障排查会话与工具组装逻辑
* **文件路径**：`open-swe/openswe/incidents/runtime.py`（重点看第 50~99 行）
* **重点研读内容**：
  * 查看 `IncidentSession.__init__` 如何将系统提示词、排障规则提示词拼接到一起，并将 `record_incident_report`、`search_incidents`、`read_incident` 封装为 LangChain 的 `StructuredTool`。

### 5. 本地代码库目录遍历与文件读取工具原语
* **文件路径**：
  * `open-swe/mda/rt/remote_files.py`（重点看第 43~120 行）
  * `open-swe/mda/rt/file_tools.py`（重点看第 56~70 行）
* **重点研读内容**：
  1. `FILE_TOOLS` 集合定义的 8 个标准原语：`ls`（列目录）、`glob`（按通配符找文件）、`grep`（在目录中搜索代码关键词并返回匹配行及上下游上下文行 `ContextLine`）、`read_file`（带 `offset` 起始行和 `limit` 行数限制的分页读取）、`write_file`、`edit_file`、`delete`、`execute`。
  2. `_GUARDED_WORDS` 正则拦截机制：如何在执行终端命令或代码修改前拦截高危操作并转交人工审批。

---

## 第二部分：我们要实现的项目整体规划（面试 + 毕设完整版）

### 一、 项目名称与架构定位
* **项目名称**：基于 LangGraph 多智能体与多源证据链的微服务故障根因分析（RCA）及自愈平台
* **核心差异化（对比开源 `SWE-agent` 与普通学生项目）**：
  1. **多源运行时证据交叉验证**：不止步于静态代码阅读，而是将 **Elasticsearch 运行时错误日志** + **本地代码库 AST/窗口探针** + **MySQL 业务数据只读核验（NL2SQL）** + **Qdrant 历史故障库（CRAG）** 融合进同一个 LangGraph 状态机。
  2. **五大硬核工程深度**（面试核心壁垒）：
     * **代码感知层**：基于 `tree-sitter` 的 AST 语法树函数级切片 + RepoMap 仓库调用拓扑图生成；
     * **上下文工程层**：多步排障中的历史轨迹动态折叠（Observation Masking），当某个假设被排除（`rejected`）后自动折叠已读大文件，防止 Token 爆炸与注意力迷失；
     * **推理编排层**：利用 LangGraph `Send` API 实现多假设并行取证（Map-Reduce），并在出口节点实现 `evidence_id` 防幻觉硬校验；
     * **验证与自愈层**：基于 Git Worktree 的隔离沙箱补丁应用 + 自动化单元测试执行 + 报错自愈重试闭环（Self-Healing Loop）+ `interrupt` 人工审批（HITL）；
     * **记忆演进层**：ES (BM25) + Qdrant (Dense Vector) 的 RRF 混合检索 CRAG，以及故障修复后自动生成 Post-mortem 沉淀回写知识库的自演进闭环。

---

### 二、 系统模块划分与代码量规划（总计约 9,000 ~ 11,000 行）

#### 子系统 1：Python AI 核心编排引擎（`rca-agent-engine/`，约 2,800 行）—— *春招面试重心*
```text
rca-agent-engine/
├── app/
│   ├── main.py                  # FastAPI 入口（提供 SSE 节点流/Token 流接口、HITL 恢复接口）
│   ├── models/
│   │   ├── state.py             # LangGraph 全局 State 与子图 State 定义
│   │   └── incident.py          # 复刻并扩展 open-swe 的 Evidence, Hypothesis, IncidentReport 模型
│   ├── graphs/
│   │   ├── supervisor_graph.py  # 主控图：故障分诊 -> 历史CRAG -> 多假设并行取证 -> 报告与补丁 -> HITL审批
│   │   ├── code_subgraph.py     # 代码探针子图：RepoMap 导航 + AST/窗口读取 + 调用链追踪
│   │   ├── data_subgraph.py     # 数据探针子图：只读 NL2SQL 校验数据库异常状态与脏数据
│   │   └── crag_subgraph.py     # 历史故障检索子图：ES+Qdrant RRF 混合检索 + 相关性评分器 (Grader)
│   ├── mcp_tools/
│   │   ├── codebase_mcp.py      # 本地代码工具集：ls, glob, grep, read_ast_symbol, read_window, git_blame
│   │   ├── log_es_mcp.py        # ES 日志工具集：按时间/TraceID/级别检索错误堆栈与上下文日志
│   │   ├── mysql_probe_mcp.py   # MySQL 数据核验工具集：表结构获取、只读 SQL 安全执行
│   │   └── sandbox_patch_mcp.py # 补丁生成、Git Worktree 隔离验证、单元测试执行工具
│   ├── context/
│   │   ├── repomap.py           # 基于 tree-sitter 构建代码库类/方法签名与引用拓扑图
│   │   ├── compressor.py        # 历史上下文动态折叠处理器 (Observation Masking)
│   │   └── verifier.py          # 证据链 ID 真实性校验器（防幻觉拦截）
│   └── memory/
│       ├── checkpointer.py      # 基于 Redis 的状态机断点持久化
│       └── runbook_evolver.py   # 故障修复后自动归档入库 Qdrant & ES 的自演进模块
```

#### 子系统 2：Python FastAPI 平台业务与持久化后端（`rca-platform-api/`，约 2,800 行）—— *纯 Python 支撑毕设工程量与多表设计*
（完全参考 `open-swe/openswe/` 的工程写法，使用 **Python `FastAPI` + `SQLAlchemy 2.0 (Async)` + `Pydantic v2`**，无需写任何 Java 代码）
* **核心功能模块**：
  1. **监控告警接入与工单中心（`incidents/`）**：接收业务异常 Webhook 或巡检任务，使用 SQLAlchemy 管理故障工单全生命周期（待排查 / AI 排查中 / 待人工审批 / 已修复 / 已归档）。
  2. **LangGraph 会话与轨迹持久化管理（`threads/`）**：管理每个排障线程的元数据、MongoDB 历史对话轨迹、Redis 断点状态与 SSE 实时事件流推送。
  3. **项目仓库与多源连接器管理（`workspaces/`）**：管理被监控项目的本地 Git 路径、ES 索引配置、MySQL 数据源连接。
  4. **知识库与复盘报告自演进中心（`knowledge/`）**：管理历史故障复盘文档、运维手册（Runbook）切片、Qdrant 向量入库与检索测试接口。
  5. **代码补丁审计、Git 回滚与可观测性统计接口（`analytics/`）**：记录每次 AI 生成的 Diff 补丁、审批记录，统计 MTTR（平均修复耗时）、根因定位准确率与 Token 消耗。

#### 子系统 3：Vue3 可视化排障工作台前端（`rca-dashboard-web/`，约 2,800 行）
* **五大核心页面**：
  1. **AIOps 态势感知大屏（ECharts）**：告警趋势、AI 自动治愈率、节点耗时分布、Token 成本监控。
  2. **LangGraph 实时排障工作台（核心演示页）**：
     * 左栏：实时可视化展示 LangGraph 状态图各节点的流转与并发执行状态；
     * 中栏：流式展示 Agent 提出的假设（`Hypothesis`）列表、验证状态标签（证实/排除）及可点击展开的证据卡片（日志堆栈、代码切片、SQL 查询结果）；
     * 右栏：Monaco Diff Editor 展示修复前后的代码对比，提供 **【批准应用补丁】** 与 **【驳回并附加指导意见】** 按钮（联动 LangGraph `interrupt`）。
  3. **历史故障知识库与 CRAG 检索测试台**。
  4. **被监控靶场项目与 MCP 工具管理页**。
  5. **自动化基准评测（Benchmark）实验对比看板**。

#### 子系统 4：微服务故障靶场与自动化评测集（`rca-benchmark-suite/`，约 1,200 行）—— *支撑简历量化指标与毕设实验章节*
* **内置故障靶场工程**：一个包含订单、库存、优惠券、支付模块的典型后端工程，预设 **25~30 个真实故障场景**（涵盖：空指针异常、MyBatis 字段映射错误、事务失效、优惠券脏数据导致计算异常、Redis 缓存序列化错误、配置项超时等）。
* **自动化消融实验评测脚本**：
  * 自动触发故障并对比不同架构配置下的表现（如：`Baseline 纯 ReAct` vs `+ AST RepoMap & 上下文折叠` vs `+ 多源探针 (ES+NL2SQL) & CRAG`）；
  * 自动计算并输出核心指标：**根因定位准确率（Root Cause Accuracy）**、**证据引用准确率（Citation Precision）**、**补丁单测通过率（Pass@1）**、**平均单次排障 Token 消耗与耗时**。

---

### 三、 分阶段开发实施路线图

| 阶段 | 周期安排 | 开发重点 | 阶段里程碑产出 |
| :--- | :--- | :--- | :--- |
| **Step 0：源码预热** | **第 1 天** | 读完第一部分列出的 `open-swe` 5 个核心文件 | 彻底掌握排障 SOP 提示词、证据链模型与文件工具设计 |
| **Step 1：最小闭环 (MVP)** | **第 2 ~ 4 天** | 搭建 `rca-agent-engine` 基础架子：实现本地代码探针（`ls`/`grep`/`read_window`）+ LangGraph 基础排障图 + 结构化 `IncidentReport` 输出 | 给定一段报错或模糊现象，Agent 能自动在本地目录搜代码、读文件并输出带证据引用的排障报告 |
| **Step 2：五大硬核深度叠甲** | **第 5 ~ 14 天** | 依次加入：① `tree-sitter` AST 切片与 RepoMap；② ES 日志 + MySQL NL2SQL 并行探针；③ Qdrant+ES 混合检索 CRAG；④ 上下文动态折叠；⑤ Git 隔离单测自愈 + `interrupt` 人工审批 | 完成春招面试最核心的 2,800 行高质量 Python AI 引擎代码 |
| **Step 3：前端工作台与评测集** | **第 15 ~ 24 天** | 完成 Vue3 实时排障工作台页面 + 跑通 25 个故障场景的自动化评测脚本 | 产出简历上的**量化性能指标**与面试演示系统，随时可投递春招简历 |
| **Step 4：毕设完整扩充** | **春招投递期间** | 补齐 Python FastAPI 业务管理模块（SQLAlchemy 工单表、配置表、知识库管理、统计看板接口）与其余前端页面 | 达到 ~10,000 行纯 Python + Vue3 完整工程体量，直接支撑本科毕业设计答辩与论文撰写 |
