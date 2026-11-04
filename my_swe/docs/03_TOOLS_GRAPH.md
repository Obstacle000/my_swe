# Tools & Graph Specification

> **文档说明**：本文档承接《02_STATE_SCHEMA_MODEL.md》，详细定义：
> 1. **Tools（四大工具集契约）**：所有给大模型调用的工具函数签名、入参说明、返回值格式、安全防护及自动签发 `Evidence` 的包装器机制；
> 2. **Graph（1 个主图 + 3 个子图）**：每个节点（Node）的具体处理逻辑、读写哪些 `State` 字段、以及每条条件边（Conditional Edge / `Send` API）的路由代码。

---

## 第一部分：工具契约设计（Tools Specification）

### 一、 核心底层机制：工具结果自动转 `Evidence` 与脱敏限流器

在 `ProbeWorkerState`（探针子图）中，大模型调用任何工具（查日志、读代码、查数据库）时，**不会直接返回裸字符串**，而是经过统一的 `record_tool_evidence()` 包装器处理（复刻自 `open-swe/openswe/incidents/evidence_tools.py`）：

```python
import hashlib, json, re
from app.schemas.incident_schemas import Evidence

# 自动脱敏正则：过滤日志或配置文件中的密码、API Key、Bearer Token
_SECRET_PATTERN = re.compile(
    r"(?i)(?:\b(?:sk-|gh[pousr]_)\w+|(?:password|secret|api[_-]?key|token)\s*[:=]\s*[^\s,;]+)"
)

def redact_text(text: str, max_chars: int = 4000) -> str:
    cleaned = _SECRET_PATTERN.sub("[REDACTED]", text)
    return cleaned[:max_chars]

def build_evidence(source: str, tool_name: str, query_args: dict, raw_output: str, summary: str) -> Evidence:
    """根据工具调用参数与返回内容，生成不可伪造的唯一 Evidence 对象"""
    args_str = json.dumps(query_args, ensure_ascii=False, sort_keys=True)
    short_hash = hashlib.sha256(f"{source}:{tool_name}:{args_str}:{raw_output}".encode()).hexdigest()[:6]
    prefix_map = {"es_log": "ev_log", "codebase": "ev_code", "mysql_db": "ev_db", "git_history": "ev_git"}
    ev_id = f"{prefix_map.get(source, 'ev')}:{short_hash}"

    return Evidence(
        id=ev_id,
        source=source,
        tool_name=tool_name,
        query_args=args_str,
        summary=summary[:500],
        raw_snippet=redact_text(raw_output, max_chars=4000),
        retrieved_at=now_iso(),
    )
```
* **返回给大模型看的字符串末尾会自动附上证据编号**：
  ```text
  [工具原始返回内容...]
  ---
  [系统已登记真实证据编号: ev_code:8f3a1c] (在报告中引用此结论时必须填写该编号)
  ```

---

### 二、 四大工具集详细契约（MCP Toolsets）

为了防止大模型乱调工具，系统将工具按职责划分为 **4 个独立工具集**，按需绑定给对应的子图：

---

#### 工具集 1：ES 运行时日志探针工具（`app/tools/log_tools.py`）
> **挂载对象**：仅挂载给 `probe_type == "log"` 的 `ProbeWorkerState` 探针子图。  
> **证据来源标识**：`source = "es_log"`

##### 1. `search_error_logs`
* **函数签名**：
  ```python
  @tool
  async def search_error_logs(
      keyword: str = "",
      user_id: str = "",
      api_path: str = "",
      level: Literal["ERROR", "WARN", "ALL"] = "ERROR",
      time_range_minutes: int = 60,
  ) -> str
  ```
* **参数用法**：
  * `keyword`：报错关键词或异常名（如 `"TypeError"`, `"timeout"`）；
  * `user_id`：用户 ID（如 `"10086"`）；
  * `api_path`：接口路由（如 `"/api/orders/checkout"`）；
  * `level`：默认只查 `"ERROR"` 级别日志。
* **返回格式示例**：
  ```text
  共检索到 1 条匹配日志：
  [2026-11-04 09:10:12] [ERROR] [trace_id=req-8821] [user_id=10086] [POST /api/orders/checkout]
  File "app/services/order_service.py", line 64, in checkout_order
      final_price = total_price - coupon.discount_amount
  TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'
  ```
* **核心作用**：当用户只提供模糊线索（不知道哪个文件报错）时，用它从 ES 里捞出 `trace_id`、报错文件名 `order_service.py` 和行号 `64`。

##### 2. `get_trace_logs`
* **函数签名**：
  ```python
  @tool
  async def get_trace_logs(trace_id: str) -> str
  ```
* **核心作用**：拿到上一步的 `trace_id`（如 `"req-8821"`）后，按时间先后顺序拉取该次请求从进入 Controller、调用 Service 到报错崩溃的**完整全链路上下文日志**（含 `INFO` 级打印的入参日志）。

---

#### 工具集 2：本地代码库与 Git 探针工具（`app/tools/code_tools.py`）
> **挂载对象**：仅挂载给 `probe_type == "code"` 的 `ProbeWorkerState` 探针子图。  
> **吸收设计**：结合 `open-swe` 的 `FILE_TOOLS` 与 `SWE-agent` 的行号滑动窗口（ACI）。  
> **安全红线**：所有文件路径必须经过 `_safe_resolve(repo_root, relative_path)` 校验（确保 `target_path.is_relative_to(repo_root)`），严禁大模型通过 `../../` 越权读取项目目录以外的电脑私人文件！

##### 1. `ls_directory`
* **函数签名**：
  ```python
  @tool
  def ls_directory(relative_path: str = ".") -> str
  ```
* **作用**：查看目标项目某文件夹下的文件与子目录列表（自动过滤 `.git`、`__pycache__`、`node_modules`、`.venv`）。

##### 2. `grep_code`
* **函数签名**：
  ```python
  @tool
  def grep_code(pattern: str, relative_path: str = ".", context_lines: int = 2) -> str
  ```
* **参数用法**：
  * `pattern`：要搜索的字符串或正则表达式（比如搜接口路径 `"/orders/preview"` 或函数名 `"def calc_vip_discount"`）；
  * `context_lines`：返回匹配行上下各几行代码（默认 2 行）。
* **返回格式示例**：
  ```text
  app/api/order_router.py:28: @router.post("/orders/preview")
  app/api/order_router.py:29: async def preview_order(dto: OrderDTO):
  app/api/order_router.py:30:     return await pricing_service.calc_vip_discount(dto)
  ```
* **核心作用**：专门应对**“剧本二（没有报错日志堆栈的逻辑 Bug）”**——通过搜 URL 路由或函数名，顺藤摸瓜锁定代码位置。

##### 3. `read_file_window`（最核心的读代码工具）
* **函数签名**：
  ```python
  @tool
  def read_file_window(file_path: str, offset: int = 1, limit: int = 60) -> str
  ```
* **参数用法**：
  * `file_path`：文件相对路径（如 `"app/services/order_service.py"`）；
  * `offset`：起始行号（从第几行开始读，比如报错在第 64 行，大模型会传 `offset=40`）；
  * `limit`：读取多少行（默认 60 行，最大不超过 120 行，防止一次读整个大文件撑爆 Token）。
* **返回格式示例（自动带行号！）**：
  ```python
  # 文件: app/services/order_service.py (展示第 55 ~ 70 行，共 210 行)
    55 | async def checkout_order(db: AsyncSession, user_id: int, dto: CheckoutDTO):
    56 |     total_price = await calc_items_total(db, dto.items)
    ...
    62 |     coupon = await coupon_repo.get_by_id(db, dto.coupon_id)
    63 |     if coupon:
    64 |         final_price = total_price - coupon.discount_amount
    65 |     return OrderResult(final_price=final_price)
  ```

##### 4. `git_blame_diff`
* **函数签名**：
  ```python
  @tool
  def git_blame_diff(file_path: str, start_line: int, end_line: int) -> str
  ```
* **实现原理**：在项目根目录执行 `git log -n 2 -L {start_line},{end_line}:{file_path}`。
* **核心作用**：查看报错的那几行代码最近是被哪次 Git Commit 修改过的、修改前后的 Diff 是什么，瞬间抓出“昨天刚发版改坏的回归 Bug”。

---

#### 工具集 3：MySQL 数据库只读核验工具（`app/tools/db_tools.py`）
> **挂载对象**：仅挂载给 `probe_type == "db"` 的 `ProbeWorkerState` 探针子图。  
> **安全红线**：必须通过正则 `^\s*SELECT\b` 校验且禁止包含 `UPDATE|DELETE|INSERT|DROP|ALTER|TRUNCATE` 关键字，强制使用只读数据库账号连接！

##### 1. `inspect_table_schema`
* **函数签名**：
  ```python
  @tool
  async def inspect_table_schema(table_name: str = "") -> str
  ```
* **作用**：如果不传 `table_name`，返回数据库所有表名列表；如果传了表名（如 `"coupons"`），返回该表的字段名、类型和注释，供大模型写 SQL 前参考。

##### 2. `execute_readonly_sql`
* **函数签名**：
  ```python
  @tool
  async def execute_readonly_sql(sql_query: str) -> str
  ```
* **参数用法**：传入一条 `SELECT` 查询语句（若未写 `LIMIT`，代码自动在末尾追加 `LIMIT 20` 防止拉取全表数据）。
* **核心作用**：核查是不是因为**数据库里进了脏数据、状态值非法或字段为 NULL** 才触发了代码报错。

---

#### 工具集 4：Git Worktree 沙箱改代码与测试工具（`app/tools/sandbox_tools.py`）
> **挂载对象**：仅挂载给子图 3（`PatchSelfHealSubState` 代码自愈子图），排查阶段的探针**绝不挂载**此工具集（确保排查阶段 100% 只读安全）。

##### 1. `edit_file_in_sandbox`
* **函数签名**：
  ```python
  @tool
  def edit_file_in_sandbox(file_path: str, old_code: str, new_code: str) -> str
  ```
* **作用**：在临时沙箱目录 `worktree_path` 下，将 `file_path` 中的 `old_code` 精确替换为修复后的 `new_code`（要求 `old_code` 在文件中唯一匹配，替换后自动用 `ast.parse()` 检查是否存在 Python 语法缩进错误，若语法错误当场返回提示让模型重改）。

---

## 第二部分：LangGraph 状态图节点与路由设计（Graph & Nodes）

整个系统由 **3 个独立编译的子图（Subgraphs）** 组装进 **1 个主图（Main Graph）** 构成。

---

### 一、 子图 1：`crag_subgraph`（历史故障检索自省子图）
* **绑定状态**：`CRAGSubState`
* **内部节点与边逻辑**：

| 节点 / 边名称 | 类型 | 读取字段 | 核心执行逻辑 | 更新返回字段 |
| :--- | :--- | :--- | :--- | :--- |
| `hybrid_retrieve_node` | Node | `query`, `rewritten_query` | 取 `rewritten_query or query`，通过 `asyncio.gather` 并发检索 ES(BM25) 与 Qdrant(向量)，用 **RRF 公式**合并排序取 Top-3 | `es_docs`, `qdrant_docs`, `rrf_ranked_docs` |
| `grade_docs_node` | Node | `query`, `rrf_ranked_docs` | 若 `rrf_ranked_docs` 为空直接记 `"no"`；否则调用 `llm.with_structured_output(RetrievalGrade)` 判断候选案例是否与当前故障同属一类问题 | `relevance_score` (`"yes"` / `"no"`) |
| `route_after_grade` | Edge | `relevance_score`, `retry_count` | 若 `relevance_score == "yes"` 或 `retry_count >= 1` $\rightarrow$ 跳至 `pack_output_node`；否则 $\rightarrow$ 跳至 `rewrite_query_node` | 路由跳转 |
| `rewrite_query_node` | Node | `query`, `retry_count` | 让 LLM 去除口语化词汇和具体 ID，提炼异常类名与模块名重写搜索词 | `rewritten_query`, `retry_count: retry_count + 1` |
| `pack_output_node` | Node | `relevance_score`, `rrf_ranked_docs` | 若 `relevance_score == "yes"`，将命中的历史故障写入 `historical_cases`，并调用 `build_evidence(source="runbook", ...)` 生成 `[ev_hist:xxxx]` 塞入 `evidence_ledger`；若为 `"no"` 则返回空列表 | `historical_cases`, `evidence_ledger` |

---

### 二、 子图 2：`probe_worker_subgraph`（多源并行取证特工子图）
* **绑定状态**：`ProbeWorkerState`
* **内部节点与边逻辑**：

| 节点 / 边名称 | 类型 | 读取字段 | 核心执行逻辑 | 更新返回字段 |
| :--- | :--- | :--- | :--- | :--- |
| `probe_agent_node` | Node | `hypothesis`, `worker_messages`, `remaining_chars` | 根据 `hypothesis.probe_type` 动态绑定工具集：<br>- `"log"` $\rightarrow$ 绑工具集 1<br>- `"code"` $\rightarrow$ 绑工具集 2<br>- `"db"` $\rightarrow$ 绑工具集 3<br>指示模型围绕 `hypothesis.title` 调用工具查证 | `worker_messages` (追加 `AIMessage`) |
| `should_continue_probe`| Edge | `worker_messages`, `remaining_chars` | 若最后一条 `AIMessage` 没有 `tool_calls` $\rightarrow$ 结束子图 (`END`)；<br>若 `remaining_chars <= 0` $\rightarrow$ 跳至 `budget_exceeded_node`；<br>否则 $\rightarrow$ 跳至 `probe_tool_node` | 路由跳转 |
| `probe_tool_node` | Node | `worker_messages`, `remaining_chars` | 1. 执行大模型请求的 `tool_call`；<br>2. 调用 `build_evidence()` 自动签发 `Evidence` 对象；<br>3. 扣减 `remaining_chars -= len(snippet)`；<br>4. 将附带 `[ev_xxx:id]` 的 `ToolMessage` 塞入 `worker_messages` | `worker_messages`, `evidence_ledger: [new_ev]`, `remaining_chars` |

---

### 三、 子图 3：`patch_self_heal_subgraph`（Git Worktree 沙箱代码修复与单测自愈子图）
* **绑定状态**：`PatchSelfHealSubState`
* **内部节点与边逻辑**：

| 节点 / 边名称 | 类型 | 读取字段 | 核心执行逻辑 | 更新返回字段 |
| :--- | :--- | :--- | :--- | :--- |
| `init_worktree_node` | Node | `target_project_id` | 在临时目录创建 Git Worktree 隔离副本（若已存在则重置干净），避免直接修改原项目代码 | `worktree_path`, `retry_count: 0` |
| `generate_patch_node`| Node | `final_report`, `evidence_ledger`, `test_error_log`, `human_feedback` | 结合根因报告、`evidence_ledger` 中的源码片段（以及上一轮失败的 `test_error_log` 或人工驳回意见 `human_feedback`），调用 `edit_file_in_sandbox` 修改代码，并执行 `git diff` 获取补丁文本 | `target_file`, `patch_diff` |
| `run_pytest_node` | Node | `worktree_path`, `test_command`, `retry_count` | 在 `worktree_path` 下通过 `subprocess` 执行 `pytest`（设置 30 秒超时）：<br>- 返回码为 `0` $\rightarrow$ `test_passed = True`<br>- 返回码非 `0` $\rightarrow$ `test_passed = False`，截取报错堆栈存入 `test_error_log` | `test_passed`, `test_error_log`, `retry_count: retry_count + 1` |
| `route_after_pytest` | Edge | `test_passed`, `retry_count` | 若 `test_passed == True` 或 `retry_count >= 3` $\rightarrow$ 退出子图 (`END`)，将 `patch_diff` 和 `test_passed` 交回主图；<br>否则 $\rightarrow$ 跳回 `generate_patch_node` 进行报错自愈重改 | 路由跳转 |

---

### 四、 主图：`incident_main_graph`（全局排障总控图）
* **绑定状态**：`IncidentMainState`
* **节点与核心路由函数代码实现规范**：

#### 1. 节点清单
1. **`triage_node`**：从 `user_query` 中提取 `user_id`、`trace_id`、`api_path` 等线索存入 `extracted_clues`。
2. **`crag_subgraph`（挂载子图 1）**：传入 `query`，拿回 `historical_cases` 和历史证据。
3. **`hypothesis_planner_node`**：
   * 读取 `extracted_clues`、`historical_cases` 和当前已有的 `evidence_ledger`；
   * 若是第 1 轮且连报错在哪个文件都不知道，先生成 1 个 `"log"` 假设（查 ES 报错堆栈）或 `"code"` 全局路由搜索假设；
   * 若已知报错位置，生成 2~3 个竞争性假设 `Hypothesis(id="h1", probe_type="code", ...)`、`"db"` 等，写入 `hypotheses`。
4. **`dispatch_probes_edge`（`Send` API 动态并行分发边——核心代码）**：
   ```python
   from langgraph.types import Send
   
   def dispatch_probes_edge(state: IncidentMainState) -> list[Send]:
       """将所有待验证 (plausible) 的假设，动态并行派发给 probe_worker_subgraph 子图"""
       pending_hypotheses = [h for h in state["hypotheses"] if h.assessment == "plausible"]
       return [
           Send(
               "probe_worker_subgraph",
               {
                   "hypothesis": h,
                   "worker_messages": [],
                   "remaining_chars": 15000,
                   "evidence_ledger": [],
                   "gaps": [],
               },
           )
           for h in pending_hypotheses
       ]
   ```
5. **`context_compressor_node`（证据汇总与上下文折叠节点）**：
   * 所有并行的 `probe_worker_subgraph` 跑完后，它们查到的 `evidence_ledger` 和 `gaps` 已通过 `operator.add` 自动合并到了主图；
   * 本节点让大模型根据新收集到的 `evidence_ledger`，将各个 `Hypothesis.assessment` 更新为 `"supported"`（已证实）或 `"rejected"`（已排除），并递增 `probe_round += 1`。
6. **`route_after_compressor`（判断是否需要第二轮深挖）**：
   * 若已有假设被标记为 `"supported"`（已锁定确凿根因）或 `probe_round >= 2` $\rightarrow$ 进入 `draft_report_node`；
   * 若第一轮只查了日志拿到了报错文件名，还没看具体代码 $\rightarrow$ 跳回 `hypothesis_planner_node` 开启第 2 轮代码与数据库探针。
7. **`draft_report_node`（填写 `ReportDraft` 草稿）**：
   * 加载汉化版 `report.md` 排障五步法提示词，把 `evidence_ledger` 里的所有真实证据编号与摘要列给大模型，强制调用 `llm.with_structured_output(ReportDraft)` 生成报告草稿。
8. **`verify_report_node`（复刻 `open-swe` 的代码级防幻觉验钞机）**：
   ```python
   def verify_report_node(state: IncidentMainState) -> dict:
       draft: ReportDraft = state["report_draft"]
       known_ids = {ev.id for ev in state["evidence_ledger"]}
       
       # 检查核心根因 (cause) 是否为空或引用了不存在的伪造 evidence_id
       invalid_citations = [
           cid for claim in draft.cause for cid in claim.evidence_ids if cid not in known_ids
       ]
       missing_evidence = any(len(claim.evidence_ids) == 0 for claim in draft.cause)
   
       if (invalid_citations or missing_evidence) and state["verify_retry_count"] < 2:
           # 抓到幻觉！递增重试计数，记录警告并通过条件边打回给 draft_report_node 重写
           return {
               "verify_retry_count": state["verify_retry_count"] + 1,
               "gaps": [f"检测到未绑定真实证据或伪造证据ID {invalid_citations}，已拦截打回重填"],
           }
   
       # 验钞通过：调用 finalize_report() 拼接角标、计算 fingerprint 指纹，生成正式 IncidentReport
       final_report = finalize_report(draft, state["evidence_ledger"], state["gaps"])
       return {"final_report": final_report}
   ```
9. **`route_after_verifier`（验钞后路由）**：
   * 若 `state.get("final_report") is None`（说明刚被验钞机拦截打回了） $\rightarrow$ 跳回 `draft_report_node`；
   * 若验钞通过且 `final_report.need_code_patch == True` $\rightarrow$ 进入子图 3 `patch_self_heal_subgraph` 生成代码补丁；
   * 若 `need_code_patch == False`（比如只是外部限流配置问题或待用户补充线索） $\rightarrow$ 直接结束 (`END`)。
10. **`human_approval_node`（LangGraph `interrupt` 人工审批节点）**：
    ```python
    from langgraph.types import interrupt
    
    def human_approval_node(state: IncidentMainState) -> dict:
        # 挂起图执行，将排障报告、Git Diff 补丁和单测结果推送给前端等待人工点击按钮
        decision = interrupt({
            "incident_id": state["incident_id"],
            "report": state["final_report"].model_dump(),
            "patch_diff": state["patch_diff"],
            "test_passed": state["test_passed"],
        })
        # 当前端调用 Command(resume={"action": "approved", "feedback": "..."}) 唤醒时继续往下走：
        return {
            "human_approval": decision.get("action", "rejected"),
            "human_feedback": decision.get("feedback"),
        }
    ```
11. **`route_after_approval` & `apply_and_evolve_node`（收尾与知识库自演进）**：
    * 若 `human_approval == "rejected"` 且附带了 `human_feedback` $\rightarrow$ 跳回 `patch_self_heal_subgraph` 按人工意见重改代码；
    * 若 `human_approval == "approved"` $\rightarrow$ 进入 `apply_and_evolve_node`：将 `patch_diff` 正式应用到目标代码仓库，并将本次 `final_report` + `patch_diff` 写入 MySQL `knowledge_runbooks` 表及 **ES + Qdrant** 历史故障库，完成经验自演进闭环！
