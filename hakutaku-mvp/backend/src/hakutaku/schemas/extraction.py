"""Containers do output de extração de um documento.

Dois schemas:

- `ExtractedContent` — o que o LLM produz (entidades + relações + notes).
  É o `response_model` passado ao instructor. NÃO contém metadados (source_id,
  modelo, etc.) porque essa informação é da aplicação, não do LLM.

- `ExtractionResult` — wrapper de persistência: `ExtractedContent` + metadados.
  É o que vai serializado em `data/extractions/{source_id}_{ts}.json`.

A resolução de aliases textuais para UUIDs do grafo acontece em fase posterior
(entity resolution); aqui ainda referenciamos por nome/alias.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hakutaku.schemas.entities import Entity
from hakutaku.schemas.relations import ProposedRelation


class ExtractedContent(BaseModel):
    """Schema entregue ao LLM via `instructor` — só conteúdo extraído.

    Mantemos separado de `ExtractionResult` para que o LLM nunca tente
    "adivinhar" metadados como source_id ou modelo. Esses são adicionados
    pelo extractor depois.
    """

    model_config = ConfigDict(extra="forbid")

    entities: list[Entity] = Field(default_factory=list)
    relations: list[ProposedRelation] = Field(default_factory=list)
    notes: str | None = Field(
        default=None,
        description="Ambiguidades importantes ou observações sobre o documento. Vazio é OK.",
    )


class ExtractionResult(BaseModel):
    """Wrapper completo persistido para auditoria e ingestão downstream."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    source_title: str
    extracted_at: datetime
    model: str = Field(description="Modelo LLM que produziu esta extração.")
    prompt_version: str = Field(description="Versão do prompt YAML usada.")

    entities: list[Entity] = Field(default_factory=list)
    relations: list[ProposedRelation] = Field(default_factory=list)
    notes: str | None = None

    # Metadados adicionais opcionais para inspeção (custo, tokens, latência).
    call_metadata: dict = Field(default_factory=dict)

    @classmethod
    def from_content(
        cls,
        content: ExtractedContent,
        *,
        source_id: UUID,
        source_title: str,
        model: str,
        prompt_version: str,
        call_metadata: dict | None = None,
    ) -> ExtractionResult:
        return cls(
            source_id=source_id,
            source_title=source_title,
            extracted_at=datetime.now(timezone.utc),
            model=model,
            prompt_version=prompt_version,
            entities=content.entities,
            relations=content.relations,
            notes=content.notes,
            call_metadata=call_metadata or {},
        )
