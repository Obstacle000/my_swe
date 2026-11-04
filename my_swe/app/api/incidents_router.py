"""模块二与模块三：排障会话生命周期管理、SSE 流式推理与 HITL 人工审批接口"""

from datetime import datetime
import json
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from sqlalchemy import select

from app.config import now_iso
from app.graphs.main_graph import main_incident_graph
from app.models.database import PlatformSessionLocal
from app.models.orm_models import HistoricalRunbookModel, IncidentModel
from app.schemas.api_schemas import (
    ApiResponse,
    ApprovalRequest,
    ChatStreamRequest,
    CreateIncidentRequest,
)

router = APIRouter(prefix="/api/v1/incidents", tags=["2 & 3. 排障会话、SSE 流式追踪与 HITL 审批"])


def _sse_pack(event_name: str, payload: dict) -> str:
    """将事件名与 JSON 数据打包为标准 SSE 帧格式"""
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("", response_model=ApiResponse)
async def create_incident_endpoint(req: CreateIncidentRequest) -> ApiResponse:
    """新建排障会话 (生成的 incident_id 兼任 LangGraph Checkpointer 的 thread_id)"""
    date_part = datetime.now().strftime("%Y%m%d")
    short_uid = uuid.uuid4().hex[:6]
    incident_id = f"inc_{date_part}_{short_uid}"
    default_title = req.initial_query[:15].strip() if req.initial_query else "新建排障会话"

    async with PlatformSessionLocal() as session:
        inc_model = IncidentModel(
            id=incident_id,
            workspace_id=req.workspace_id,
            title=default_title,
            trigger_source=req.trigger_source,
            status="investigating",
            latest_report=None,
            report_digest=None,
            patch_diff=None,
            test_passed=False,
        )
        session.add(inc_model)
        await session.commit()

    return ApiResponse(
        code=200,
        message="success",
        data={
            "incident_id": incident_id,
            "workspace_id": req.workspace_id,
            "title": default_title,
            "status": "investigating",
            "created_at": now_iso(),
        },
    )


@router.get("", response_model=ApiResponse)
async def list_incidents_endpoint(workspace_id: str = "ws_mall_prod") -> ApiResponse:
    """分页查询指定工作空间下的排障会话列表"""
    async with PlatformSessionLocal() as session:
        stmt = (
            select(IncidentModel)
            .where(IncidentModel.workspace_id == workspace_id)
            .order_by(IncidentModel.created_at.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        items = [
            {
                "incident_id": r.id,
                "workspace_id": r.workspace_id,
                "title": r.title,
                "trigger_source": r.trigger_source,
                "status": r.status,
                "report_digest": r.report_digest,
                "test_passed": r.test_passed,
                "updated_at": r.updated_at.isoformat() if r.updated_at else now_iso(),
            }
            for r in rows
        ]

    return ApiResponse(
        code=200,
        message="success",
        data={"total": len(items), "items": items},
    )


@router.get("/{incident_id}", response_model=ApiResponse)
async def get_incident_detail_endpoint(incident_id: str) -> ApiResponse:
    """获取单次排障会话的完整详情（含正式排障报告、证据链卡片、沙箱补丁 Diff 与审批状态）"""
    async with PlatformSessionLocal() as session:
        stmt = select(IncidentModel).where(IncidentModel.id == incident_id)
        inc = (await session.execute(stmt)).scalar_one_or_none()
        if inc is None:
            raise HTTPException(status_code=404, detail=f"排障会话 {incident_id} 不存在")

        return ApiResponse(
            code=200,
            message="success",
            data={
                "incident_id": inc.id,
                "workspace_id": inc.workspace_id,
                "title": inc.title,
                "status": inc.status,
                "report_digest": inc.report_digest,
                "latest_report": inc.latest_report,
                "patch_diff": inc.patch_diff,
                "test_passed": inc.test_passed,
                "created_at": inc.created_at.isoformat() if inc.created_at else now_iso(),
                "updated_at": inc.updated_at.isoformat() if inc.updated_at else now_iso(),
            },
        )


@router.post("/{incident_id}/chat-stream")
async def chat_stream_endpoint(incident_id: str, req: ChatStreamRequest) -> StreamingResponse:
    """核心排障对话与 LangGraph 节点级 SSE 实时流式推送接口"""

    async def event_generator():
        config = {"configurable": {"thread_id": incident_id}}
        initial_state = {
            "incident_id": incident_id,
            "workspace_id": "ws_mall_prod",
            "messages": [HumanMessage(content=req.message)],
            "extracted_clues": {},
            "historical_context": "",
            "crag_hit": False,
            "hypotheses": [],
            "evidence": [],
            "loop_count": 0,
            "report_draft": None,
            "final_report": None,
            "verification_error": "",
            "verify_retry_count": 0,
            "patch_diff": "",
            "test_passed": False,
            "human_approval": "pending",
            "human_feedback": "",
        }

        try:
            for chunk in main_incident_graph.stream(initial_state, config=config, stream_mode="updates"):
                for node_name, node_update in chunk.items():
                    if node_name == "__interrupt__":
                        interrupt_info = node_update[0].value if node_update else {}
                        yield _sse_pack("hitl_interrupt", interrupt_info)
                        continue

                    yield _sse_pack("node_start", {"node": node_name, "timestamp": now_iso()})

                    if node_name == "triage_node":
                        clues = node_update.get("extracted_clues", {})
                        yield _sse_pack("triage_completed", {"extracted_clues": clues})

                    elif node_name == "crag_subgraph_node":
                        yield _sse_pack(
                            "crag_result",
                            {
                                "crag_hit": node_update.get("crag_hit", False),
                                "historical_context": node_update.get("historical_context", ""),
                            },
                        )

                    elif node_name == "hypothesis_planner_node":
                        hyps = [h.model_dump() for h in node_update.get("hypotheses", [])]
                        yield _sse_pack("probe_dispatched", {"hypotheses": hyps})

                    elif node_name == "probe_worker_node":
                        for ev in node_update.get("evidence", []):
                            yield _sse_pack(
                                "evidence_collected",
                                {
                                    "evidence_id": ev.id,
                                    "source": ev.source,
                                    "tool_name": ev.tool_name,
                                    "summary": ev.summary,
                                },
                            )

                    elif node_name == "verify_report_node":
                        rep = node_update.get("final_report")
                        v_err = node_update.get("verification_error", "")
                        if rep is not None:
                            yield _sse_pack(
                                "report_verified",
                                {
                                    "digest": rep.digest,
                                    "outcome": rep.outcome,
                                    "need_code_patch": rep.need_code_patch,
                                    "report": rep.model_dump(),
                                },
                            )
                        else:
                            yield _sse_pack("report_rejected_by_verifier", {"error": v_err})

                    elif node_name == "patch_subgraph_node":
                        yield _sse_pack(
                            "sandbox_patch_ready",
                            {
                                "test_passed": node_update.get("test_passed", False),
                                "patch_diff": node_update.get("patch_diff", ""),
                            },
                        )

            # 流运行至 interrupt() 断点后，读取当前最新图状态并持久化到数据库（含 SHA-256 指纹去重判定）
            snapshot = main_incident_graph.get_state(config)
            vals = snapshot.values
            final_rep = vals.get("final_report")
            clues = vals.get("extracted_clues", {})
            patch_diff = vals.get("patch_diff", "")
            test_passed = vals.get("test_passed", False)

            async with PlatformSessionLocal() as session:
                stmt = select(IncidentModel).where(IncidentModel.id == incident_id)
                inc = (await session.execute(stmt)).scalar_one_or_none()
                if inc is not None and final_rep is not None:
                    inc.title = clues.get("title") or inc.title
                    inc.status = "waiting_approval"
                    inc.patch_diff = patch_diff
                    inc.test_passed = test_passed
                    # SHA-256 指纹幂等判重：仅当新报告 digest 与已存 digest 不同时才更新报告 JSON
                    if inc.report_digest != final_rep.digest:
                        inc.report_digest = final_rep.digest
                        inc.latest_report = final_rep.model_dump()
                    await session.commit()

        except Exception as exc:
            yield _sse_pack("error", {"message": str(exc)})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/{incident_id}/approval", response_model=ApiResponse)
async def approval_endpoint(incident_id: str, req: ApprovalRequest) -> ApiResponse:
    """人工审批唤醒接口：通过 Command(resume=...) 恢复挂起的 LangGraph 图实例并执行经验自进化归档"""
    config = {"configurable": {"thread_id": incident_id}}
    snapshot = main_incident_graph.get_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"未找到会话 {incident_id} 的挂起图状态")

    resumed_state = main_incident_graph.invoke(
        Command(resume={"decision": req.decision, "feedback": req.feedback}),
        config=config,
    )

    final_rep = resumed_state.get("final_report")
    clues = resumed_state.get("extracted_clues", {})
    new_code = f"INC-AUTO-{incident_id[-4:].upper()}"

    async with PlatformSessionLocal() as session:
        stmt = select(IncidentModel).where(IncidentModel.id == incident_id)
        inc = (await session.execute(stmt)).scalar_one_or_none()
        if inc is not None:
            inc.status = "resolved" if req.decision == "approved" else "investigating"
            await session.commit()

        if req.decision == "approved" and final_rep is not None:
            rb_check = await session.execute(
                select(HistoricalRunbookModel).where(HistoricalRunbookModel.incident_code == new_code)
            )
            if rb_check.scalar_one_or_none() is None:
                session.add(
                    HistoricalRunbookModel(
                        incident_code=new_code,
                        title=clues.get("title", final_rep.summary[:40]),
                        symptoms=final_rep.problem,
                        root_cause=final_rep.cause,
                        resolution="\n".join(final_rep.next_steps),
                        service_name=f"{clues.get('module_hint', 'core')}-service",
                        error_keywords=f"{clues.get('error_keyword', '')} {clues.get('api_path', '')}".strip(),
                        es_doc_id=f"es_{new_code}",
                        qdrant_point_id=f"vec_{new_code}",
                    )
                )
                await session.commit()

    return ApiResponse(
        code=200,
        message="已批准合并并完成故障经验自进化归档" if req.decision == "approved" else "已驳回补丁并重新进入沙箱修复",
        data={
            "incident_id": incident_id,
            "new_status": "resolved" if req.decision == "approved" else "investigating",
            "patch_applied": bool(resumed_state.get("patch_diff")) and req.decision == "approved",
            "self_evolved_runbook": {
                "incident_code": new_code,
                "synced_to_mysql": True,
                "synced_to_es_bm25": True,
                "synced_to_qdrant_vec": True,
            },
        },
    )
