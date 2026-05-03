"""Schemas Pydantic para Findings e Propostas (Fase 5).

Findings são sinais brutos detectados pelos módulos de raciocínio (orphan_tasks,
escalating_risks, etc.). O LLM consome um batch de findings e devolve um
`ProposalsBatch` — propostas tipadas com justificativa e referências às
entidades envolvidas.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProposalType(str, Enum):
    ALERT = "alert"           # avisar a liderança sobre algo que precisa atenção
    SUGGESTION = "suggestion" # melhoria de processo ou recomendação
    ACTION = "action"         # ação executável concreta


class ProposalStatus(str, Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"


class EntityRef(BaseModel):
    """Referência leve a uma entidade — usado em findings + proposals."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    type: str


class Finding(BaseModel):
    """Sinal detectado por um detector. Input do gerador de propostas."""

    model_config = ConfigDict(extra="forbid")

    detector: str = Field(description="Nome do detector que produziu o finding.")
    severity: int = Field(ge=1, le=5, description="1 (informativo) a 5 (urgente).")
    description: str = Field(min_length=1)
    related_entities: list[EntityRef] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class Proposal(BaseModel):
    """Proposta de ação gerada pelo LLM. Espelha hakutaku.proposals."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    proposal_type: ProposalType
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    priority: int = Field(ge=1, le=5)
    justification: dict[str, Any] = Field(
        description="Estrutura livre — ex.: {based_on_findings: [...], reasoning: '...'}"
    )
    related_entity_ids: list[UUID] = Field(
        default_factory=list,
        description="IDs de entidades citadas (copiar dos findings, não inventar).",
    )


class ProposalsBatch(BaseModel):
    """Output do gerador — batch de propostas para um ciclo de raciocínio."""

    model_config = ConfigDict(extra="forbid")

    proposals: list[Proposal] = Field(default_factory=list)
    summary: str = Field(
        default="",
        description="Comentário curto do raciocínio do gerador sobre o batch.",
    )
