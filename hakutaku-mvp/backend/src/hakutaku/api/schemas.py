"""Pydantic schemas das responses da API.

Mantemos separados dos schemas de domínio (`hakutaku.schemas`) por dois motivos:
(a) view models podem precisar achatar/transformar campos sem poluir a ontologia,
(b) versionamento da API pode evoluir independente da ontologia interna.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------
# Entities & graph
# ---------------------------------------------------------------------
class EntitySummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    type: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    state: str | None = None
    confidence: float = 1.0


class EntityDetail(EntitySummary):
    attributes: dict[str, Any] = Field(default_factory=dict)
    current_state: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: datetime | None = None
    last_updated_at: datetime | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class RelationSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    from_entity: UUID
    to_entity: UUID
    relation_type: str
    confidence: float = 1.0
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphSnapshot(BaseModel):
    generated_at: datetime
    entities: list[EntitySummary]
    relations: list[RelationSummary]


# ---------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------
class ProposalView(BaseModel):
    id: UUID
    proposal_type: Literal["alert", "suggestion", "action"]
    title: str
    description: str
    priority: int = Field(ge=1, le=5)
    status: Literal["open", "accepted", "dismissed", "resolved"]
    related_entities: list[UUID] = Field(default_factory=list)
    justification: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ProposalStatusUpdate(BaseModel):
    status: Literal["open", "accepted", "dismissed", "resolved"]


# ---------------------------------------------------------------------
# Pipeline triggers
# ---------------------------------------------------------------------
class IngestRequest(BaseModel):
    source_type: Literal["meeting", "chat"]
    title: str = Field(min_length=1, max_length=300)
    raw_content: str = Field(min_length=1)


class IngestResponse(BaseModel):
    source_id: UUID
    extraction: dict[str, Any]
    ingest: dict[str, Any]


class ReasoningResponse(BaseModel):
    findings_count: int
    findings_by_detector: dict[str, int]
    proposals_generated: int
    proposals_persisted: int
    proposals_by_type: dict[str, int]
    summary: str
    snapshot_path: str | None
    cost_usd: float


class CrossLinkResponse(BaseModel):
    questions_considered: int
    candidate_pairs: int
    haiku_calls: int
    verdict_yes: int
    verdict_no: int
    verdict_maybe: int
    links_created: int


class StatsResponse(BaseModel):
    entities: int
    relations: int
    events: int
