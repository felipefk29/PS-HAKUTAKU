"""Modelo Pydantic para eventos do log temporal.

A tabela `events` é a fonte da verdade do histórico. Cada mudança no grafo
passa por aqui antes de ser projetada em `entities` ou `relations`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    ENTITY_CREATED = "entity_created"
    ATTRIBUTE_CHANGED = "attribute_changed"
    STATUS_CHANGED = "status_changed"
    RELATION_ADDED = "relation_added"
    RELATION_REMOVED = "relation_removed"
    ENTITY_MERGED = "entity_merged"


class Event(BaseModel):
    """Evento atômico de mudança no grafo.

    `payload` é JSONB tipado pelo `event_type`:

    - `entity_created`     → {type, canonical_name, attributes, confidence}
    - `attribute_changed`  → {attribute, old_value, new_value, reason}
    - `status_changed`     → {old_status, new_status, trigger}
    - `relation_added`     → {from_entity, to_entity, relation_type, attributes}
    - `relation_removed`   → {relation_id, reason}
    - `entity_merged`      → {merged_into, merged_from, similarity_score, decision_method}
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: UUID | None = None
    entity_id: UUID
    event_type: EventType
    payload: dict
    source_id: UUID | None = None
    source_excerpt: str | None = None
    occurred_at: datetime
    recorded_at: datetime | None = None
