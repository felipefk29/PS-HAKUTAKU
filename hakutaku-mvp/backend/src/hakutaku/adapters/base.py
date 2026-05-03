"""Contratos de adapter — cada tipo de fonte tem um adapter próprio que
produz um `NormalizedDocument` consumível pelo extrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


SourceType = Literal["meeting", "chat"]


class NormalizedDocument(BaseModel):
    """Documento normalizado pronto para ser entregue ao extrator.

    `raw_content` preserva o input original (auditoria); `normalized_content` é
    o que efetivamente vai para o LLM (whitespace limpo, headers removidos, etc.).
    `metadata` carrega tudo que o adapter conseguiu extrair sem LLM (participantes,
    quantidade de mensagens, duração) — útil para o prompt e para análise.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: UUID = Field(default_factory=uuid4)
    source_type: SourceType
    title: str
    raw_content: str
    normalized_content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None

    @property
    def occurred_at_str(self) -> str:
        """String legível para uso em prompts. Vazio quando ausente."""
        return self.occurred_at.isoformat() if self.occurred_at else "desconhecida"


class SourceAdapter(ABC):
    """Interface comum a todos os adapters."""

    source_type: SourceType

    @abstractmethod
    def parse(self, raw_text: str, *, hints: dict[str, Any] | None = None) -> NormalizedDocument:
        """Converte texto bruto em `NormalizedDocument`.

        Args:
            raw_text: conteúdo da fonte como veio do disco / API.
            hints: dicas opcionais (ex.: `title`) que o adapter pode honrar.
        """
