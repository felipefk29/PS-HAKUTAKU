"""FastAPI app — endpoints HTTP para o frontend Next.js (Fase 6).

Endpoints:
    GET  /health                      — liveness/readiness
    GET  /stats                       — contagens do grafo
    GET  /graph                       — snapshot completo (nodes + edges)
    GET  /entities                    — lista filtrável por type
    GET  /entities/{id}               — detalhe + histórico de eventos
    GET  /proposals                   — lista filtrável por status
    PATCH /proposals/{id}/status      — atualizar status
    POST /pipeline/ingest             — ingerir um documento (sync)
    POST /pipeline/reason             — rodar 1 ciclo de raciocínio
    POST /pipeline/cross-link         — rodar cross-linker

Run:
    cd backend && uvicorn hakutaku.api.main:app --reload --port 8000
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from hakutaku.adapters import ChatAdapter, MeetingAdapter
from hakutaku.api.schemas import (
    CrossLinkResponse,
    EntityDetail,
    EntitySummary,
    GraphSnapshot,
    IngestRequest,
    IngestResponse,
    ProposalStatusUpdate,
    ProposalView,
    ReasoningResponse,
    RelationSummary,
    StatsResponse,
)
from hakutaku.extraction import extract_from_document
from hakutaku.graph import GraphRepository, get_repository, ingest_extraction
from hakutaku.llm.client import LLMClient, get_llm_client
from hakutaku.memory import link_questions_to_decisions
from hakutaku.reasoning import run_reasoning_cycle


# =====================================================================
# App
# =====================================================================
app = FastAPI(
    title="Hakutaku API",
    description="Organizational Intelligence Layer — graph + reasoning + proposals",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================================
# Dependencies
# =====================================================================
def repo_dep() -> Iterator[GraphRepository]:
    """Singleton via lru_cache em get_repository — não fechamos por request."""
    yield get_repository()


def llm_dep() -> Iterator[LLMClient]:
    """Singleton — wire DB sink para auditoria de chamadas LLM."""
    llm = get_llm_client()
    repo = get_repository()
    llm.attach_db_sink(repo.insert_llm_call)
    yield llm


# =====================================================================
# Health & stats
# =====================================================================
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "hakutaku-api", "version": "0.1.0"}


@app.get("/stats", response_model=StatsResponse)
def stats(repo: GraphRepository = Depends(repo_dep)) -> StatsResponse:
    s = repo.stats()
    return StatsResponse(**s)


# =====================================================================
# Graph
# =====================================================================
@app.get("/graph", response_model=GraphSnapshot)
def graph_snapshot(repo: GraphRepository = Depends(repo_dep)) -> GraphSnapshot:
    snap = repo.get_full_graph()
    entities = [
        EntitySummary(
            id=UUID(str(e["id"])),
            type=e["type"],
            canonical_name=e["canonical_name"],
            aliases=list(e.get("aliases") or []),
            state=(e.get("current_state") or {}).get("state"),
            confidence=float(e.get("confidence") or 1.0),
        )
        for e in snap["entities"]
    ]
    relations = [
        RelationSummary(
            id=UUID(str(r["id"])),
            from_entity=UUID(str(r["from_entity"])),
            to_entity=UUID(str(r["to_entity"])),
            relation_type=r["relation_type"],
            confidence=float(r.get("confidence") or 1.0),
            attributes=dict(r.get("attributes") or {}),
        )
        for r in snap["relations"]
    ]
    return GraphSnapshot(
        generated_at=snap["generated_at"],
        entities=entities,
        relations=relations,
    )


# =====================================================================
# Entities
# =====================================================================
@app.get("/entities", response_model=list[EntitySummary])
def list_entities(
    type: str | None = None,
    repo: GraphRepository = Depends(repo_dep),
) -> list[EntitySummary]:
    snap = repo.get_full_graph()
    items: list[EntitySummary] = []
    for e in snap["entities"]:
        if type and e["type"] != type:
            continue
        items.append(
            EntitySummary(
                id=UUID(str(e["id"])),
                type=e["type"],
                canonical_name=e["canonical_name"],
                aliases=list(e.get("aliases") or []),
                state=(e.get("current_state") or {}).get("state"),
                confidence=float(e.get("confidence") or 1.0),
            )
        )
    return items


@app.get("/entities/{entity_id}", response_model=EntityDetail)
def get_entity(
    entity_id: UUID,
    repo: GraphRepository = Depends(repo_dep),
) -> EntityDetail:
    # Reusa get_full_graph (cabe em memória no MVP) e filtra.
    snap = repo.get_full_graph()
    found = next((e for e in snap["entities"] if str(e["id"]) == str(entity_id)), None)
    if not found:
        raise HTTPException(status_code=404, detail=f"Entity {entity_id} not found")
    history = repo.get_entity_history(entity_id)
    history_serialized = [
        {
            "id": str(h["id"]),
            "event_type": h["event_type"],
            "payload": dict(h.get("payload") or {}),
            "source_id": str(h["source_id"]) if h.get("source_id") else None,
            "source_excerpt": h.get("source_excerpt"),
            "occurred_at": h["occurred_at"].isoformat() if h.get("occurred_at") else None,
            "recorded_at": h["recorded_at"].isoformat() if h.get("recorded_at") else None,
        }
        for h in history
    ]
    return EntityDetail(
        id=UUID(str(found["id"])),
        type=found["type"],
        canonical_name=found["canonical_name"],
        aliases=list(found.get("aliases") or []),
        state=(found.get("current_state") or {}).get("state"),
        confidence=float(found.get("confidence") or 1.0),
        attributes=dict(found.get("attributes") or {}),
        current_state=dict(found.get("current_state") or {}),
        first_seen_at=found.get("first_seen_at"),
        last_updated_at=found.get("last_updated_at"),
        events=history_serialized,
    )


# =====================================================================
# Proposals
# =====================================================================
@app.get("/proposals", response_model=list[ProposalView])
def list_proposals(
    status_filter: str | None = None,
    repo: GraphRepository = Depends(repo_dep),
) -> list[ProposalView]:
    rows = repo.list_proposals(status=status_filter, limit=200)
    return [_proposal_row_to_view(r) for r in rows]


@app.patch("/proposals/{proposal_id}/status", response_model=ProposalView)
def update_proposal_status(
    proposal_id: UUID,
    body: ProposalStatusUpdate,
    repo: GraphRepository = Depends(repo_dep),
) -> ProposalView:
    repo._ensure_alive()
    with repo._conn.cursor() as cur:
        cur.execute(repo.SCHEMA_SETUP)
        cur.execute(
            """
            UPDATE hakutaku.proposals
               SET status = %s
             WHERE id = %s
             RETURNING id, proposal_type, title, description, justification,
                       priority, status, related_entities, created_at;
            """,
            (body.status, str(proposal_id)),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    repo._conn.commit()
    return _proposal_row_to_view(row)


def _proposal_row_to_view(row: dict[str, Any]) -> ProposalView:
    return ProposalView(
        id=UUID(str(row["id"])),
        proposal_type=row["proposal_type"],
        title=row["title"],
        description=row["description"],
        priority=int(row["priority"]),
        status=row.get("status", "open"),
        related_entities=[
            UUID(str(x)) for x in (row.get("related_entities") or [])
        ],
        justification=dict(row.get("justification") or {}),
        created_at=row["created_at"],
    )


# =====================================================================
# Pipeline triggers
# =====================================================================
@app.post(
    "/pipeline/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_201_CREATED,
)
def trigger_ingest(
    body: IngestRequest,
    repo: GraphRepository = Depends(repo_dep),
    llm: LLMClient = Depends(llm_dep),
) -> IngestResponse:
    """Ingere um documento sincronamente (extração + grafo). Pode levar 30-60s."""
    adapter = MeetingAdapter() if body.source_type == "meeting" else ChatAdapter()
    doc = adapter.parse(body.raw_content, hints={"title": body.title})

    repo.upsert_source(
        source_id=doc.source_id,
        source_type=doc.source_type,
        title=body.title,
        raw_content=doc.raw_content,
        metadata=doc.metadata,
        occurred_at=doc.occurred_at,
    )
    llm.set_source_context(doc.source_id)
    try:
        extraction = extract_from_document(doc, repository=repo, save=True)
        ingest_stats = ingest_extraction(
            extraction,
            repository=repo,
            llm=llm,
            source_occurred_at=doc.occurred_at,
        )
    finally:
        llm.set_source_context(None)

    return IngestResponse(
        source_id=doc.source_id,
        extraction={
            "entities": len(extraction.entities),
            "relations": len(extraction.relations),
            "model": extraction.model,
            "prompt_version": extraction.prompt_version,
            "call_metadata": extraction.call_metadata,
        },
        ingest=ingest_stats.as_dict(),
    )


@app.post("/pipeline/reason", response_model=ReasoningResponse)
def trigger_reasoning(
    repo: GraphRepository = Depends(repo_dep),
    llm: LLMClient = Depends(llm_dep),
) -> ReasoningResponse:
    """Roda 1 ciclo de detectores + gerador de propostas."""
    s = run_reasoning_cycle(repository=repo, llm=llm)
    return ReasoningResponse(
        findings_count=s.findings_count,
        findings_by_detector=s.findings_by_detector,
        proposals_generated=s.proposals_generated,
        proposals_persisted=s.proposals_persisted,
        proposals_by_type=s.proposals_by_type,
        summary=s.summary,
        snapshot_path=str(s.snapshot_path) if s.snapshot_path else None,
        cost_usd=float(s.call_metadata.get("cost_usd", 0.0)),
    )


@app.post("/pipeline/cross-link", response_model=CrossLinkResponse)
def trigger_cross_link(
    repo: GraphRepository = Depends(repo_dep),
    llm: LLMClient = Depends(llm_dep),
) -> CrossLinkResponse:
    """Roda 1 ciclo de cross-linking question → decision."""
    s = link_questions_to_decisions(repository=repo, llm=llm)
    return CrossLinkResponse(
        questions_considered=s.questions_considered,
        candidate_pairs=s.candidate_pairs,
        haiku_calls=s.haiku_calls,
        verdict_yes=s.verdict_yes,
        verdict_no=s.verdict_no,
        verdict_maybe=s.verdict_maybe,
        links_created=s.links_created,
    )
