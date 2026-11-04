"""排障报告防幻觉校验器与 SHA-256 结论指纹去重模块
(完整复刻并增强自 open-swe/openswe/incidents/report.py 与 service.py)
"""

import hashlib
import json
from app.config import now_iso
from app.schemas.incident_schemas import (
    Claim,
    Evidence,
    Hypothesis,
    IncidentReport,
    ReportDraft,
)


def valid_refs(refs: list[str], valid_ids: set[str]) -> list[str]:
    """过滤并返回真实存在于 State.evidence 池中的证据编号列表"""
    seen: set[str] = set()
    verified: list[str] = []
    for ref in refs:
        clean_ref = ref.strip()
        if clean_ref in valid_ids and clean_ref not in seen:
            seen.add(clean_ref)
            verified.append(clean_ref)
    return verified


def _verify_claims_section(
    section_name: str,
    claims: list[Claim],
    valid_ids: set[str],
    require_non_empty: bool = False,
) -> tuple[list[str], list[str]]:
    """校验单个章节的 Claim 列表，返回 (通过校验的带引用文本列表, 违规错误列表)"""
    verified_texts: list[str] = []
    violations: list[str] = []

    if require_non_empty and not claims:
        violations.append(f"[{section_name}] 章节不能为空，必须提供绑定真实证据ID的排障结论。")
        return verified_texts, violations

    for idx, claim in enumerate(claims, start=1):
        text = claim.text.strip()
        if not text:
            continue
        real_refs = valid_refs(claim.evidence_ids, valid_ids)
        if not real_refs:
            violations.append(
                f"[{section_name} 第{idx}条] 结论 '{text[:60]}...' 未绑定真实存在的 evidence_ids "
                f"(模型填写的引用为: {claim.evidence_ids}，但当前真实证据池仅有: {sorted(valid_ids)})。"
            )
        else:
            ref_tag = ", ".join(real_refs)
            verified_texts.append(f"{text} [证据引用: {ref_tag}]")

    return verified_texts, violations


def digest_fields(
    summary: str,
    problem: str,
    cause: str,
    next_steps: list[str],
    hypotheses: list[Hypothesis],
) -> dict:
    """提取用于计算排障报告 SHA-256 指纹的核心确定性字段 (剔除时间戳等易变字段)"""
    return {
        "summary": summary.strip(),
        "problem": problem.strip(),
        "cause": cause.strip(),
        "next_steps": [s.strip() for s in next_steps],
        "hypotheses": [
            {"id": h.id, "title": h.title.strip(), "assessment": h.assessment}
            for h in hypotheses
        ],
    }


def fingerprint(payload: dict) -> str:
    """计算核心结论字典的 64 位 SHA-256 指纹"""
    canonical_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def verify_and_finalize_report(
    incident_id: str,
    draft: ReportDraft,
    evidence_pool: list[Evidence],
) -> tuple[IncidentReport | None, str]:
    """对大模型提交的 ReportDraft 执行严格的防幻觉证据校验。

    返回值:
        (IncidentReport, "") -> 校验全部通过，产出正式排障报告
        (None, violation_error_msg) -> 校验不通过，返回具体的违规原因供 LangGraph 打回重写
    """
    valid_ids = {ev.id for ev in evidence_pool}
    all_violations: list[str] = []

    summary_lines, v_sum = _verify_claims_section("summary", draft.summary, valid_ids, require_non_empty=True)
    problem_lines, v_prob = _verify_claims_section("problem", draft.problem, valid_ids, require_non_empty=True)
    prev_lines, v_prev = _verify_claims_section("previous_occurrence", draft.previous_occurrence, valid_ids, require_non_empty=False)
    impact_lines, v_imp = _verify_claims_section("impact", draft.impact, valid_ids, require_non_empty=False)
    cause_lines, v_cause = _verify_claims_section("cause", draft.cause, valid_ids, require_non_empty=True)
    step_lines, v_steps = _verify_claims_section("next_steps", draft.next_steps, valid_ids, require_non_empty=True)

    all_violations.extend(v_sum + v_prob + v_prev + v_imp + v_cause + v_steps)

    # 校验每个被标记为 supported 的假设是否绑定了真实证据
    cleaned_hypotheses: list[Hypothesis] = []
    for h in draft.hypotheses:
        real_h_refs = valid_refs(h.evidence_ids, valid_ids)
        if h.assessment == "supported" and not real_h_refs:
            all_violations.append(
                f"[hypotheses {h.id}] 假设 '{h.title}' 被标记为 supported，但未提供有效的真实证据ID ({h.evidence_ids})。"
            )
        cleaned_hypotheses.append(
            Hypothesis(
                id=h.id,
                title=h.title,
                probe_type=h.probe_type,
                assessment=h.assessment,
                evidence_ids=real_h_refs,
            )
        )

    if all_violations:
        error_feedback = (
            "【防幻觉校验器拦截】你生成的 ReportDraft 存在以下无证断言或伪造证据编号违规，请修正并仅引用真实证据ID：\n"
            + "\n".join(f"- {v}" for v in all_violations)
        )
        return None, error_feedback

    summary_str = "\n".join(summary_lines)
    problem_str = "\n".join(problem_lines)
    prev_str = "\n".join(prev_lines) if prev_lines else "未命中历史同类故障"
    impact_str = "\n".join(impact_lines) if impact_lines else "影响当前接口关联业务流程"
    cause_str = "\n".join(cause_lines)
    # 纯净信息提取加密,以便查重
    digest_payload = digest_fields(
        summary=summary_str,
        problem=problem_str,
        cause=cause_str,
        next_steps=step_lines,
        hypotheses=cleaned_hypotheses,
    )
    report_digest = fingerprint(digest_payload)

    report = IncidentReport(
        incident_id=incident_id,
        summary=summary_str,
        problem=problem_str,
        previous_occurrence=prev_str,
        impact=impact_str,
        cause=cause_str,
        next_steps=step_lines,
        outcome="findings" if cause_str else "inconclusive",
        need_code_patch=draft.need_code_patch,
        hypotheses=cleaned_hypotheses,
        evidence=evidence_pool,
        gaps=draft.gaps,
        questions=draft.questions,
        digest=report_digest,
        created_at=now_iso(),
    )
    return report, ""
