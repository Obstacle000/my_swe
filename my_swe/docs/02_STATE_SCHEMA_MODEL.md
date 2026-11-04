#  State & Schema&model

## State

项目里，一共只需要设计 **1 个主图 State + 3 个子图 State**：

#####  **主图全局状态：`IncidentMainState(TypedDict)`**

```py
class IncidentMainState(TypedDict):
    # 1. 基础输入与提取出的线索
    incident_id: str
    user_query: str                                          # 用户的原始提问
    extracted_clues: dict[str, str]                          # 提炼出的 user_id, trace_id, url 等
    messages: Annotated[list[AnyMessage], add_messages]      # 主图消息历史

    # 2. 排障核心数据（里面装的就是上面定义的 BaseModel 对象！）
    historical_cases: list[dict]                             # 历史库检索子图返回的结果
    hypotheses: list[Hypothesis]                             # 当前提出的所有假设列表
    evidence_ledger: Annotated[list[Evidence], operator.add] # 各探针子图汇总上来的真实证据池
    gaps: Annotated[list[str], operator.add]                 # 各探针汇总上来的排查盲区

    # 3. 报告与防幻觉校验状态
    report_draft: ReportDraft | None                         # 大模型交上来的草稿
    final_report: IncidentReport | None                      # 验钞通过后的正式报告
    verify_retry_count: int                                  # 防幻觉打回重填次数

    # 4. 代码修复与人工审批状态
    patch_diff: str | None                                   # 代码修复子图生成的 Diff 补丁
    test_passed: bool                                        # 沙箱单测是否通过
    human_approval: Literal["pending", "approved", "rejected"] # 人工审批状态
```

**第一组：基础输入与线索字段**

**1. `incident_id: str`**

- **含义**：本次故障排障任务的唯一工单编号（比如 `"INC-20261104-001"`）。可以直接当作`thread_id`
- **谁写入的**：用户发起请求时，由最外层传入。
- **作用**：用来关联 Redis 断点状态（`thread_id`）和最后存数据库时的主键。

**2. `user_query: str`**

- **含义**：用户最开始输入的原始故障描述或告警原文。
- **例子**：`"刚刚用户 user_id=10086 在结算页提交订单报错 500，帮我查原因并修复"`。
- **谁写入的**：启动图时初始传入，全程只读不改。

**3. `extracted_clues: dict[str, str]`**

- **含义**：从 `user_query` 里提取出来的**结构化搜索关键词字典**。
- **例子**：`{"user_id": "10086", "module": "订单结算", "error_code": "500", "api_path": ""}`。
- **谁写入的**：第 1 个节点 `triage_node`（分诊节点）。
- **作用**：给后面的历史检索子图和日志探针直接拿去当搜索条件用，不用每次都重新解析用户原话。

**4. `messages: Annotated[list[AnyMessage], add_messages]`**

- **含义**：主图自己的对话消息列表（存 `SystemMessage`、`HumanMessage`、`AIMessage`）。
- **为什么带 `add_messages`**：这是 LangGraph 内置的追加器（Reducer）。节点返回 `{"messages": [新消息]}` 时，不会覆盖旧消息，而是自动追加到列表末尾。

**第二组：排障核心证据与假设字段（取证阶段产生）**

**5. `historical_cases: list[dict]`**

- **含义**：从 Qdrant + ES (RRF合并)历史故障库里查出来的相似历史故障单列表。
- **例子**：`[{"case_id": "INC-089", "title": "短信接口超时", "solution": "开启异步队列开关"}]`（如果没查到相似的，就是空列表 `[]`）。
- **谁写入的**：`CRAGSubState`（历史故障检索子图）跑完后写回给主图。

**6. `hypotheses: list[Hypothesis]`**

- **含义**：主图规划节点提出的**待验证排障假设列表**，以及它们验证后的状态（`plausible` 待定 / `supported` 已证实 / `rejected` 已排除）。
- **例子**
  - 假设 1：`Hypothesis(id="h1", title="order_service.py 计算折扣未判空", probe_type="code", assessment="supported")`
  - 假设 2：`Hypothesis(id="h2", title="MySQL 优惠券表存在脏数据", probe_type="db", assessment="rejected")`
- **谁写入的**：`hypothesis_planner_node` 生成初始假设；探针跑完后由汇总节点更新它们的 `assessment` 状态。

**7. `evidence_ledger: Annotated[list[Evidence], operator.add]`**

- **含义**：**真实证据总账本（白名单）**！所有探针调工具（查 ES、读代码、查 MySQL）拿到的真实结果，都会自动打包成 `Evidence(id="ev_code:01", ...)` 存进这里。
- **为什么必须带 `Annotated[..., operator.add]`（重点！）**： 因为我们会用 `Send` API **同时并行启动 3 个 `ProbeWorkerState` 探针子图**（一个查日志、一个查代码、一个查数据库）。 如果不加 `operator.add`，3 个并行子图同时结束写回 `evidence_ledger` 时，最后一个完成的子图会**把前两个子图查到的证据覆盖掉**！加上 `operator.add` 后，LangGraph 会自动把 3 个并行子图返回的证据列表拼接（`list1 + list2 + list3`）合并到一起。

**8. `gaps: Annotated[list[str], operator.add]`**

- **含义**：排查过程中遇到的**盲区或失败记录**（即昨天你在 `open-swe` 里看到的 `gaps`）。
- **例子**：`["ES 缺少支付网关日志索引", "已剔除 1 条缺少真实证据引用的结论"]`。
- **为什么带 `operator.add`**：和上面一样，允许多个并行探针子图以及后面的验钞节点各自往里追加盲区说明，互不覆盖。

**第三组：报告草稿与防幻觉验钞字段（总结阶段产生）**

**9. `report_draft: ReportDraft | None`**

- **含义**：大模型刚刚填写交上来的**待审报告草稿**（里面的每句结论都是 `Claim(text="...", evidence_ids=["ev_code:01"])`，还没经过 Python 验钞）。
- **谁写入的**：`draft_report_node`（报告草稿生成节点）。

**10. `final_report: IncidentReport | None`**

- **含义**：经过 Python `finalize_report()` 核对 `evidence_ledger` 白名单、剔除掉瞎猜幻觉之后，最终生成的**正式排障报告对象**。
- **谁写入的**：`verify_report_node`（防幻觉验钞节点）。

**11. `verify_retry_count: int`**

- **含义**：防幻觉打回重填的**计数器**（初始为 `0`）。
- **作用**：如果 `verify_report_node` 发现大模型在 `report_draft` 里瞎编了不存在的 `evidence_id`，就会把 `verify_retry_count + 1` 并通过条件边打回给 `draft_report_node` 重写。为了防止大模型一直犯傻陷入死循环，条件边会判断 `if state["verify_retry_count"] >= 2:` 就强制放行结束。

**第四组：代码补丁与人工审批字段（修复阶段产生）**

**12. `patch_diff: str | None`**

- **含义**：针对定位出的代码 Bug，修改源码后生成的标准 **Git Diff 代码对比文本**（如果本次故障是外部配置问题不需要改代码，则为 `None`）。
- **谁写入的**：`PatchSelfHealSubState`（代码补丁与单测自愈子图）跑完后写回给主图，用来在 Vue3 页面右边展示红绿代码对比。

**13. `test_passed: bool`**

- **含义**：上面的 `patch_diff` 补丁在沙箱里跑 `pytest` 单元测试**有没有通过**（`True` / `False`）。
- **谁写入的**：`PatchSelfHealSubState` 子图。

**14. `human_approval: Literal["pending", "approved", "rejected"]`**

- **含义**：人工审批断点的状态（`pending` 等待人工点击 / `approved` 人工已批准 / `rejected` 人工已驳回）。
- **谁写入的**：图走到 `human_approval_node` 时调用 `interrupt()` 暂停；当你在前端点击【批准】按钮后，恢复执行并把这个字段更新为 `"approved"`，随后进入最后一个节点把补丁真正写进文件并归档到知识库。*]



#####  **子图 1 状态——历史故障检索子图：`CRAGSubState(TypedDict)`**

```py

class CRAGSubState(TypedDict):
    query: str                        # 从主图拿过来的搜索词
    rewritten_query: str              # 子图内部重写后的搜索词
    es_docs: list[dict]               # 子图内部：ES 召回结果
    qdrant_docs: list[dict]           # 子图内部：Qdrant 召回结果
    rrf_ranked_docs: list[dict]       # 子图内部：RRF 重排结果
    relevance_score: str              # 子图内部：Grader 相关性打分 ("yes" / "no")
    retry_count: int                  # 子图内部：重写搜索重试次数
    # 最终写回给主图的字段：
    historical_cases: list[dict]
    evidence_ledger: Annotated[list[Evidence], operator.add]
```

**1. `query: str`（输入字段）**

- **含义**：刚进入子图时，用于去历史故障库搜索的初始查询词。
- **例子**：`"订单结算报错 500 user_id=10086"`（由主图根据 `user_query` 和 `extracted_clues` 拼好传进来）。

**2. `rewritten_query: str`（重写搜索词）**

- **含义**：如果第一轮用原始 `query` 搜出来的文档都不相关（比如原始 `query` 带了太多像 `"10086"`、`"刚刚"` 这种干扰词），由大模型改写提炼后的**更专业的搜索词**。
- **例子**：从 `"刚刚用户10086结算报错500"` 改写为 `"checkout order 500 Internal Server Error coupon"`。
- **谁写入的**：`rewrite_query_node`（Query 重写节点）。下一轮重搜时，检索节点会优先用 `rewritten_query` 去搜。

**3. `es_docs: list[dict]`（ES 召回列表）**

- **含义**：用当前搜索词去 **Elasticsearch** 做 BM25 关键词匹配，搜出来的前 10 条历史故障记录。
- **例子**：`[{"case_id": "INC-01", "title": "...", "cause": "..."}, ...]`。
- **为什么单独存一个字段**：方便你在调试或评测时，清楚看到到底是 ES 搜出来的还是 Qdrant 搜出来的。

**4. `qdrant_docs: list[dict]`（Qdrant 召回列表）**

- **含义**：用当前搜索词转成向量，去 **Qdrant** 做余弦相似度匹配，搜出来的前 10 条历史故障记录。

**5. `rrf_ranked_docs: list[dict]`（RRF 融合排序后的 Top-K 列表）**

- **含义**：把上面的 `es_docs` 和 `qdrant_docs` 用刚才讲的 **RRF（倒数秩融合）公式** 合并打分后，排在最前面的 **前 3 条（Top-3）** 候选历史故障文档。
- **作用**：这 3 篇只是“机器觉得像”的候选文档，接下来要交给大模型裁判（Grader）去检查它们到底跟当前故障有没有真关系。

**6. `relevance_score: str`（Grader 相关性裁判打分）**

- **含义**：大模型裁判看完 `rrf_ranked_docs`里的候选文档后，给出的判定结果，取值只有两个：

  `Literal["yes", "no"]`

  - `"yes"`：候选文档里确实有跟当前报错高度吻合的历史故障案例；
  - `"no"`：搜出来的 3 篇全是八竿子打不着的旧故障（比如当前是“订单报 500”，搜出来的却是“去年登录页面样式错位”）。

- **谁写入的**：`grade_documents_node`（相关性评估节点）。它直接决定后面的条件边是**直接输出结果**，还是**跳去重写 Query 再搜一次**。

**7. `retry_count: int`（重写重试计数器）**

- **含义**：记录子图内部已经重写 `query` 重新搜索了几次（初始为 `0`）。
- **作用**：**防死循环保护**。如果历史故障库里本来就没有这类新 Bug，不管怎么改写搜索词，`relevance_score` 永远是 `"no"`。我们限制 `retry_count` 最多到 `1` 或 `2`：一旦重试过 1 次还是 `"no"`，就不再重搜了，直接认命返回空结果 `historical_cases = []` 退出子图，让后面的探针自己去查代码。

**最终写回给主图的 2 个同名字段（LangGraph 子图通信机制）**

**8. `historical_cases: list[dict]`**

- **含义**：最终通过了 Grader 裁判检验（`relevance_score == "yes"`）的有效历史故障案例。如果两轮都没搜到相关的，这里就是空列表 `[]`。

**9. `evidence_ledger: Annotated[list[Evidence], operator.add]`**

- **含义**：如果真的在历史库里搜到了高度吻合的历史复发故障（比如 `INC-089`），子图在退出前，会顺手把这条历史故障包装成一个正式的 `Evidence(id="ev_hist:01", source="runbook", ...)` 对象，通过 `operator.add` 自动追加进主图的真实证据总账本里，供最后写报告时合法引用 `[ev_hist:01]`。*]



#####  **子图 2 状态——多源探针取证子图：`ProbeWorkerState(TypedDict)`**

```py
class ProbeWorkerState(TypedDict):
    hypothesis: Hypothesis                                   # 主图派发给当前子图要验证的那 1 个假设
    worker_messages: Annotated[list[AnyMessage], add_messages] # 子图自己调工具（ls/grep/read_file/sql）的对话过程，不污染主图！
    remaining_chars: int                                     # 当前探针的字符预算上限
    # 子图跑完后，自动通过 operator.add 合并回主图 IncidentMainState 的字段：
    evidence_ledger: Annotated[list[Evidence], operator.add]
    gaps: Annotated[list[str], operator.add]
```

**1. `hypothesis: Hypothesis`（当前特工领到的唯一任务）**

- **含义**：主图派发给当前这个子图去求证的**那 1 条具体假设**（注意不是列表，而是单个 `Hypothesis` 对象）。

- **例子**：

  ```py
  Hypothesis(
      id="h1",
      title="检查 app/services/order_service.py 第 64 行计算折扣时是否未处理 None 值",
      probe_type="code",          # 决定了这个子图挂载哪套工具（code / log / db）
      assessment="plausible"      # 刚进来时是“待定”，跑完出去时会被改成 "supported" 或 "rejected"
  )
  ```

- **作用**：

  1. 作为当前子图大模型的任务目标（告诉它：“你这趟只负责查证这一件事，查清就停”）；
  2. 根据里面的 `probe_type`（`"log"` / `"code"` / `"db"`），子图只给大模型绑定对应的工具（比如 `"db"` 探针只绑 SQL 工具，不绑改代码工具，防止乱调）。

**2. `worker_messages: Annotated[list[AnyMessage], add_messages]`（子图私有的草稿纸）**

- **含义**：当前探针子图在连续调用工具（比如先调 `ls` 看目录 → 再调 `grep` 搜函数 → 再调 `read_file` 读 50 行代码）过程中产生的对话历史。
- **为什么故意叫 `worker_messages` 而不叫 `messages`？（面试核心亮点）**
  - 主图里有个字段叫 `messages`。
  - 如果子图这个字段也叫 `messages`，那么当 3 个探针子图跑完退出时，它们各自翻找目录、读错文件的几十条中间过程消息，会**全部灌回主图的 `messages` 里**，瞬间把主图上下文撑爆（几万 Token 全是翻目录的废话）！
  - **故意改名叫 `worker_messages` 后**：因为主图 `IncidentMainState` 里没有叫 `worker_messages` 的字段，所以子图在内部随便调十几次工具，退出子图的那一刻，**`worker_messages` 会被直接销毁丢弃**，主图完全不会被污染,这叫 **“子图级上下文隔离（Subgraph Context Isolation）”**。

**3. `remaining_chars: int`（复刻自 `open-swe` 的字符容量预算）**

- **含义**：当前这个探针子图最多允许收集多少个字符的原始证据（比如初始设为 `15000` 字符）。
- **谁修改它**：工具执行节点（复刻你昨天看的 `open-swe/openswe/incidents/evidence_tools.py` 第 51 行）。
- **作用**：每当大模型调了一次 `read_file` 读出 3000 字代码，就执行 `remaining_chars -= 3000`。一旦扣到 `0`，如果大模型还想继续读别的文件，直接停止工具调用并在 `gaps` 里记一笔，防止单个探针陷入死循环把内存撑爆。

**最终合并回主图的 2 个同名字段**

**4. `evidence_ledger: Annotated[list[Evidence], operator.add]`**

- **含义**：当前探针在这趟调查中，从工具返回值里提炼出来的**高价值真实证据列表**。
- **例子**： 虽然 `worker_messages` 里的几十条翻目录废话被扔掉了，但真正查到的那段关键代码被打包成了 `[Evidence(id="ev_code:02", summary="order_service.py:64 未判断 coupon_type", raw_snippet="...")]`，通过 `operator.add` 干干净净地交回主图的总账本。

**5. `gaps: Annotated[list[str], operator.add]`**

- **含义**：如果当前探针在查证过程中遇到了工具报错（比如连不上 MySQL）、或者没找着目标文件、或者超出了 `remaining_chars` 预算，就把原因写成字符串（如 `"MySQL 查询超时，未能核实优惠券表数据"`）追加回主图的 `gaps` 列表。

##### **子图 3 状态——代码补丁与单测自愈子图：`PatchSelfHealSubState(TypedDict)`**

```py
class PatchSelfHealSubState(TypedDict):
    final_report: IncidentReport      # 从主图拿过来的根因报告
    evidence_ledger: list[Evidence]   # 从主图拿过来的报错代码证据
    worktree_path: str                # 子图内部：临时 Git Worktree 沙箱路径
    target_file: str                  # 子图内部：修改的目标文件路径
    test_command: str                 # 子图内部：执行的 pytest 命令
    test_error_log: str | None        # 子图内部：单测失败时的报错堆栈（用于自愈重试）
    retry_count: int                  # 子图内部：单测自愈重试次数（最多 3 次）
    # 最终交回给主图的字段：
    patch_diff: str | None
    test_passed: bool
```

**第 1 组：从主图带进来的只读输入字段**

**1. `final_report: IncidentReport`**

- **含义**：前面已经通过防幻觉验钞的正式排障报告。
- **作用**：告诉当前改代码的大模型：“根因（`cause`）到底是什么、建议怎么改（`next_steps`）”，让它严格照着报告改，不要自己瞎发挥。

**2. `evidence_ledger: list[Evidence]`**

- **含义**：前面探针收集到的真实证据列表（里面包含了报错文件的路径和报错行附近原本的代码片段 `raw_snippet`）。
- **作用**：大模型直接从这里看到原本有 Bug 的那几行代码长什么样，不需要再重新去搜一遍目录。

**第 2 组：子图内部专用的沙箱与自愈循环字段**

**3. `worktree_path: str`（隔离沙箱目录路径）**

- **含义**：用 `git worktree add` 命令临时创建出来的**隔离代码副本文件夹路径**（比如 `"C:/temp/sandbox_inc_001"`）。
- **为什么要这个字段（面试加分点）**： 在人工还没点击“批准”之前，Agent **绝对不能直接去改你原本正在运行的项目主目录代码**（万一改崩了或者单测没过，原项目就毁了）。 所以子图先建一个临时副本目录 `worktree_path`，大模型所有的 `edit_file` 和 `pytest` 全在这个临时文件夹里折腾。

**4. `target_file: str`（被修改的目标文件相对路径）**

- **含义**：大模型决定要修改的那个源码文件路径。
- **例子**：`"app/services/order_service.py"`。

**5. `test_command: str`（要执行的测试命令）**

- **含义**：用来验证这次代码修改对不对的终端命令。
- **例子**：`"pytest tests/test_order_service.py -v"`（如果某个文件没有写专门的单元测试，兜底命令就是 `"python -m py_compile app/services/order_service.py"`，至少保证改完的代码没有语法和导包错误）。

**6. `test_error_log: str | None`（自愈的核心：单测失败的报错信息）**

- **含义**：
  - 第 1 次改代码时，它是 `None`；
  - 如果第 1 次改完代码跑 `pytest` **失败了**（比如大模型少写了一个括号，或者漏改了一个变量名），测试节点就会把 `pytest` 吐出来的**最新报错堆栈**存进 `test_error_log` 里，然后跳回改代码节点。
- **作用**：第 2 次进入改代码节点时，大模型看到 `test_error_log` 里写着“你刚才改的代码在第 66 行报了 `NameError: name 'round_price' is not defined`”，它就能立刻针对性地把自己刚写错的地方改对！这就是 **“执行反馈自愈（Self-Healing with Execution Feedback）”**。

**7. `retry_count: int`（自愈重试计数器）**

- **含义**：记录大模型“改代码 → 跑测试失败 → 重改”已经循环了多少次（初始为 `0`）。
- **作用**：最多允许重试 **3 次**。如果改了 3 次单测还是不过，说明这个问题超出了模型自动修复的能力，停止重试直接退出子图（此时 `test_passed = False`），在前端展示给人类工程师并标红提示“自动单测未通过，请人工介入审查”。

**最终写回给主图的 2 个同名字段**

当测试通过（或重试满 3 次）退出子图时，上面第 3~7 号内部字段自动销毁，只把下面这两个字段交回给主图（紧接着主图就会触发 `interrupt` 让你在前端审批）：

**8. `patch_diff: str | None`**

- **含义**：在沙箱里跑一句 

  `git diff`

  生成的标准红绿代码修改对比文本：

  ```diff
  --- a/app/services/order_service.py
  
  +++ b/app/services/order_service.py
  
  @@ -63,2 +63,5 @@
  
  -    final_price = total_price - coupon.discount_amount
  
  +    if coupon.coupon_type == "RATE":
  
  +        final_price = round(total_price * coupon.discount_rate, 2) 
  ```

- **作用**：交回主图后，推送给前端 Vue3 页面展示代码对比；如果人工点击【批准】，最后一个节点就拿这个 `patch_diff` 直接应用（`git apply`）到你的主项目里。

**9. `test_passed: bool`**

- **含义**：告诉主图和前端页面：这份 `patch_diff` 补丁在沙箱里**到底有没有通过自动化测试**（`True` 显示绿色“测试已通过”徽章，`False` 显示黄色警告徽章）。

****

## 三个术语

| 层级名称                         | 使用技术                           | 所处位置（在哪一层用）                                       | 核心职责                                                     |
| -------------------------------- | ---------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| **1. `Model`** *(持久化模型)*    | `SQLAlchemy (Base)`                | **最底层：数据库仓储层（`repositories/` / `dao/`）**         | **只跟 MySQL 表打交道**。定义表名、字段类型、索引。出了 `repository` 层之后，外面的业务代码不直接操作 `Model`。 |
| **2. `Entity`** *(领域核心实体)* | `@dataclass` *(Python 原生标准库)* | **最核心：业务逻辑层与 LangGraph 状态机内部（`domain/` / `graphs/`）** | **纯净的内部业务对象**。不耦合数据库（SQLAlchemy），也不背负沉重的外部校验逻辑。在 Service 和 LangGraph 节点之间高速流转，里面可以写纯粹的领域方法（比如计算证据剩余配额、判断假设是否全部排除）。 |
| **3. `Schema`** *(外部边界契约)* | `Pydantic (BaseModel)`             | **最外层边界：FastAPI 接口层（`api/`） & 大模型交互边界（`tools/`）** | **守门员（负责一切不可信外部输入的校验与序列化）**： ① 校验前端 HTTP 请求参数与格式化响应（DTO/VO）； ② 校验大模型（LLM）吐出来的 JSON 参数（如 `ReportDraft` 的 `args_schema`）。 |


---

## Schema

参考 `open-swe` 的精简两层架构（**`Schema (Pydantic BaseModel)` $\leftrightarrow$ `Model (SQLAlchemy Base)`**，砍掉冗余的 `@dataclass` 转换层）：
* **`Schema`（继承自 `BaseModel`）**：既作为装进 LangGraph `State` 里流转的**内存业务对象**，也作为**大模型结构化输出契约（`args_schema`）**与 **FastAPI 前后端接口 DTO/VO**；
* **`Model`（继承自 SQLAlchemy `Base`）**：只负责在最底层映射到 MySQL 的 6 张表进行持久化落盘。

---

### 一、 核心排障与大模型契约 Schema

`app/schemas/incident_schemas.py`

#### 1. 真实证据记录模型：`Evidence(BaseModel)`
```python
class Evidence(BaseModel):
    id: str = Field(..., description="唯一证据编号，如 ev_log:a1b2, ev_code:c3d4, ev_db:e5f6")
    source: Literal["es_log", "codebase", "mysql_db", "git_history", "runbook"]
    tool_name: str = Field(..., description="产生该证据的工具名称，如 read_file, search_error_logs")
    query_args: str = Field(..., description="调用工具时的参数 JSON 字符串")
    summary: str = Field(..., max_length=500, description="证据简述")
    raw_snippet: str = Field(..., max_length=4000, description="经 redact() 脱敏和截断后的内容片段")
    retrieved_at: str = Field(..., description="证据生成时间戳")
```
* **各字段用法详解**：
  * **`id: str`**：由 Python 工具包装器根据查询内容哈希自动生成的唯一编号（大模型不可伪造），存入 `State["evidence_ledger"]` 白名单中。
  * **`source` & `tool_name`**：标识这条证据是从哪来的（比如是查 ES 日志拿到的，还是读本地代码文件拿到的），方便前端用不同颜色的卡片展示。
  * **`query_args: str`**：记录当时传给工具的参数（例如 `{"file_path": "order_service.py", "offset": 45}`），用于溯源复现。
  * **`raw_snippet: str`**：工具返回的真实原文片段（经过正则脱敏密码/密钥，且限制单条最长 4000 字符防止撑爆上下文）。

---

#### 2. 单句带证据断言：`Claim(BaseModel)`
```python
class Claim(BaseModel):
    text: str = Field(..., max_length=1200, description="单句排障结论陈述")
    evidence_ids: list[str] = Field(default_factory=list, max_length=10, description="支撑该结论的真实证据ID列表")
```
* **各字段用法详解**：
  * **`text: str`**：大模型写出的单句结论文本（比如 `"订单服务在第 64 行未判断折扣券类型导致空指针"`）。限制最大 1200 字符，防止大模型写长篇大论。
  * **`evidence_ids: list[str]`**：**防幻觉最核心字段**。要求大模型必须把前面工具返回给它的证据编号（如 `["ev_log:01", "ev_code:02"]`）填进这个列表里。在 `verify_report_node` 节点中，Python 代码会遍历这个列表，拿里面的每个 ID 去跟 `evidence_ledger` 账本核对；只要有一个 ID 是假的或者列表为空，这句 `text` 就会被系统直接判定为“幻觉”并拦截剔除。

---

#### 3. 排障假设模型：`Hypothesis(BaseModel)` & `HypothesisPlanList(BaseModel)`
```python
class Hypothesis(BaseModel):
    id: str = Field(..., description="假设编号，如 h1, h2, h3")
    title: str = Field(..., max_length=500, description="具体要查证的假设内容")
    probe_type: Literal["log", "code", "db"] = Field(..., description="需要派发的探针类型")
    assessment: Literal["supported", "plausible", "rejected"] = Field(default="plausible")
    evidence_ids: list[str] = Field(default_factory=list, description="证实或排除该假设的证据ID")

class HypothesisPlanList(BaseModel):
    hypotheses: list[Hypothesis] = Field(..., max_length=3, description="每次最多生成3个并行假设")
```
* **各字段用法详解**：
  * **`id: str`**：假设的简短唯一标识（`"h1"`、`"h2"`），方便后续汇总节点更新某个假设的状态。
  * **`title: str`**：派发给子图特工（`ProbeWorkerState`）的具体查证指令（例如 `"去 MySQL 查询用户 10086 的优惠券记录是否存在 NULL 脏数据"`）。
  * **`probe_type: Literal["log", "code", "db"]`**：**决定子图工具挂载的路由字段**。
    * 填 `"log"` $\rightarrow$ 启动的探针子图只挂载 ES 日志搜索工具；
    * 填 `"code"` $\rightarrow$ 启动的探针子图只挂载本地目录 `ls`/`grep`/`read_file`/`git_blame` 工具；
    * 填 `"db"` $\rightarrow$ 启动的探针子图只挂载 MySQL 只读 NL2SQL 工具。
  * **`assessment: Literal["supported", "plausible", "rejected"]`**：假设的生命周期状态：
    * `"plausible"`（待查证）：刚由 `hypothesis_planner_node` 提出来时的默认状态；
    * `"supported"`（已证实）：探针查到了确凿证据证明 Bug 就是这个原因；
    * `"rejected"`（已排除）：探针查完发现这块没问题（此时触发 `context_compressor_node` 将该分支读取的长代码折叠丢弃）。
  * **`evidence_ids: list[str]`**：证明该假设成立或被排除的证据编号列表。

---

#### 4. 大模型交卷答题卡（待审草稿）：`ReportDraft(BaseModel)`
```python
class ReportDraft(BaseModel):
    summary: list[Claim] = Field(default_factory=list, max_length=4)
    problem: list[Claim] = Field(default_factory=list, max_length=4)
    previous_occurrence: list[Claim] = Field(default_factory=list, max_length=3)
    impact: list[Claim] = Field(default_factory=list, max_length=4)
    cause: list[Claim] = Field(default_factory=list, max_length=4)
    next_steps: list[Claim] = Field(default_factory=list, max_length=3)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=6)
    gaps: list[str] = Field(default_factory=list, max_length=10)
    questions: list[str] = Field(default_factory=list, max_length=5)
    need_code_patch: bool = Field(default=False)
```
* **各字段用法详解（对应 `open-swe` 的 `ReportDraft`）**：
  * **`summary: list[Claim]`**：一句话总结本次排障的最终定性结论（每句话都必须带 `evidence_ids`）。在前端会话列表和报告顶部作为醒目摘要展示。
  * **`problem: list[Claim]`**：明确描述触发报错的是哪个接口、哪个服务、报了什么异常类型（对应排障五步法的第 1 步）。
  * **`previous_occurrence: list[Claim]`**：历史复发情况说明（对应排障五步法的第 2 步）。如果 `CRAGSubState` 查到了历史相似案例（如 `INC-089`），在此填写并绑定 `[ev_hist:01]`；如果没查到，则由 `finalize_report` 自动兜底填入 `"已检索历史故障库，未发现同类复发记录"`。
  * **`impact: list[Claim]`**：故障影响面评估（比如影响所有使用折扣券下单的用户，还是仅影响单个用户）。
  * **`cause: list[Claim]`**：**最核心字段——已证实的故障根因**（比如明确指出哪个文件的第几行代码逻辑错误，必须绑定对应的代码和日志证据 ID）。
  * **`next_steps: list[Claim]`**：具体的修复步骤或运维缓解建议（最多 3 条，按优先级排序）。
  * **`hypotheses: list[Hypothesis]`**：本轮排查过程中提出过的所有假设及其最终验证结果（让人类工程师看到 Agent 不仅查对了方向，还排除了哪些干扰项）。
  * **`gaps: list[str]`**：**排查盲区列表（注意是纯 `str`，不需要 `evidence_ids`）**。用来记录“哪个工具调用失败了”、“哪块日志没权限看”或者由系统自动追加“某条结论因缺少证据被剔除”。
  * **`questions: list[str]`**：**多轮对话反问列表（纯 `str`，不需要 `evidence_ids`）**。当线索不足无法锁定根因时，大模型把需要向人类追问的问题填在这里（如 `"请提供报错用户的 user_id"`），触发多轮对话。
  * **`need_code_patch: bool`**：**主图条件边路由开关**！
    * 如果根因是本地业务代码 Bug，大模型将其设为 `True` $\rightarrow$ 验钞通过后主图自动进入 `PatchSelfHealSubState` 子图去改代码、跑单测；
    * 如果根因只是第三方接口限流、或者线索不足待用户回复，设为 `False` $\rightarrow$ 跳过改代码子图，直接结束本轮并向前端输出报告。

---

#### 5. 防幻觉验钞通过后的正式报告：`IncidentReport(BaseModel)`
```python
class IncidentReport(BaseModel):
    incident_id: str
    summary: str
    problem: str
    previous_occurrence: str
    impact: str
    cause: str
    next_steps: list[str]
    outcome: Literal["inconclusive", "findings"]
    need_code_patch: bool
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    gaps: list[str]
    questions: list[str]
    digest: str                     # SHA-256 查重指纹
    created_at: str
```
* **各字段用法详解（由 Python `finalize_report()` 转换生成，直接装入 `State["final_report"]` 及写入数据库 JSON 列）**：
  * **`summary` / `problem` / `cause` 等字符串字段**：由 `finalize_report()` 过滤掉无证据的 `Claim` 后，自动将合格句子拼接成带 `[ev_log:01, ev_code:02]` 角标的正式文本。
  * **`outcome: Literal["inconclusive", "findings"]`**：如果验钞后有效结论为空，系统自动设为 `"inconclusive"`（未得出确凿结论）；否则设为 `"findings"`。
  * **`evidence: list[Evidence]`**：直接附上后台 `evidence_ledger` 里的真实证据详情列表，供前端点击角标展开查看代码/日志原文。
  * **`digest: str`**：调用 `fingerprint(digest_fields(report))` 算出的 64 位 SHA-256 指纹，用于幂等防重。

---

#### 6. CRAG 检索裁判打分模型：`RetrievalGrade(BaseModel)`
```python
class RetrievalGrade(BaseModel):
    relevance_score: Literal["yes", "no"] = Field(..., description="检索到的历史故障文档是否与当前故障相关")
    reason: str = Field(..., max_length=300, description="判定相关或不相关的简短理由")
```
* **用法**：用在子图 1（`CRAGSubState`）的 `Grader` 节点中，通过 `llm.with_structured_output(RetrievalGrade)` 强制大模型裁判只输出 `"yes"` 或 `"no"`。

---

### 二、 FastAPI 前后端交互接口 Schema

`app/schemas/api_schemas.py`

1. **`IncidentCreateRequest(BaseModel)`（发起排障/多轮追问请求 DTO）**：
   * `incident_id: str | None = None`：首次提问传 `None`（后端自动生成新会话 ID）；多轮追问时传入已有的 `incident_id`（即 `thread_id`）继续对话。
   * `target_project_id: str`：指定要排查的目标项目 ID（对应 `workspaces` 表主键，告诉 Agent 去读哪个本地文件夹和数据库）。
   * `user_query: str`：用户输入的故障描述或补充线索。
2. **`PatchApprovalRequest(BaseModel)`（人工审批补丁请求 DTO，用于唤醒 `interrupt`）**：
   * `incident_id: str`：要唤醒的排障会话 ID（即 Redis 中的 `thread_id`）。
   * `action: Literal["approved", "rejected"]`：人工点击了【批准】还是【驳回】。
   * `feedback: str | None = None`：若点击驳回，人工输入的修改指导意见（如 `"请把折扣计算抽成独立函数"`）。

---

## Model

`Model` 仅存在于最底层的 `app/repositories/`（数据库仓储层）中，负责将上面的 Pydantic `Schema` 对象（直接通过 `.model_dump()`）落盘保存到 MySQL 的 **6 张表** 中：

1. **`IncidentModel`（对应表 `incidents` —— 排障会话主表）**：
   * `id` (PK, 会话 ID，直接兼任 LangGraph `thread_id`), `target_project_id` (FK, 被排查的目标项目 ID), `title`, `trigger_source`, `status`, `latest_report` (JSON, 存 `IncidentReport.model_dump()`), `report_digest`, `created_at`, `updated_at`
2. **`IncidentMessageModel`（对应表 `incident_messages` —— 多轮对话消息表）**：
   * `id` (PK), `incident_id` (FK), `turn_index`, `role`, `msg_type`, `content`, `metadata_json` (JSON), `created_at`
3. **`EvidenceModel`（对应表 `incident_evidences` —— 真实证据账本表）**：
   * `evidence_id` (PK), `incident_id` (FK), `source`, `tool_name`, `query_args`, `summary`, `raw_snippet`, `created_at`
4. **`PatchRecordModel`（对应表 `patch_records` —— 补丁与沙箱自愈审批表）**：
   * `id` (PK), `incident_id` (FK), `target_file`, `patch_diff`, `test_command`, `test_passed`, `test_output`, `self_heal_retries`, `approval_status`, `human_feedback`, `created_at`
5. **`WorkspaceModel`（对应表 `workspaces` —— 被监控靶场项目配置表）**：
   * `id` (PK, 即 `target_project_id`), `name`, `local_repo_path`, `es_log_index`, `mysql_readonly_uri`, `default_test_cmd`, `repo_map_cache`
6. **`KnowledgeRunbookModel`（对应表 `knowledge_runbooks` —— 历史故障复盘知识库主表）**：
   * `case_id` (PK, 与 ES 及 Qdrant 主键一致), `title`, `error_signature`, `api_or_module`, `symptom_desc`, `root_cause`, `resolution`, `source_incident_id`, `created_at`
