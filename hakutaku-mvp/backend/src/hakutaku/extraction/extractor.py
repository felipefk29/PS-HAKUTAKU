"""Pipeline de extração: `NormalizedDocument` → `ExtractionResult`.

Orquestra: carrega prompt YAML → formata → chama LLM com `instructor` →
embrulha em `ExtractionResult` com metadados → persiste em `data/extractions/`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hakutaku.adapters.base import NormalizedDocument
from hakutaku.config import get_settings
from hakutaku.llm.client import LLMClient, get_llm_client
from hakutaku.llm.prompts import load_prompt
from hakutaku.schemas.extraction import ExtractedContent, ExtractionResult


def extract_from_document(
    document: NormalizedDocument,
    *,
    llm: LLMClient | None = None,
    model: str | None = None,
    context_block: str = "",
    save: bool = True,
) -> ExtractionResult:
    """Extrai entidades e relações de um documento já normalizado.

    Args:
        document: documento normalizado (output de um adapter).
        llm: cliente LLM (default = singleton lazy).
        model: modelo Anthropic (default = `anthropic_model_heavy` da config).
        context_block: contexto retrieved do grafo (Fase 4). Vazio na Fase 2.
        save: se True, persiste o JSON em `data/extractions/`.
    """
    settings = get_settings()
    llm = llm or get_llm_client()
    model = model or settings.anthropic_model_heavy

    prompt = load_prompt("extraction")
    system, user = prompt.format(
        source_type=document.source_type,
        source_title=document.title,
        occurred_at=document.occurred_at_str,
        document_text=document.normalized_content,
        context_block=context_block,
    )

    content, call_meta = llm.extract_structured(
        system=system,
        user=user,
        response_model=ExtractedContent,
        model=model,
        stage="extraction",
    )

    result = ExtractionResult.from_content(
        content,
        source_id=document.source_id,
        source_title=document.title,
        model=model,
        prompt_version=prompt.version,
        call_metadata=call_meta,
    )

    if save:
        save_extraction(result)
    return result


def save_extraction(result: ExtractionResult) -> Path:
    """Persiste `ExtractionResult` como JSON em `data/extractions/`.

    Returns:
        Caminho absoluto do arquivo gravado.
    """
    out_dir = get_settings().extractions_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{result.source_id}_{ts}.json"
    path.write_text(
        result.model_dump_json(indent=2, exclude_none=False),
        encoding="utf-8",
    )
    return path
