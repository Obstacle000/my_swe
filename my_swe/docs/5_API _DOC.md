# 第 5 篇：FastAPI 前后端 REST 与 SSE 流式接口设计文档（API Specification）

> **文档说明**：本文档定义 Python `FastAPI` 后端向 `Vue3` 前端暴露的全部接口契约，包含 **4 大模块共 12 个核心接口**，重点定义了 **LangGraph 节点进度与证据卡片的 SSE（Server-Sent Events）实时流式推送协议** 以及 **`interrupt()` 人工审批唤醒接口**。

---

## 一、 接口总览与通信机制说明

| 模块分类 | 请求方法 | 接口路径 | 核心作用 |
| :--- | :--- | :--- | :--- |
| **1. 核心排障与流式对话** | `POST` | `/api/v1/incidents/chat-stream` | **[核心]** 发起新排障会话 / 多轮追问（SSE 流式返回节点状态、证据与报告） |
| | `POST` | `/api/v1/incidents/{incident_id}/approval` | **[核心]** 人工点击【批准/驳回】代码补丁，唤醒 LangGraph `interrupt` 断点 |
| | `GET` | `/api/v1/incidents` | 获取左侧历史排障会话列表（支持按项目、状态过滤） |
| | `GET` | `/api/v1/incidents/{incident_id}` | 获取某次会话的完整详情（多轮聊天消息 + 证据池 + 报告 + 补丁） |
| | `GET` | `/api/v1/incidents/{incident_id}/evidences/{ev_id}` | 点击报告中的 `[ev_code:xxxx]` 角标时，查看该条证据的代码/日志原文 |
| **2. 靶场故障注入与告警** | `POST` | `/api/v1/range/trigger-fault` | **[演示利器]** 一键触发靶场预设故障（自动往 ES 写入真实报错日志并造好测试数据） |
| | `POST` | `/api/v1/webhooks/alert` | 接收外部监控告警 Webhook，自动创建工单并触发后台排障 |
| **3. 历史知识库 (CRAG)** | `GET` | `/api/v1/knowledge/runbooks` | 分页查看已沉淀的历史故障复盘库 |
| | `POST` | `/api/v1/knowledge/runbooks` | 手动新增历史故障案例（自动三端同步写入 MySQL + ES + Qdrant） |
| | `POST` | `/api/v1/knowledge/search-debug` | CRAG 混合检索测试台接口（同时展示 ES 排名、Qdrant 排名与 RRF 最终得分） |
| **4. 项目配置与统计大屏** | `GET/POST` | `/api/v1/workspaces` | 查看或新增被监控的目标项目配置（本地代码路径、ES索引、MySQL连接） |
| | `GET` | `/api/v1/analytics/overview` | 首页 ECharts 监控大屏指标（排障总数、自动定位率、平均耗时 MTTR） |

---

## 二、 模块 1：核心排障会话与 SSE 流式接口详解

### 1. 发起排障 / 多轮追问流式接口（最核心接口）
* **路径**：`POST /api/v1/incidents/chat-stream`
* **响应类型**：`text/event-stream`（SSE 服务端实时推送流）
* **作用**：
  * 当 `incident_id` 为 `null` 时：创建新会话（生成新 `incident_id` 作为 LangGraph 的 `thread_id`），启动主图开始第 1 轮排障；
  * 当传入已有的 `incident_id` 时：作为**多轮对话**，在同一个 `thread_id` 上补充线索或继续追问。

#### 请求体（Request Body - `IncidentCreateRequest`）
```json
{
  "incident_id": null,
  "target_project_id": "mall_demo",
  "user_query": "刚刚用户 user_id=10086 反馈在购物车点击提交订单时报错 500，帮我排查原因并修复。"
}
```

#### SSE 流式推送事件协议（前端根据 `event` 类型实时更新界面）
因为一次完整排障（查历史库 $\rightarrow$ 并行探针查 ES/代码/MySQL $\rightarrow$ 写报告验钞 $\rightarrow$ 沙箱改代码跑 `pytest`）大约需要 15~25 秒，后端通过 SSE 依次向前端推送以下 **6 种事件**：

1. **`event: session_init`（会话初始化成功）**
   ```json
   {
     "incident_id": "inc_20261104_001",
     "title": "用户10086订单结算500异常",
     "status": "investigating"
   }
   ```
2. **`event: node_transition`（用于点亮前端左侧的 LangGraph 流程图节点！）**
   ```json
   {
     "node_name": "hypothesis_planner_node",
     "display_name": "正在生成排障假设并派发并行探针...",
     "timestamp": "2026-11-04T10:50:03"
   }
   ```
3. **`event: hypotheses_updated`（推送当前提出的假设列表及状态）**
   ```json
   {
     "hypotheses": [
       {"id": "h1", "title": "检查 order_service.py:64 计算折扣时是否未处理 None", "probe_type": "code", "assessment": "plausible"},
       {"id": "h2", "title": "查询 MySQL 优惠券表用户 10086 的折扣字段是否为 NULL", "probe_type": "db", "assessment": "plausible"}
     ]
   }
   ```
4. **`event: evidence_collected`（每当探针调工具拿到一条新证据，实时弹出证据小卡片）**
   ```json
   {
     "evidence": {
       "id": "ev_code:9b2d01",
       "source": "codebase",
       "tool_name": "read_file_window",
       "summary": "读取 app/services/order_service.py 第 50~70 行",
       "raw_snippet": "62 | coupon = await coupon_repo.get_by_id(...)\n64 | final_price = total_price - coupon.discount_amount"
     }
   }
   ```
5. **`event: report_ready`（经过 `verify_report_node` 防幻觉验钞通过后的正式报告）**
   ```json
   {
     "report": {
       "incident_id": "inc_20261104_001",
       "outcome": "findings",
       "summary": "订单服务结算折扣券时未区分优惠券类型，读取为 None 的 discount_amount 导致 TypeError [ev_log:8f3a1c, ev_code:9b2d01, ev_db:4c1e09]",
       "problem": "POST /api/orders/checkout 接口抛出 TypeError [ev_log:8f3a1c]",
       "previous_occurrence": "已检索历史故障库，未发现同类复发记录",
       "cause": "order_service.py:64 直接使用 total_price - coupon.discount_amount，未处理 coupon_type='RATE' 时该字段为 NULL 的情况 [ev_code:9b2d01, ev_db:4c1e09]",
       "next_steps": ["在 order_service.py:64 增加 RATE 折扣券计算分支 [ev_code:9b2d01]"],
       "gaps": [],
       "questions": []
     }
   }
   ```
6. **`event: waiting_approval`（当图走到 `human_approval_node` 触发 `interrupt()` 挂起时推送）**
   ```json
   {
     "incident_id": "inc_20261104_001",
     "status": "waiting_approval",
     "target_file": "app/services/order_service.py",
     "patch_diff": "--- a/app/services/order_service.py\n+++ b/app/services/order_service.py\n@@ -64,1 +64,4 @@\n-    final_price = total_price - coupon.discount_amount\n+    if coupon.coupon_type == 'RATE': ...",
     "test_passed": true,
     "self_heal_retries": 1,
     "test_output": "1 passed in 0.42s"
   }
   ```

---

### 2. 人工审批补丁唤醒接口（联动 LangGraph `interrupt` 与 `Command(resume=...)`）
* **路径**：`POST /api/v1/incidents/{incident_id}/approval`
* **作用**：用户在前端看到代码 Diff 对比后，点击 **【批准应用补丁】** 或 **【驳回并重改】** 时调用。

#### 请求体（Request Body - `PatchApprovalRequest`）
```json
{
  "action": "approved",
  "feedback": null
}
```
*(若点击驳回，则传 `{"action": "rejected", "feedback": "请使用 Decimal 替代 float 计算折扣，防止精度丢失"}`)*

#### 后端 FastAPI 核心实现代码逻辑（直接对接 LangGraph）：
```python
from langgraph.types import Command

@router.post("/incidents/{incident_id}/approval")
async def approve_or_reject_patch(incident_id: str, req: PatchApprovalRequest):
    # 使用与会话完全相同的 incident_id 作为 thread_id，从 Redis 唤醒挂起的图
    config = {"configurable": {"thread_id": incident_id}}
    
    result_state = await incident_main_graph.ainvoke(
        Command(resume={"action": req.action, "feedback": req.feedback}),
        config=config,
    )
    return {
        "incident_id": incident_id,
        "status": "resolved" if req.action == "approved" else "waiting_approval",
        "patch_diff": result_state.get("patch_diff"),
        "test_passed": result_state.get("test_passed"),
        "evolved_case_id": result_state.get("evolved_case_id"), # 自动沉淀进 Qdrant/ES 的知识库编号
    }
```

---

### 3. 获取单个排障会话完整详情接口
* **路径**：`GET /api/v1/incidents/{incident_id}`
* **作用**：用户在页面左侧点击切换历史会话时，一次性拉取该会话的所有多轮聊天记录、已收集到的证据池、报告与补丁状态。
* **响应示例**：
  ```json
  {
    "incident": {
      "id": "inc_20261104_001",
      "target_project_id": "mall_demo",
      "title": "用户10086订单结算500异常",
      "status": "waiting_approval",
      "latest_report": { "...": "..." },
      "created_at": "2026-11-04T10:50:00"
    },
    "messages": [
      {"id": 1, "turn_index": 1, "role": "user", "msg_type": "text", "content": "刚刚用户 user_id=10086..."},
      {"id": 2, "turn_index": 1, "role": "assistant", "msg_type": "report_card", "content": "..."}
    ],
    "evidences": [
      {"evidence_id": "ev_log:8f3a1c", "source": "es_log", "summary": "..."},
      {"evidence_id": "ev_code:9b2d01", "source": "codebase", "summary": "..."}
    ],
    "latest_patch": {
      "id": "patch_001",
      "target_file": "app/services/order_service.py",
      "patch_diff": "...",
      "test_passed": true,
      "approval_status": "pending"
    }
  }
  ```

---

## 三、 模块 2：故障靶场一键注入接口（面试演示与毕设答辩神器）

在面试或毕设答辩现场，为了让你能一键演示《01_USE_CASES.md》里的 3 个剧本，专门设计此接口：

* **路径**：`POST /api/v1/range/trigger-fault`
* **请求体**：
  ```json
  {
    "scenario_code": "case_1_coupon_null_500"
  }
  ```
  *支持枚举值*：
  * `"case_1_coupon_null_500"`（剧本一：往 ES 写入用户 10086 的 `TypeError` 报错日志，并确保 MySQL 优惠券表里存在 `coupon_type='RATE', discount_amount=NULL` 的记录）
  * `"case_2_vip_discount_bug"`（剧本二：黄金会员折扣失效，无 ERROR 日志）
  * `"case_3_sms_timeout_recurrence"`（剧本三：短信接口超时，确保 Qdrant/ES 知识库中已预置上个月的 `INC-2026-089` 历史案例）
* **响应体**：
  ```json
  {
    "injected": true,
    "suggested_query": "刚刚用户 user_id=10086 反馈在购物车点击提交订单时报错 500，帮我排查原因并修复。",
    "message": "已成功向 ES 注入报错堆栈并初始化 MySQL 靶场数据，可直接点击发送开始排障演示。"
  }
  ```

---

## 四、 模块 3：CRAG 历史故障库管理与混合检索调试接口

### 1. 混合检索可视化调试接口
* **路径**：`POST /api/v1/knowledge/search-debug`
* **作用**：在前端“知识库检索测试台”页面输入一句话，直观展示 **ES (BM25) 召回列表**、**Qdrant (向量) 召回列表**、**RRF 融合重排分数** 以及 **Grader 相关性裁判打分结果**（方便在答辩时直观展示你的混合检索算法效果）。
* **请求体**：
  ```json
  {
    "query": "短信接口 ConnectTimeoutError 超时",
    "top_k": 3
  }
  ```
* **响应体**：
  ```json
  {
    "es_hits": [
      {"rank": 1, "case_id": "INC-2026-089", "bm25_score": 14.82, "title": "促销高峰期短信网关超时限流"}
    ],
    "qdrant_hits": [
      {"rank": 1, "case_id": "INC-2026-089", "cosine_score": 0.891, "title": "促销高峰期短信网关超时限流"}
    ],
    "rrf_merged": [
      {"rank": 1, "case_id": "INC-2026-089", "rrf_score": 0.03278, "title": "促销高峰期短信网关超时限流"}
    ],
    "grader_verdict": {
      "relevance_score": "yes",
      "reason": "候选案例 INC-2026-089 与查询同属 /api/notify/sms 接口 ConnectTimeoutError 故障"
    }
  }
  ```
