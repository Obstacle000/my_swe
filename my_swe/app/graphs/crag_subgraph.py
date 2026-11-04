"""子图一：CRAG (Corrective RAG) 历史故障双路混合检索与相关性纠错子图
通过 Runtime[IncidentGraphContext] 从运行上下文中获取注入的：
- runbook_es_repo (ES BM25 倒排索引仓储)
- runbook_qdrant_repo (Qdrant 稠密向量检索仓储)
- embedding (OpenAIEmbeddings 向量模型客户端)
"""

import json
from typing import Any, Literal
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.config import get_llm, settings
from app.core.evidence_collector import build_evidence
from app.graphs.context import IncidentGraphContext
from app.graphs.state import CRAGSubState
from app.schemas.incident_schemas import RetrievalGrade


def _has_valid_llm_key() -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"


def rrf_fuse_runbooks(
    bm25_hits: list[dict[str, Any]],
    vec_hits: list[dict[str, Any]],
    top_k: int = 3,
    k_constant: int = 60,
) -> list[dict[str, Any]]:
    """对 ES (BM25) 与 Qdrant (Vector) 两路召回结果执行 RRF (k=60) 倒数秩融合重排。

    公式:
        RRF_Score(d) = 1 / (k + rank_bm25(d)) + 1 / (k + rank_vector(d)), 其中 k = 60
    """
    bm25_rank_map: dict[str, tuple[int, float]] = {}
    doc_map: dict[str, dict[str, Any]] = {}

    for rank, item in enumerate(bm25_hits, start=1):
        code = item["incident_code"]
        bm25_rank_map[code] = (rank, float(item.get("bm25_score", 0.0)))
        doc_map.setdefault(code, item)

    vec_rank_map: dict[str, tuple[int, float]] = {}
    for rank, item in enumerate(vec_hits, start=1):
        code = item["incident_code"]
        vec_rank_map[code] = (rank, float(item.get("vector_score", 0.0)))
        doc_map.setdefault(code, item)

    fused_list: list[dict[str, Any]] = []
    for code, doc in doc_map.items():
        b_rank, b_score = bm25_rank_map.get(code, (999, 0.0))
        v_rank, v_score = vec_rank_map.get(code, (999, 0.0))
        rrf_score = (1.0 / (k_constant + b_rank)) + (1.0 / (k_constant + v_rank))
        effective_score = rrf_score if (b_score > 0 or v_score > 0) else 0.0
        fused_list.append(
            {
                "incident_code": code,
                "title": doc.get("title", ""),
                "symptoms": doc.get("symptoms", ""),
                "root_cause": doc.get("root_cause", ""),
                "resolution": doc.get("resolution", ""),
                "service_name": doc.get("service_name", ""),
                "error_keywords": doc.get("error_keywords", ""),
                "bm25_rank": b_rank,
                "bm25_score": b_score,
                "vector_rank": v_rank,
                "vector_score": v_score,
                "rrf_score": round(effective_score, 6),
            }
        )

    fused_list.sort(key=lambda item: (item["rrf_score"], item["bm25_score"]), reverse=True)
    return fused_list[:top_k]


async def hybrid_retrieve_node(
    state: CRAGSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """通过 runtime.context 中注入的 ES 仓储与 Qdrant 仓储执行双路检索与 RRF (k=60) 融合"""
    ctx = runtime.context
    es_repo = ctx["runbook_es_repo"]
    qdrant_repo = ctx["runbook_qdrant_repo"]
    embedder = ctx["embedding"]

    query = state.get("current_query") or state.get("original_query", "")

    bm25_hits = await es_repo.search_bm25(query, limit=10)
    query_vector = await embedder.aembed_query(query)
    vec_hits = await qdrant_repo.search_vector(query_vector, limit=10)

    docs = rrf_fuse_runbooks(bm25_hits=bm25_hits, vec_hits=vec_hits, top_k=3, k_constant=60)
    return {"retrieved_docs": docs, "current_query": query}


async def grade_docs_node(
    state: CRAGSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """使用 RetrievalGrade 结构化输出对召回的历史 Runbook 逐条进行相关性裁判"""
    query = state.get("current_query", "")
    docs = state.get("retrieved_docs", [])
    relevant: list[dict] = []

    for doc in docs:
        if doc.get("rrf_score", 0.0) <= 0.0 or (
            doc.get("bm25_score", 0.0) <= 0.0 and doc.get("vector_score", 0.0) < 0.15
        ):
            continue

        is_rel = False
        if _has_valid_llm_key():
            try:
                grader_llm = get_llm(temperature=0.0).with_structured_output(RetrievalGrade)
                prompt_text = (
                    "你是 SRE 历史故障相关性裁判。请判断以下【历史故障 Runbook】是否与【当前排障查询】属于同类故障或具有直接参考价值。\n\n"
                    f"【当前排障查询】：{query}\n"
                    f"【历史故障标题】：{doc['title']}\n"
                    f"【历史故障现象】：{doc['symptoms']}\n"
                    f"【历史故障根因】：{doc['root_cause']}\n"
                )
                grade_res: RetrievalGrade = await grader_llm.ainvoke(prompt_text)
                is_rel = grade_res.relevance_score == "yes"
            except Exception:
                is_rel = _rule_based_grade(query, doc)
        else:
            is_rel = _rule_based_grade(query, doc)

        if is_rel:
            relevant.append(doc)

    return {"relevant_docs": relevant}


def _rule_based_grade(query: str, doc: dict) -> bool:
    """确定性相关性裁判兜底逻辑"""
    q_lower = query.lower()
    code = doc.get("incident_code", "")
    if code == "INC-2026-089":
        return any(
            k in q_lower
            for k in ("readtimeouterror", "send-sms", "短信", "验证码", "aliyun-gateway")
        )
    if code == "INC-2026-042":
        return any(k in q_lower for k in ("poolexhaustederror", "seckill", "redis", "连接池"))
    if code == "INC-2026-015":
        return any(k in q_lower for k in ("decimal", "rounding", "汇率", "一分钱"))
    keywords = [kw.lower() for kw in doc.get("error_keywords", "").split() if len(kw) >= 5]
    matched_kw = sum(1 for kw in keywords if kw in q_lower)
    return matched_kw >= 2


async def rewrite_query_node(
    state: CRAGSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """当初次检索未命中高相关 Runbook 时，改写检索词"""
    orig = state.get("original_query", "")
    retry = state.get("retry_count", 0) + 1

    if _has_valid_llm_key():
        try:
            llm = get_llm(temperature=0.0)
            resp = await llm.ainvoke(
                "请将以下排障描述改写为适合 Elasticsearch BM25 与向量混合检索的紧凑技术关键词组合（仅输出改写后的关键词串）：\n"
                f"{orig}"
            )
            rewritten = str(resp.content).strip()
            if rewritten:
                return {"current_query": rewritten, "retry_count": retry}
        except Exception:
            pass

    rewritten = f"{orig} Exception Error timeout 500 504"
    return {"current_query": rewritten.strip(), "retry_count": retry}


async def pack_output_node(
    state: CRAGSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """将通过相关性裁判的 Runbook 打包为历史经验上下文，并自动签发 ev_hist 证据"""
    relevant = state.get("relevant_docs", [])
    if not relevant:
        return {
            "historical_context": "未命中高相关的历史同类故障 Runbook，需进入全量探针排查。",
            "crag_hit": False,
            "evidence": [],
        }

    top_doc = relevant[0]
    context_text = (
        f"【命中历史同类故障 Runbook: {top_doc['incident_code']}】\n"
        f"- 标题: {top_doc['title']}\n"
        f"- 历史症状: {top_doc['symptoms']}\n"
        f"- 历史根因: {top_doc['root_cause']}\n"
        f"- 历史解决方案: {top_doc['resolution']}\n"
        f"- 混合检索得分: RRF={top_doc['rrf_score']} (BM25 Rank={top_doc['bm25_rank']}, Vector Rank={top_doc['vector_rank']})"
    )

    ev = build_evidence(
        tool_name="crag_runbook_search",
        tool_args={
            "query": state.get("current_query", ""),
            "incident_code": top_doc["incident_code"],
        },
        raw_output=json.dumps(top_doc, ensure_ascii=False, indent=2),
        summary_override=f"CRAG 混合检索命中历史故障 {top_doc['incident_code']}: {top_doc['title']}",
    )

    return {
        "historical_context": context_text,
        "crag_hit": True,
        "evidence": [ev],
    }


def _route_after_grade(
    state: CRAGSubState,
) -> Literal["rewrite_query_node", "pack_output_node"]:
    if state.get("relevant_docs"):
        return "pack_output_node"
    if state.get("retry_count", 0) < 1:
        return "rewrite_query_node"
    return "pack_output_node"


def build_crag_subgraph():
    """构建并编译带 IncidentGraphContext 上下文 Schema 的 CRAG 纠错检索子图"""
    builder = StateGraph(state_schema=CRAGSubState, context_schema=IncidentGraphContext)
    builder.add_node("hybrid_retrieve_node", hybrid_retrieve_node)
    builder.add_node("grade_docs_node", grade_docs_node)
    builder.add_node("rewrite_query_node", rewrite_query_node)
    builder.add_node("pack_output_node", pack_output_node)

    builder.add_edge(START, "hybrid_retrieve_node")
    builder.add_edge("hybrid_retrieve_node", "grade_docs_node")
    builder.add_conditional_edges(
        "grade_docs_node",
        _route_after_grade,
        {
            "rewrite_query_node": "rewrite_query_node",
            "pack_output_node": "pack_output_node",
        },
    )
    builder.add_edge("rewrite_query_node", "hybrid_retrieve_node")
    builder.add_edge("pack_output_node", END)
    return builder.compile()


crag_subgraph = build_crag_subgraph()
