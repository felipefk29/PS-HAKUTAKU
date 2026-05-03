"""Modelos Pydantic das arestas do grafo.

`ProposedRelation` é o que o extrator produz: arestas referenciadas por
nomes/aliases, ainda não resolvidos a UUIDs no grafo.
`Relation` é a versão pós-resolução — UUIDs reais, pronta para persistir.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RelationType(str, Enum):
    """Vocabulário oficial de tipos de relação. Ver docs/SPEC.md §3."""

    OWNS = "owns"
    ASSIGNED_TO = "assigned_to"
    DECIDED_BY = "decided_by"
    AFFECTS = "affects"
    MITIGATES = "mitigates"
    DEPENDS_ON = "depends_on"
    BLOCKS = "blocks"
    MENTIONS = "mentions"
    ANSWERS = "answers"
    ESCALATES_TO = "escalates_to"
    PARTICIPATES_IN = "participates_in"
    BELONGS_TO = "belongs_to"
    COMMITS_TO = "commits_to"
    EXHIBITS = "exhibits"


class ProposedRelation(BaseModel):
    """Aresta tal como sai do extrator — pontas são aliases textuais.

    A resolução de `from_alias`/`to_alias` para UUIDs reais é responsabilidade
    da Fase 3 (entity resolution). O extrator não tem acesso ao grafo atual,
    então só pode produzir arestas em termos do que está no documento.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    relation_type: RelationType
    from_alias: str = Field(min_length=1, description="Nome/alias da entidade de origem como aparece na fonte.")
    to_alias: str = Field(min_length=1, description="Nome/alias da entidade de destino como aparece na fonte.")
    from_type: str | None = Field(
        default=None,
        description="Tipo esperado da entidade de origem (ajuda blocking durante resolução).",
    )
    to_type: str | None = Field(
        default=None,
        description="Tipo esperado da entidade de destino.",
    )
    attributes: dict = Field(default_factory=dict)
    source_excerpt: str = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class Relation(BaseModel):
    """Aresta resolvida — pronta para persistir em `relations`."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: UUID | None = None
    relation_type: RelationType
    from_entity: UUID
    to_entity: UUID
    attributes: dict = Field(default_factory=dict)
    source_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
