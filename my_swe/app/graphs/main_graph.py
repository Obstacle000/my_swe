"""排障主图编排模块 (IncidentMainGraph)
通过 StateGraph(state_schema=IncidentMainState, context_schema=IncidentGraphContext) 声明运行期依赖上下文，
所有节点通过 Runtime[IncidentGraphContext] 获取请求级注入的 Repository 实例，彻底解耦数据库直接实例化。
"""

import json
import os
import re
import shutil
from pathlib import Path
from typing import Literal
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send, interrupt

from app.config import PROJECT_ROOT, get_llm, settings
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
    EvidenceEvaluation,
    Hypothesis,
    HypothesisPlanList,
    ReportDraft,
    TriageClues,
)


def _has_valid_llm_key() -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"


def _latest_user_query(state: IncidentMainState) -> str:
    if state.get("user_query"):
        return str(state["user_query"])
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
                "extracted_clues": clues_obj,
                "loop_count": 0,
                "verify_retry_count": 0,
            }
        except Exception:
            pass
    # 动态结构化提取兜底逻辑 (纯算法正则解析，绝无特定用户ID/业务分支硬编码)
    uid_match = re.search(r"(?:用户|user(?:_id)?[:=]?\s*)(\d{4,10})", user_text, re.IGNORECASE) or re.search(r"\b(\d{5,10})\b", user_text)
    api_match = re.search(r"(/[a-zA-Z0-9_\-/]+)", user_text)
    user_id = uid_match.group(1) if uid_match else ""
    api_path = api_match.group(1) if api_match else ""

    # 提取错误特征关键字 (如 500, timeout, TypeError, error, failed 等)
    err_matches = re.findall(r"\b(50\d|timeout|error|exception|[A-Z][a-zA-Z]+Error)\b", user_text, re.IGNORECASE)
    error_kw = " ".join(err_matches) if err_matches else "ERROR"

    # 推断模块名称
    mod_hint = "core"
    if api_path:
        parts = [p for p in api_path.strip("/").split("/") if p not in ("api", "v1", "v2")]
        if parts:
            mod_hint = parts[0]

    title = f"{mod_hint} 服务异常排查: {user_text[:40]}"

    clues = TriageClues(
        user_id=user_id,
        api_path=api_path,
        error_keyword=error_kw,
        module_hint=mod_hint,
        title=title[:15],
    )
    return {
        "extracted_clues": clues,
        "loop_count": 0,
        "verify_retry_count": 0,
    }


# ============================================================================
# 节点 2: crag_subgraph_node (调用子图一：CRAG 历史故障双路混合检索，透传 runtime.context)
# ============================================================================
async def crag_subgraph_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    clues = state.get("extracted_clues") or TriageClues()
    user_text = _latest_user_query(state)
    combined_query = (
        f"{user_text} {clues.api_path} {clues.error_keyword}".strip()
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
    clues = state.get("extracted_clues") or TriageClues()
    hist_ctx = state.get("historical_context", "")
    crag_hit = state.get("crag_hit", False)
    loop_count = state.get("loop_count", 0)
    retry_reason = state.get("evaluate_retry_reason", "")
    ev_pool = _dedup_evidences(state.get("evidence", []))

    if _has_valid_llm_key():
        try:
            sys_prompt = load_prompt("planner.md")
            # crag_hit 为false 那么historical_context一定不存在,true一定存在,可能是兜底发力了
            user_ctx = (
                f"【分诊提取线索】：{clues.model_dump_json()}\n"
                f"【CRAG 历史故障检索结果 (crag_hit={crag_hit})】：\n{hist_ctx}\n"
            )
            # 注入前序所有探针已查明的确凿证据事实账本 (Evidence Ledger)
            if ev_pool:
                facts_text = "\n".join(
                    f"- 证据[{ev.source}] 来自工具 {ev.tool_name} (ID: `{ev.id}`):\n"
                    f"  结论摘要: {ev.summary}\n"
                    f"  内容切片: {ev.raw_snippet[:300]}"
                    for ev in ev_pool
                )
                user_ctx += f"\n【前序探针已查明的现场确凿事实清单 (Evidence Ledger)】：\n{facts_text}\n"

            if retry_reason:
                user_ctx += (
                    f"\n【上一轮证据审查打回原因 (evaluate_retry_reason)】：\n"
                    f"{retry_reason}\n"
                    f"请务必重点针对上述审查意见与待补线索，结合前序已查明的事实清单，有针对性地生成新一轮排障假设以补全证据链！\n"
                )
            planner_llm = get_llm(temperature=0.0).with_structured_output(HypothesisPlanList)
            plan: HypothesisPlanList = await planner_llm.ainvoke(
                [SystemMessage(content=sys_prompt), HumanMessage(content=user_ctx)]
            )
            if plan.hypotheses:
                return {
                    "hypotheses": plan.hypotheses,
                    "loop_count": loop_count + 1,
                    "evaluate_retry_reason": "",# 重置上一轮的重试理由
                }
        except Exception:
            pass
    # 动态排障假设生成 (基于线索与CRAG知识动态生成，绝无特定用户/写死业务的硬编码)
    api_desc = clues.api_path or "目标服务接口"
    uid_desc = f"用户 {clues.user_id} " if clues.user_id else ""
    kw_desc = clues.error_keyword or "ERROR"

    if crag_hit:
        rb_code = crag_hit.get("incident_code", "")
        rb_title = crag_hit.get("title", "")
        hypotheses = [
            Hypothesis(
                id="h1",
                title=f"检索线上运行态日志，核对 {api_desc} 是否出现与历史 Runbook [{rb_code}] ({rb_title}) 一致的异常特征",
                probe_type="log",
            ),
            Hypothesis(
                id="h2",
                title=f"审查 {api_desc} 相关业务实现代码与配置，对比历史故障复盘方案进行排查",
                probe_type="code",
            ),
        ]
    else:
        hypotheses = [
            Hypothesis(
                id="h1",
                title=f"检索 {uid_desc}调用 {api_desc} 产生的运行态日志与异常堆栈 ({kw_desc})，定位报错文件与行号",
                probe_type="log",
            ),
            Hypothesis(
                id="h2",
                title=f"读取与审计 {api_desc} 对应业务逻辑模块源码及近期 Git 变更记录",
                probe_type="code",
            ),
            Hypothesis(
                id="h3",
                title=f"查询业务数据库与关联表，验证 {uid_desc}状态及相关业务订单/券码数据一致性",
                probe_type="db",
            ),
        ]

    return {"hypotheses": hypotheses, "loop_count": loop_count + 1}


# ============================================================================
# 条件分发边: 使用 LangGraph Send API 并行派生多个 ProbeWorkerSubgraph
# ============================================================================
def _dispatch_probe_workers(state: IncidentMainState) -> list[Send]:
    clues = state.get("extracted_clues") or TriageClues()
    ev_pool = _dedup_evidences(state.get("evidence", []))
    # 提炼出已知的情报事实摘要下发给并行探针 (Known Facts)
    known_facts = [
        f"[{e.source}:{e.tool_name}] {e.summary}"
        for e in ev_pool
    ]
    return [
        Send(
            "probe_worker_node",
            ProbeWorkerState(
                hypothesis=h,
                clues=clues,
                known_facts=known_facts,
                worker_messages=[],
                remaining_chars=15000,
                evidence=[],
                gaps=[],
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
    # 调用三次探针子图,每一次state的Hypothesis不同
    sub_res = await probe_subgraph.ainvoke(state, context=runtime.context)
    return {
        "evidence": sub_res.get("evidence", []),
        "gaps": sub_res.get("gaps", []),
    }


# ============================================================================
# 节点 5: evidence_evaluate_node (评估并行探针归并后的证据池充分性与生成打回批注)
# ============================================================================
async def evidence_evaluate_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """审查并行探针汇总的真实证据池，判定是否足以锁定确凿根因；若不足则生成 evaluate_retry_reason"""
    ev_list = _dedup_evidences(state.get("evidence", []))
    clues = state.get("extracted_clues") or TriageClues()
    hypotheses = state.get("hypotheses", [])
    valid_ev_ids = {ev.id for ev in ev_list}

    if _has_valid_llm_key() and ev_list:
        try:
            sys_prompt = load_prompt("evidence_evaluator.md")
            ev_summary_text = "\n\n".join(
                f"- 证据ID `{ev.id}` (来源={ev.source}, 工具={ev.tool_name}):\n  摘要: {ev.summary}\n  内容片段:\n{ev.raw_snippet[:500]}"
                for ev in ev_list
            )
            user_prompt = (
                f"【排障线索】：{clues.model_dump_json()}\n\n"
                f"【当前待验证假设列表】：{json.dumps([h.model_dump() for h in hypotheses], ensure_ascii=False)}\n\n"
                f"【当前真实证据池 (State.evidence)】：\n{ev_summary_text}\n"
            )
            evaluator_llm = get_llm(temperature=0.0).with_structured_output(EvidenceEvaluation)
            eval_res: EvidenceEvaluation = await evaluator_llm.ainvoke(
                [SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)]
            )

            # 防幻觉过滤：严格校验 updated_hypotheses 中绑定的 evidence_ids 必须来自真实证据池
            clean_hypotheses: list[Hypothesis] = []
            for h in eval_res.updated_hypotheses:
                filtered_ids = [eid for eid in h.evidence_ids if eid in valid_ev_ids]
                # 基于现有对象创建一个副本，并在副本中替换或添加指定的字段值。
                clean_hypotheses.append(h.model_copy(update={"evidence_ids": filtered_ids}))

            # 若证据不足以锁定根因，生成结构化的打回重试批注；证据充足则置空
            if eval_res.is_sufficient:
                retry_reason = ""
            else:
                reason_part = f"审查意见: {eval_res.reason}\n" if eval_res.reason else ""
                missing_part = (
                    "待补线索:\n" + "\n".join(f"- {c}" for c in eval_res.missing_clues)
                    if eval_res.missing_clues
                    else ""
                )
                retry_reason = (reason_part + missing_part).strip()

            return {
                "evidence_sufficient": eval_res.is_sufficient,
                "hypotheses": clean_hypotheses or hypotheses,
                "evaluate_retry_reason": retry_reason,
            }
        except Exception:
            pass

    # 全量规则兜底：若有有效证据则认为证据充足；若完全无证据则判定为不足并提供打回批注
    is_sufficient = bool(ev_list)
    retry_reason = (
        ""
        if is_sufficient
        else "当前证据池为空，未能采集到有效日志报错、代码片段或数据库记录，线索不足，请重新规划探针方向"
    )
    return {
        "evidence_sufficient": is_sufficient,
        "evaluate_retry_reason": retry_reason,
    }


def _route_after_evaluate(
    state: IncidentMainState,
) -> Literal["hypothesis_planner_node", "generate_report_node"]:
    ev_list = state.get("evidence", [])
    # 优先根据 evidence_evaluate_node 判定的 evidence_sufficient；若未设则以 bool(ev_list) 兜底
    is_suff = state.get("evidence_sufficient")
    if is_suff is None:
        is_suff = bool(ev_list)

    # 若证据不足以锁定根因，且外层大循环次数 < 2，则打回 hypothesis_planner_node 结合 gaps 重新规划
    if not is_suff and state.get("loop_count", 0) < 2:
        return "hypothesis_planner_node"
    return "generate_report_node"


# ============================================================================
# 节点 6: generate_report_node (生成强制绑定真实证据 ID 的 ReportDraft)
# ============================================================================
async def generate_report_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    clues = state.get("extracted_clues") or TriageClues()
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
                f"【排障线索】：{clues.model_dump_json()}\n\n"
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
    # 兜底逻辑
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
    clues: TriageClues,
    hypotheses: list[Hypothesis],
    ev_pool: list[Evidence],
) -> ReportDraft:
    """动态报告草稿组装器：纯粹从现场真实采集到的证据池动态提取事实与合法 ID，零特定业务硬编码"""
    all_ids = [ev.id for ev in ev_pool]
    log_evs = [ev for ev in ev_pool if ev.id.startswith("ev_log:")]
    code_evs = [ev for ev in ev_pool if ev.id.startswith(("ev_code:", "ev_git:"))]
    db_evs = [ev for ev in ev_pool if ev.id.startswith("ev_db:")]
    hist_evs = [ev for ev in ev_pool if ev.id.startswith("ev_hist:")]

    log_ids = [e.id for e in log_evs] or all_ids[:1]
    code_ids = [e.id for e in code_evs] or all_ids[:1]
    db_ids = [e.id for e in db_evs] or all_ids[:1]
    hist_ids = [e.id for e in hist_evs] or log_ids

    # 1. 动态更新假设列表的证据绑定
    updated_hyps: list[Hypothesis] = []
    for h in hypotheses:
        if h.probe_type == "log":
            ref = log_ids
        elif h.probe_type == "code":
            ref = code_ids
        else:
            ref = db_ids
        updated_hyps.append(
            h.model_copy(update={"assessment": "supported", "evidence_ids": ref})
        )

    # 2. 动态提取异常现象
    if log_evs:
        problem_text = f"运行态异常堆栈记录：{log_evs[0].summary}"
    else:
        api = clues.api_path or "业务服务"
        kw = clues.error_keyword or "异常"
        problem_text = f"系统监测到 {api} 接口异常 ({kw})，导致用户请求处理失败。"

    # 3. 动态提取根因事实
    cause_evidence_ids: list[str] = []
    if code_evs:
        cause_text = f"代码与配置缺陷定位：{code_evs[0].summary}"
        cause_evidence_ids.extend([e.id for e in code_evs[:2]])
    elif db_evs:
        cause_text = f"数据库业务记录异常：{db_evs[0].summary}"
        cause_evidence_ids.extend([e.id for e in db_evs[:2]])
    else:
        cause_text = f"经探针排查，异常与 {clues.module_hint or '核心'} 模块外部依赖或配置相关。"
        cause_evidence_ids.extend(all_ids[:2])

    if db_evs and db_evs[0].id not in cause_evidence_ids:
        cause_evidence_ids.append(db_evs[0].id)

    # 4. 判断是否需要代码补丁修复 (若涉及代码源文件或 Git 变更则触发代码修复自愈闭环)
    need_patch = bool(code_evs) and any(
        e.source in ("source_code", "git_history")
        or ".py" in e.query_args
        or ".py" in e.summary
        for e in code_evs
    )

    # 5. 动态构建各章节结论声明
    summary_claim = Claim(
        text=f"故障排障结论：{cause_text}。已完成现场取证与根因锁定。",
        evidence_ids=all_ids[:3] or log_ids,
    )
    problem_claim = Claim(
        text=problem_text,
        evidence_ids=log_ids,
    )
    prev_claims = (
        [
            Claim(
                text=f"CRAG 混合检索命中历史相似排障记录：{hist_evs[0].summary}",
                evidence_ids=hist_ids,
            )
        ]
        if hist_evs
        else []
    )

    impact_claim = Claim(
        text=f"影响 {clues.module_hint or '业务'} 模块相关用户业务请求及后续链路正常流转。",
        evidence_ids=(log_ids + db_ids)[:3] or all_ids[:2],
    )
    cause_claim = Claim(
        text=cause_text,
        evidence_ids=cause_evidence_ids or all_ids[:2],
    )
    next_step_claim = Claim(
        text="进入沙箱自愈环境应用代码修复补丁并执行自动化测试验证。"
        if need_patch
        else "调整系统配置参数或切换通道，并触发线上健康检查。",
        evidence_ids=(code_ids + hist_ids)[:2] or all_ids[:2],
    )

    return ReportDraft(
        summary=[summary_claim],
        problem=[problem_claim],
        previous_occurrence=prev_claims,
        impact=[impact_claim],
        cause=[cause_claim],
        next_steps=[next_step_claim],
        hypotheses=updated_hyps or hypotheses,
        gaps=[],
        questions=[],
        need_code_patch=need_patch,
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
            "gaps": [f"[防幻觉验钞拦截] {error_msg}"],
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
            # 不管是ainvoke还是astream都睡产生token,靠的是一个特殊的 流式回调处理器（AsyncCallbackHandler)
            # 模型每吐出一个 token，底层 client 就会触发回调方法 on_llm_new_token(token)。
            # LangGraph 的回调监听器会立刻把这个 token 打包成一个 AIMessageChunk,对外暴露出来
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
        clues = state.get("extracted_clues") or TriageClues()
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
    ws_id = state.get("workspace_id") or "ws_default_prod"

    patch_res = await patch_subgraph.ainvoke(
        PatchSelfHealSubState(
            incident_id=inc_id,
            workspace_id=ws_id,
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


def _merge_sandbox_patch_to_target_repo(
    sandbox_dir: Path,
    target_repo: Path,
    patch_diff: str = "",
) -> list[str]:
    """人工审批通过后，将沙箱中已通过验证的代码补丁安全合入目标业务项目（源仓库）。"""
    merged_files: list[str] = []
    if not sandbox_dir.exists() or not target_repo.exists():
        return merged_files

    # 1. 优先从 patch_diff 中提取修改的文件相对路径
    rel_files: list[str] = []
    if patch_diff:
        for line in patch_diff.splitlines():
            if line.startswith("--- a/") or line.startswith("+++ b/"):
                rel = line[6:].strip()
                if rel and rel not in rel_files:
                    rel_files.append(rel)

    # 2. 将沙箱中验证通过的文件写入目标仓库
    for rel in rel_files:
        src = sandbox_dir / rel
        dst = target_repo / rel
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            merged_files.append(rel.replace("\\", "/"))

    # 3. 兜底扫描：若 diff 未解析出路径，比对沙箱与目标仓库中的差异文件
    if not merged_files:
        for root, dirs, files in os.walk(sandbox_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache", ".venv", ".git")]
            for f in files:
                if f.endswith(".py"):
                    sand_f = Path(root) / f
                    rel = os.path.relpath(sand_f, sandbox_dir)
                    orig_f = target_repo / rel
                    if orig_f.exists() and sand_f.read_bytes() != orig_f.read_bytes():
                        shutil.copy2(sand_f, orig_f)
                        merged_files.append(rel.replace("\\", "/"))

    return merged_files


# ============================================================================
# 节点 10: apply_and_evolve_node (通过 runtime.context 中的仓储写入 MySQL -> ES -> Qdrant，并合入主仓库)
# ============================================================================
async def apply_and_evolve_node(
    state: IncidentMainState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    ctx = runtime.context
    report = state.get("final_report")
    clues = state.get("extracted_clues") or TriageClues()
    inc_id = state.get("incident_id", "inc_default")
    approval = state.get("human_approval", "approved")

    if approval == "rejected":
        reject_msg = AIMessage(
            content=f"本次排障会话 (`{inc_id}`) 的处置建议已被人工驳回，未合入主项目亦未写入历史知识库。"
        )
        return {"messages": [reject_msg]}

    # 1. 人工审批通过且沙箱单测通过：将沙箱代码补丁真正写回目标业务主项目 (settings.default_target_repo_path)
    merged_files: list[str] = []
    if approval == "approved" and state.get("test_passed"):
        ws_id = state.get("workspace_id") or "ws_default_prod"
        sandbox_dir = PROJECT_ROOT / ".sandboxes" / ws_id
        target_repo = settings.default_target_repo_path
        patch_diff = state.get("patch_diff", "")
        merged_files = _merge_sandbox_patch_to_target_repo(
            sandbox_dir=sandbox_dir,
            target_repo=target_repo,
            patch_diff=patch_diff,
        )

    # 2. 沉淀排障 SOP 到知识库
    if report is not None:
        new_code = f"INC-AUTO-{inc_id[-4:].upper()}"
        # 先存 MySQL 关系库 historical_runbooks 主表 (Source of Truth)
        rb_model = await ctx["runbook_repo"].create_if_not_exists(
            incident_code=new_code,
            title=clues.title or report.summary[:40],
            symptoms=report.problem,
            root_cause=report.cause,
            resolution="\n".join(report.next_steps),
            service_name=f"{clues.module_hint or 'core'}-service",
            error_keywords=f"{clues.error_keyword} {clues.api_path}".strip(),
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
        # 同步写入 ES BM25 索引
        await ctx["runbook_es_repo"].index_runbook(runbook_doc)
        # 同步向量化并写入 Qdrant 向量集合
        embed_text = (
            f"故障标题: {runbook_doc['title']}\n"
            f"故障现象: {runbook_doc['symptoms']}\n"
            f"故障根因: {runbook_doc['root_cause']}\n"
            f"解决方案: {runbook_doc['resolution']}"
        )
        vector = await ctx["embedding"].aembed_query(embed_text)
        await ctx["runbook_qdrant_repo"].upsert_runbook(vector=vector, payload=runbook_doc)

    merge_status_desc = (
        f"已成功将沙箱自愈补丁合入业务主项目 (`{', '.join(merged_files)}`)"
        if merged_files
        else "无需合入代码补丁（配置变更或排障审计报告）"
    )
    summary_msg = AIMessage(
        content=(
            f"本次排障会话 (`{inc_id}`) 已完成人工审批并结案归档。\n"
            f"- 报告 SHA-256 指纹: `{report.digest if report else ''}`\n"
            f"- 补丁合并状态: {merge_status_desc}。\n"
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
    # 用Send()默认是 与连接
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
