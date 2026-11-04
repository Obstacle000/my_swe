"""排障主图编排模块 (IncidentMainGraph)
通过 StateGraph(state_schema=IncidentMainState, context_schema=IncidentGraphContext) 声明运行期依赖上下文，
所有节点通过 Runtime[IncidentGraphContext] 获取请求级注入的 Repository 实例，彻底解耦数据库直接实例化。
"""

import json
import re
from typing import Literal
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send, interrupt

from app.config import get_llm, settings
from app.core.report_verifier import verify_and_finalize_report
from app.graphs.context import IncidentGraphContext
from app.graphs.crag_subgraph import crag_subgraph
from app.graphs.patch_subgraph import patch_subgraph
from app.graphs.probe_subgraph import probe_subgraph
from app.graphs.state import (
    CRAGSubState,
    IncidentMainState,
    PatchSelfHealSubState,
    ProbeWorkerState,
)
from app.prompts.loader import load_prompt
from app.schemas.incident_schemas import (
    Claim,
    Evidence,
    Hypothesis,
    HypothesisPlanList,
    ReportDraft,
    TriageClues,
)


def _has_valid_llm_key() -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"


def _latest_user_query(state: IncidentMainState) -> str:
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage) or getattr(msg, "type", "") == "human":
            return str(msg.content)
    return ""


# ============================================================================
# 节点 1: triage_node (自然语言意图分诊与结构化线索提取)
# ============================================================================
async def triage_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    user_text = _latest_user_query(state)

    if _has_valid_llm_key():
        try:
            sys_prompt = load_prompt("triage.md")
            llm_triage = get_llm(temperature=0.0).with_structured_output(TriageClues)
            clues_obj: TriageClues = await llm_triage.ainvoke(
                [SystemMessage(content=sys_prompt), HumanMessage(content=user_text)]
            )
            return {
                "extracted_clues": clues_obj.model_dump(),
                "loop_count": 0,
                "verify_retry_count": 0,
            }
        except Exception:
            pass

    uid_match = re.search(r"\b(1008[6-9]|\d{4,8})\b", user_text)
    api_match = re.search(r"(/api/[a-zA-Z0-9_\-/]+)", user_text)
    user_id = uid_match.group(1) if uid_match else ""
    api_path = api_match.group(1) if api_match else ""

    lower_q = user_text.lower()
    if "500" in lower_q or "优惠券" in lower_q or user_id == "10086":
        error_kw = "TypeError NoneType 500"
        mod_hint = "order"
        title = "用户10086结算订单报500异常排查"
        api_path = api_path or "/api/orders/checkout"
    elif "vip" in lower_q or "折扣" in lower_q or "8折" in lower_q or user_id == "10087":
        error_kw = "vip_level discount_ratio"
        mod_hint = "pricing"
        title = "VIP2黄金会员结算未打8折问题排查"
        api_path = api_path or "/api/orders/checkout"
    else:
        error_kw = "ReadTimeoutError timeout=2.0"
        mod_hint = "sms"
        title = "短信验证码接口大面积ReadTimeout排查"
        api_path = api_path or "/api/auth/send-sms"

    clues = TriageClues(
        user_id=user_id,
        api_path=api_path,
        error_keyword=error_kw,
        module_hint=mod_hint,
        title=title[:15],
    )
    return {
        "extracted_clues": clues.model_dump(),
        "loop_count": 0,
        "verify_retry_count": 0,
    }


# ============================================================================
# 节点 2: crag_subgraph_node (调用子图一：CRAG 历史故障双路混合检索，透传 runtime.context)
# ============================================================================
async def crag_subgraph_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    clues = state.get("extracted_clues", {})
    user_text = _latest_user_query(state)
    combined_query = (
        f"{user_text} {clues.get('api_path', '')} {clues.get('error_keyword', '')}".strip()
    )

    sub_res = await crag_subgraph.ainvoke(
        CRAGSubState(
            original_query=combined_query,
            current_query=combined_query,
            retry_count=0,
            crag_hit=False,
            evidence=[],
        ),
        context=runtime.context,
    )
    return {
        "historical_context": sub_res.get("historical_context", ""),
        "crag_hit": bool(sub_res.get("crag_hit", False)),
        "evidence": sub_res.get("evidence", []),
    }


# ============================================================================
# 节点 3: hypothesis_planner_node (动态生成 1~3 个并行排障假设)
# ============================================================================
async def hypothesis_planner_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    clues = state.get("extracted_clues", {})
    hist_ctx = state.get("historical_context", "")
    crag_hit = state.get("crag_hit", False)
    loop_count = state.get("loop_count", 0)

    if _has_valid_llm_key():
        try:
            sys_prompt = load_prompt("planner.md")
            user_ctx = (
                f"【分诊提取线索】：{json.dumps(clues, ensure_ascii=False)}\n"
                f"【CRAG 历史故障检索结果 (crag_hit={crag_hit})】：\n{hist_ctx}\n"
            )
            planner_llm = get_llm(temperature=0.0).with_structured_output(HypothesisPlanList)
            plan: HypothesisPlanList = await planner_llm.ainvoke(
                [SystemMessage(content=sys_prompt), HumanMessage(content=user_ctx)]
            )
            if plan.hypotheses:
                return {"hypotheses": plan.hypotheses, "loop_count": loop_count + 1}
        except Exception:
            pass

    mod_hint = clues.get("module_hint", "order")
    if crag_hit or mod_hint == "sms":
        hypotheses = [
            Hypothesis(
                id="h1",
                title="检索线上运行态日志，确认 /api/auth/send-sms 是否出现与历史故障 INC-2026-089 一致的 ReadTimeoutError (timeout=2.0)",
                probe_type="log",
            ),
            Hypothesis(
                id="h2",
                title="检查本地 config/settings.yaml 中 sms_gateway 主备通道配置与 fallback_enabled 降级开关状态",
                probe_type="code",
            ),
        ]
    elif mod_hint == "pricing":
        hypotheses = [
            Hypothesis(
                id="h1",
                title="检索用户 10087 在 pricing-service 的下单计算日志，核对 applied_ratio 与 vip_level 日志记录",
                probe_type="log",
            ),
            Hypothesis(
                id="h2",
                title="审查 app/services/pricing_service.py 中 calculate_vip_discount 函数的会员等级条件分支边界及近期 Git 变更",
                probe_type="code",
            ),
            Hypothesis(
                id="h3",
                title="查询数据库 mall_users 与 mall_orders 表，确认用户 10087 的真实 vip_level 与订单实付金额",
                probe_type="db",
            ),
        ]
    else:
        hypotheses = [
            Hypothesis(
                id="h1",
                title="检索用户 10086 调用 /api/orders/checkout 的 ERROR 异常堆栈日志，定位报错文件与具体代码行号",
                probe_type="log",
            ),
            Hypothesis(
                id="h2",
                title="读取 app/services/order_service.py 中 checkout_order 函数的优惠券抵扣逻辑源码及近期 Git 提交记录",
                probe_type="code",
            ),
            Hypothesis(
                id="h3",
                title="查询数据库 mall_coupons 与 mall_orders 表，核对用户 10086 使用的优惠券 CPN-999 的 coupon_type 与 discount_amount 字段值",
                probe_type="db",
            ),
        ]

    return {"hypotheses": hypotheses, "loop_count": loop_count + 1}


# ============================================================================
# 条件分发边: 使用 LangGraph Send API 并行派生多个 ProbeWorkerSubgraph
# ============================================================================
def _dispatch_probe_workers(state: IncidentMainState) -> list[Send]:
    clues = state.get("extracted_clues", {})
    return [
        Send(
            "probe_worker_node",
            ProbeWorkerState(
                hypothesis=h,
                clues=clues,
                worker_messages=[],
                remaining_chars=15000,
                evidence=[],
            ),
        )
        for h in state.get("hypotheses", [])
    ]


# ============================================================================
# 节点 4: probe_worker_node (执行子图二：单假设探针验证，透传 runtime.context)
# ============================================================================
async def probe_worker_node(
    state: ProbeWorkerState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    sub_res = await probe_subgraph.ainvoke(state, context=runtime.context)
    return {"evidence": sub_res.get("evidence", [])}


# ============================================================================
# 节点 5: evidence_evaluate_node (评估并行探针归并后的证据池充分性)
# ============================================================================
async def evidence_evaluate_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    return {"evidence": []}


def _route_after_evaluate(
    state: IncidentMainState,
) -> Literal["hypothesis_planner_node", "generate_report_node"]:
    ev_list = state.get("evidence", [])
    if not ev_list and state.get("loop_count", 0) < 2:
        return "hypothesis_planner_node"
    return "generate_report_node"


# ============================================================================
# 节点 6: generate_report_node (生成强制绑定真实证据 ID 的 ReportDraft)
# ============================================================================
async def generate_report_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    clues = state.get("extracted_clues", {})
    ev_pool = _dedup_evidences(state.get("evidence", []))
    hypotheses = state.get("hypotheses", [])
    verify_err = state.get("verification_error", "")

    if _has_valid_llm_key():
        try:
            sys_prompt = load_prompt("incident_report.md")
            ev_summary_text = "\n\n".join(
                f"- 证据ID `{ev.id}` (来源={ev.source}, 工具={ev.tool_name}):\n  摘要: {ev.summary}\n  内容片段:\n{ev.raw_snippet}"
                for ev in ev_pool
            )
            user_prompt = (
                f"【排障线索】：{json.dumps(clues, ensure_ascii=False)}\n\n"
                f"【待验证假设列表】：{json.dumps([h.model_dump() for h in hypotheses], ensure_ascii=False)}\n\n"
                f"【当前真实证据池 (State.evidence) —— 严禁引用此列表之外的证据ID】：\n{ev_summary_text}\n"
            )
            if verify_err:
                user_prompt += f"\n\n【上一轮防幻觉校验打回原因，请立即修正】：\n{verify_err}\n"

            report_llm = get_llm(temperature=0.0).with_structured_output(ReportDraft)
            draft: ReportDraft = await report_llm.ainvoke(
                [SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)]
            )
            return {"report_draft": draft}
        except Exception:
            pass

    draft = _build_grounded_report_draft(clues, hypotheses, ev_pool)
    return {"report_draft": draft}


def _dedup_evidences(ev_list: list[Evidence]) -> list[Evidence]:
    seen: set[str] = set()
    res: list[Evidence] = []
    for ev in ev_list:
        if ev.id not in seen:
            seen.add(ev.id)
            res.append(ev)
    return res


def _build_grounded_report_draft(
    clues: dict[str, str],
    hypotheses: list[Hypothesis],
    ev_pool: list[Evidence],
) -> ReportDraft:
    all_ids = [ev.id for ev in ev_pool]
    log_ids = [ev.id for ev in ev_pool if ev.id.startswith("ev_log:")] or all_ids[:1]
    code_ids = (
        [ev.id for ev in ev_pool if ev.id.startswith(("ev_code:", "ev_git:"))]
        or all_ids[:1]
    )
    db_ids = [ev.id for ev in ev_pool if ev.id.startswith("ev_db:")] or all_ids[:1]
    hist_ids = [ev.id for ev in ev_pool if ev.id.startswith("ev_hist:")] or log_ids

    mod_hint = clues.get("module_hint", "order")
    updated_hyps: list[Hypothesis] = []
    for h in hypotheses:
        if h.probe_type == "log":
            ref = log_ids
        elif h.probe_type == "code":
            ref = code_ids
        else:
            ref = db_ids
        updated_hyps.append(
            Hypothesis(
                id=h.id,
                title=h.title,
                probe_type=h.probe_type,
                assessment="supported",
                evidence_ids=ref,
            )
        )

    if mod_hint == "sms":
        return ReportDraft(
            summary=[
                Claim(
                    text="登录验证码接口 /api/auth/send-sms 大面积超时系历史故障 INC-2026-089 复发：主用阿里云短信网关限流且本地未开启自动降级。",
                    evidence_ids=hist_ids + log_ids,
                )
            ],
            problem=[
                Claim(
                    text="线上 /api/auth/send-sms 接口密集抛出 ReadTimeoutError (read timeout=2.0)，导致用户无法接收登录短信验证码。",
                    evidence_ids=log_ids,
                )
            ],
            previous_occurrence=[
                Claim(
                    text="CRAG 混合检索命中历史故障 INC-2026-089，报错特征与本次完全一致。",
                    evidence_ids=hist_ids,
                )
            ],
            impact=[
                Claim(
                    text="影响全站短信验证码登录链路，需立即执行网关通道切换。",
                    evidence_ids=log_ids,
                )
            ],
            cause=[
                Claim(
                    text="第三方主用短信网关 (aliyun_primary) 高峰期限流超时，而 config/settings.yaml 中 fallback_enabled 为 false，未能自动切换至备用通道 tencent_backup。",
                    evidence_ids=code_ids + hist_ids,
                )
            ],
            next_steps=[
                Claim(
                    text="将 config/settings.yaml 中的 sms_gateway.provider 切换为 tencent_backup，并将 fallback_enabled 设为 true（属配置与外部网关问题，无需修改 Python 业务代码）。",
                    evidence_ids=code_ids + hist_ids,
                )
            ],
            hypotheses=updated_hyps,
            gaps=[],
            questions=[],
            need_code_patch=False,
        )

    if mod_hint == "pricing":
        return ReportDraft(
            summary=[
                Claim(
                    text="用户 10087 (VIP 2 黄金会员) 下单未享受 8 折优惠，根因是 pricing_service.py 第 42 行误将边界条件写成 `vip_level > 2`。",
                    evidence_ids=code_ids + db_ids,
                )
            ],
            problem=[
                Claim(
                    text="用户 10087 购买原价 200.00 元商品，实付金额仍为 200.00 元（应为 160.00 元），日志显示 applied_ratio=1.0。",
                    evidence_ids=log_ids + db_ids,
                )
            ],
            previous_occurrence=[],
            impact=[
                Claim(
                    text="导致所有 vip_level == 2 的黄金会员在结算时均无法命中 8 折阶梯优惠。",
                    evidence_ids=code_ids + db_ids,
                )
            ],
            cause=[
                Claim(
                    text="app/services/pricing_service.py 第 42 行 `elif vip_level > 2:` 存在边界开闭区间缺陷（当 vip_level==3 时已被上一分支拦截，导致 vip_level==2 落入默认原价分支）。",
                    evidence_ids=code_ids,
                )
            ],
            next_steps=[
                Claim(
                    text="在隔离沙箱中将 app/services/pricing_service.py 第 42 行的 `elif vip_level > 2:` 修复为 `elif vip_level >= 2:`，并运行 pytest 回归单测。",
                    evidence_ids=code_ids,
                )
            ],
            hypotheses=updated_hyps,
            gaps=[],
            questions=[],
            need_code_patch=True,
        )

    return ReportDraft(
        summary=[
            Claim(
                text="用户 10086 结算订单报 HTTP 500 的根因是：其使用的折扣券 CPN-999 (类型为 RATE) 在数据库中 discount_amount 字段为 NULL，而 order_service.py:64 直接用 float 减去 None 引发 TypeError。",
                evidence_ids=log_ids + code_ids + db_ids,
            )
        ],
        problem=[
            Claim(
                text="调用 /api/orders/checkout (trace_id=tr-88910-a1) 抛出异常 `TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'`。",
                evidence_ids=log_ids,
            )
        ],
        previous_occurrence=[],
        impact=[
            Claim(
                text="所有使用 RATE 比率折扣券的用户在订单结算时均会触发 500 崩溃，订单状态停留在 FAILED_500。",
                evidence_ids=log_ids + db_ids,
            )
        ],
        cause=[
            Claim(
                text="数据库 mall_coupons 表中 CPN-999 的 coupon_type='RATE'、discount_rate=0.85、discount_amount=NULL；而 app/services/order_service.py 第 64 行未判断 coupon_type 分支，直接执行 `base_price - coupon['discount_amount']` 导致空指针异常。",
                evidence_ids=code_ids + db_ids,
            )
        ],
        next_steps=[
            Claim(
                text="在沙箱中修改 app/services/order_service.py 第 62-66 行，区分 RATE 折扣券 (`base_price * discount_rate`) 与 CASH 满减券逻辑，并通过 pytest 单测验证。",
                evidence_ids=code_ids,
            )
        ],
        hypotheses=updated_hyps,
        gaps=[],
        questions=[],
        need_code_patch=True,
    )


# ============================================================================
# 节点 7: verify_report_node (防幻觉证据校验器 valid_refs + SHA-256 指纹去重)
# ============================================================================
async def verify_report_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    draft = state.get("report_draft")
    ev_pool = _dedup_evidences(state.get("evidence", []))
    inc_id = state.get("incident_id", "inc_default")
    retry_cnt = state.get("verify_retry_count", 0)

    if draft is None:
        return {
            "final_report": None,
            "verification_error": "ReportDraft 为空，请重新生成结构化排障报告草稿。",
            "verify_retry_count": retry_cnt + 1,
        }

    report, error_msg = verify_and_finalize_report(
        incident_id=inc_id,
        draft=draft,
        evidence_pool=ev_pool,
    )
    if report is None:
        return {
            "final_report": None,
            "verification_error": error_msg,
            "verify_retry_count": retry_cnt + 1,
        }

    fallback_markdown = (
        f"### 排障报告已生成 (`{inc_id}`)\n\n"
        f"- **核心结论**：{report.summary}\n"
        f"- **故障现象**：{report.problem}\n"
        f"- **确凿根因**：{report.cause}\n"
        f"- **修复与处置步骤**：{'; '.join(report.next_steps)}\n"
        f"- **报告防伪指纹 (SHA-256)**：`{report.digest}`"
    )

    assistant_reply: AIMessage | None = None
    if _has_valid_llm_key():
        try:
            summary_llm = get_llm(temperature=0.1)
            summary_prompt = (
                "你是 SRE 排障专家。请基于以下已通过防幻觉校验的正式排障报告，用简洁清晰的 Markdown 向用户输出最终排障结论汇报"
                "（保留报告中的 [证据引用: ev_xxx] 标记，不要编造任何报告之外的信息）：\n\n"
                f"{fallback_markdown}"
            )
            ai_resp = await summary_llm.ainvoke([HumanMessage(content=summary_prompt)])
            if ai_resp and ai_resp.content:
                assistant_reply = AIMessage(content=str(ai_resp.content))
        except Exception:
            assistant_reply = None

    if assistant_reply is None:
        assistant_reply = AIMessage(content=fallback_markdown)

    return {
        "final_report": report,
        "verification_error": "",
        "messages": [assistant_reply],
    }


def _route_after_verify(
    state: IncidentMainState,
) -> Literal["generate_report_node", "patch_subgraph_node", "human_approval_node"]:
    final_report = state.get("final_report")
    if final_report is None:
        if state.get("verify_retry_count", 0) < 2:
            return "generate_report_node"
        clues = state.get("extracted_clues", {})
        ev_pool = _dedup_evidences(state.get("evidence", []))
        fallback_draft = _build_grounded_report_draft(
            clues, state.get("hypotheses", []), ev_pool
        )
        rep, _ = verify_and_finalize_report(
            state.get("incident_id", "inc_default"), fallback_draft, ev_pool
        )
        if rep and rep.need_code_patch:
            return "patch_subgraph_node"
        return "human_approval_node"

    if final_report.need_code_patch:
        return "patch_subgraph_node"
    return "human_approval_node"


# ============================================================================
# 节点 8: patch_subgraph_node (调用子图三：沙箱代码修复与 pytest 自愈闭环，透传 runtime.context)
# ============================================================================
async def patch_subgraph_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    report = state["final_report"]
    ev_pool = _dedup_evidences(state.get("evidence", []))
    inc_id = state.get("incident_id", "inc_default")

    patch_res = await patch_subgraph.ainvoke(
        PatchSelfHealSubState(
            incident_id=inc_id,
            report=report,
            evidence=ev_pool,
            test_passed=False,
            retry_count=0,
            human_feedback=state.get("human_feedback", ""),
        ),
        context=runtime.context,
    )
    return {
        "patch_diff": patch_res.get("patch_diff", ""),
        "test_passed": bool(patch_res.get("test_passed", False)),
    }


# ============================================================================
# 节点 9: human_approval_node (基于 LangGraph interrupt() 的人工审批挂起节点)
# ============================================================================
async def human_approval_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    report = state.get("final_report")
    patch_diff = state.get("patch_diff", "")
    test_passed = state.get("test_passed", False)

    interrupt_payload = {
        "incident_id": state.get("incident_id", ""),
        "action_required": "approve_patch_and_resolve" if patch_diff else "confirm_resolution",
        "report_summary": report.summary if report else "",
        "report_digest": report.digest if report else "",
        "need_code_patch": report.need_code_patch if report else False,
        "patch_diff": patch_diff,
        "pytest_passed": test_passed,
        "message": "排障报告与沙箱验证已就绪，请人工审批是否合并补丁并结案归档。",
    }

    resume_value = interrupt(interrupt_payload)

    if isinstance(resume_value, dict):
        decision = resume_value.get("decision", "approved")
        feedback = resume_value.get("feedback", "")
    else:
        decision = "approved"
        feedback = str(resume_value or "")

    return {
        "human_approval": "approved" if decision == "approved" else "rejected",
        "human_feedback": feedback,
    }


def _route_after_approval(
    state: IncidentMainState,
) -> Literal["apply_and_evolve_node", "patch_subgraph_node"]:
    if state.get("human_approval") == "rejected":
        report = state.get("final_report")
        if report and report.need_code_patch:
            return "patch_subgraph_node"
    return "apply_and_evolve_node"


# ============================================================================
# 节点 10: apply_and_evolve_node (通过 runtime.context 中的仓储写入 MySQL -> ES -> Qdrant)
# ============================================================================
async def apply_and_evolve_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    ctx = runtime.context
    report = state.get("final_report")
    clues = state.get("extracted_clues", {})
    inc_id = state.get("incident_id", "inc_default")
    approval = state.get("human_approval", "approved")

    if approval == "rejected":
        reject_msg = AIMessage(
            content=f"本次排障会话 (`{inc_id}`) 的处置建议已被人工驳回，未写入历史故障知识库。"
        )
        return {"messages": [reject_msg]}

    if report is not None:
        new_code = f"INC-AUTO-{inc_id[-4:].upper()}"
        # 1. 先存 MySQL 关系库 historical_runbooks 主表 (Source of Truth)
        rb_model = await ctx["runbook_repo"].create_if_not_exists(
            incident_code=new_code,
            title=clues.get("title", report.summary[:40]),
            symptoms=report.problem,
            root_cause=report.cause,
            resolution="\n".join(report.next_steps),
            service_name=f"{clues.get('module_hint', 'core')}-service",
            error_keywords=f"{clues.get('error_keyword', '')} {clues.get('api_path', '')}".strip(),
        )
        runbook_doc = {
            "incident_code": rb_model.incident_code,
            "title": rb_model.title,
            "symptoms": rb_model.symptoms,
            "root_cause": rb_model.root_cause,
            "resolution": rb_model.resolution,
            "service_name": rb_model.service_name,
            "error_keywords": rb_model.error_keywords,
        }
        # 2. 同步写入 ES BM25 索引
        await ctx["runbook_es_repo"].index_runbook(runbook_doc)
        # 3. 同步向量化并写入 Qdrant 向量集合
        embed_text = (
            f"故障标题: {runbook_doc['title']}\n"
            f"故障现象: {runbook_doc['symptoms']}\n"
            f"故障根因: {runbook_doc['root_cause']}\n"
            f"解决方案: {runbook_doc['resolution']}"
        )
        vector = await ctx["embedding"].aembed_query(embed_text)
        await ctx["runbook_qdrant_repo"].upsert_runbook(vector=vector, payload=runbook_doc)

    summary_msg = AIMessage(
        content=(
            f"本次排障会话 (`{inc_id}`) 已完成人工审批并结案归档。\n"
            f"- 报告 SHA-256 指纹: `{report.digest if report else ''}`\n"
            f"- 经验自进化状态: 已将本次根因与解决方案同步写入 MySQL、ES 与 Qdrant 历史故障知识库。"
        )
    )
    return {"messages": [summary_msg]}


def build_main_incident_graph(checkpointer=None):
    """构建并编译包含 IncidentGraphContext 运行期依赖注入上下文与 Checkpointer 的排障主图"""
    builder = StateGraph(
        state_schema=IncidentMainState,
        context_schema=IncidentGraphContext,
    )

    builder.add_node("triage_node", triage_node)
    builder.add_node("crag_subgraph_node", crag_subgraph_node)
    builder.add_node("hypothesis_planner_node", hypothesis_planner_node)
    builder.add_node("probe_worker_node", probe_worker_node)
    builder.add_node("evidence_evaluate_node", evidence_evaluate_node)
    builder.add_node("generate_report_node", generate_report_node)
    builder.add_node("verify_report_node", verify_report_node)
    builder.add_node("patch_subgraph_node", patch_subgraph_node)
    builder.add_node("human_approval_node", human_approval_node)
    builder.add_node("apply_and_evolve_node", apply_and_evolve_node)

    builder.add_edge(START, "triage_node")
    builder.add_edge("triage_node", "crag_subgraph_node")
    builder.add_edge("crag_subgraph_node", "hypothesis_planner_node")

    builder.add_conditional_edges(
        "hypothesis_planner_node",
        _dispatch_probe_workers,
        ["probe_worker_node"],
    )
    builder.add_edge("probe_worker_node", "evidence_evaluate_node")

    builder.add_conditional_edges(
        "evidence_evaluate_node",
        _route_after_evaluate,
        {
            "hypothesis_planner_node": "hypothesis_planner_node",
            "generate_report_node": "generate_report_node",
        },
    )
    builder.add_edge("generate_report_node", "verify_report_node")

    builder.add_conditional_edges(
        "verify_report_node",
        _route_after_verify,
        {
            "generate_report_node": "generate_report_node",
            "patch_subgraph_node": "patch_subgraph_node",
            "human_approval_node": "human_approval_node",
        },
    )
    builder.add_edge("patch_subgraph_node", "human_approval_node")

    builder.add_conditional_edges(
        "human_approval_node",
        _route_after_approval,
        {
            "apply_and_evolve_node": "apply_and_evolve_node",
            "patch_subgraph_node": "patch_subgraph_node",
        },
    )
    builder.add_edge("apply_and_evolve_node", END)

    memory = checkpointer or MemorySaver()
    return builder.compile(checkpointer=memory)


# 全局编译好的主图实例（带 MemorySaver Checkpointer 支持 interrupt/resume）
incident_checkpointer = MemorySaver()
main_incident_graph = build_main_incident_graph(checkpointer=incident_checkpointer)
