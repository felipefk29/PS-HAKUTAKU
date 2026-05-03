"""Pipeline de extração: `NormalizedDocument` → `ExtractionResult`.

Orquestra: (opcionalmente) monta context block via retrieval do grafo →
carrega prompt YAML → formata → chama LLM com `instructor` → embrulha em
`ExtractionResult` com metadados → persiste em `data/extractions/`.

Modo "sem memória" (Fase 2): chame com `repository=None` (ou apenas omita).
Modo "com memória" (Fase 4): passe `repository` — o extrator consulta o grafo,
monta o `context_block` e injeta no prompt automaticamente. Para forçar um
context_block customizado (ex.: testes), passe `context_block` explicitamente.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hakutaku.adapters.base import NormalizedDocument
from hakutaku.config import get_settings
from hakutaku.graph.repository import GraphRepository
from hakutaku.llm.client import LLMClient, get_llm_client
from hakutaku.llm.prompts import load_prompt
from hakutaku.memory.context_retriever import build_context_block
from hakutaku.schemas.extraction import ExtractedContent, ExtractionResult


def extract_from_document(
    document: NormalizedDocument,
    *,
    llm: LLMClient | None = None,
    repository: GraphRepository | None = None,
    model: str | None = None,
    context_block: str | None = None,
    save: bool = True,
) -> ExtractionResult:
    """Extrai entidades e relações de um documento já normalizado.

    Args:
        document: documento normalizado (output de um adapter).
        llm: cliente LLM (default = singleton lazy).
        repository: se fornecido E `context_block` é None, monta context block
            automaticamente consultando o grafo. Se ambos forem None, opera no
            modo Fase 2 (sem contexto).
        model: modelo Anthropic (default = `anthropic_model_heavy` da config).
        context_block: contexto retrieved do grafo. None → auto-build se
            `repository` foi passado, senão "". String vazia → modo sem memória
            explícito (uso em demo_learning para o modo A).
        save: se True, persiste o JSON em `data/extractions/`.
    """
    settings = get_settings()
    llm = llm or get_llm_client()
    model = model or settings.anthropic_model_heavy

    # Resolve context_block: explicit > auto via repository > vazio.
    context_metadata: dict[str, Any] = {"empty": True, "reason": "no_context_provided"}
    if context_block is None:
        if repository is not None:
            context_block, context_metadata = build_context_block(
                document, repository=repository, llm=llm
            )
        else:
            context_block = ""
    elif context_block == "":
        # String vazia explícita = modo sem memória (uso em demo Mode A).
        context_metadata = {"empty": True, "reason": "explicit_empty"}
    else:
        # Caller passou texto pronto.
        context_metadata = {
            "empty": False,
            "reason": "caller_provided",
            "context_chars": len(context_block),
        }

    prompt = load_prompt("extraction")
    system, user = prompt.format(
        source_type=document.source_type,
        source_title=document.title,
        occurred_at=document.occurred_at_str,
        document_text=document.normalized_content,
        context_block=context_block,
    )

    # Extras viram campos top-level no JSON de log e populam
    # `prompt_template_version` na tabela `hakutaku.llm_calls` via DB sink.
    log_extras: dict[str, Any] = {
        "prompt_template_version": prompt.version,
        "context_block_excerpt": (context_block[:500] if context_block else ""),
        "context_entities_count": context_metadata.get("context_entities_count", 0),
        "context_chars": context_metadata.get("context_chars", 0),
        "context_empty": context_metadata.get("empty", True),
    }

    content, call_meta = llm.extract_structured(
        system=system,
        user=user,
        response_model=ExtractedContent,
        model=model,
        stage="extraction",
        log_extras=log_extras,
    )

    # Anexa contexto ao call_metadata persistido em ExtractionResult — útil
    # para auditoria do JSON em data/extractions/.
    full_meta = {**call_meta, **context_metadata}

    result = ExtractionResult.from_content(
        content,
        source_id=document.source_id,
        source_title=document.title,
        model=model,
        prompt_version=prompt.version,
        call_metadata=full_meta,
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
