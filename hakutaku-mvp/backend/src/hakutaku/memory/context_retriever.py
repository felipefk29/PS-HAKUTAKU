"""Construção do bloco de contexto organizacional para extração informada (Fase 4).

Antes de chamar o LLM para extrair de um documento novo, montamos um resumo
do grafo acumulado relevante para esse documento. Isso permite que o extrator:

1. Reusar `canonical_name` de entidades existentes em vez de criar duplicatas.
2. Detectar atualizações de estado em entidades já no grafo (Risk escala,
   Question é respondida, Task termina) em vez de gerar Risk/Question/Task
   novos.
3. Vincular menções no documento corrente a entidades de fontes anteriores
   — isso é a base do `cross-source linking` (D013).

Estratégia:
- Embed do conteúdo normalizado do documento (truncado em 6000 chars para caber
  num único request de embedding sem perder muita semântica).
- 5 buckets de retrieval no repositório:
    * top-K entidades cross-type por cosine sim ao embedding do doc;
    * Top-N entidades atualizadas recentemente (sinal "o que está vivo");
    * OpenQuestions ainda abertas (precisam ser ancoradas/respondidas);
    * Riscos com severidade alta/crítica em estado aberto;
    * Projetos ativos (escopo onde a maioria dos itens vai cair).
- Render textual estruturado em PT-BR com seções por tipo, terminando com a
  instrução de uso "APENAS para desambiguação" — protege contra o LLM copiar
  atributos do contexto para entidades novas.

Quando o grafo está vazio (primeiro documento), retornamos `""` e o extrator
opera no modo Fase 2 — sem perder funcionalidade.
"""

from __future__ import annotations

from typing import Any

from hakutaku.adapters.base import NormalizedDocument
from hakutaku.graph.repository import EntityRecord, GraphRepository
from hakutaku.llm.client import LLMClient


# Não mostramos BehavioralPattern para o extrator — é entidade de raciocínio
# interno do sistema (ver SPEC §2.10), e copiá-la para o contexto convida o
# LLM a tentar extraí-la, o que é proibido pelo prompt.
_HIDDEN_TYPES_FROM_CONTEXT = ["BehavioralPattern"]

# Truncamento do texto antes do embedding. text-embedding-3-small aceita
# até 8192 tokens; 6000 chars (~1500 tokens em PT) é folgado e não dilui o sinal.
_DOC_EMBED_MAX_CHARS = 6000


def _bullet_person(rec: EntityRecord) -> str:
    role = (rec.attributes or {}).get("role")
    team = (rec.attributes or {}).get("team")
    descr = ", ".join([x for x in (role, team) if x])
    aliases = ", ".join(rec.aliases[:3]) if rec.aliases else ""
    parts = [f"- **{rec.canonical_name}**"]
    if descr:
        parts.append(f"({descr})")
    if aliases:
        parts.append(f"[aliases: {aliases}]")
    return " ".join(parts)


def _bullet_project(rec: EntityRecord) -> str:
    state = (rec.current_state or {}).get("state", "active")
    desc = (rec.attributes or {}).get("description")
    suffix = f" — {desc}" if desc else ""
    return f"- **{rec.canonical_name}** (state={state}){suffix}"


def _bullet_client(rec: EntityRecord) -> str:
    aliases = ", ".join(rec.aliases[:3]) if rec.aliases else ""
    suffix = f" [aliases: {aliases}]" if aliases else ""
    return f"- **{rec.canonical_name}**{suffix}"


def _bullet_risk(rec: EntityRecord) -> str:
    sev = (rec.attributes or {}).get("severity", "medium")
    state = (rec.current_state or {}).get("state", "identified")
    return f"- **{rec.canonical_name}** — severidade {sev} — state={state}"


def _bullet_question(rec: EntityRecord) -> str:
    state = (rec.current_state or {}).get("state", "open")
    return f"- {rec.canonical_name} (state={state})"


def _bullet_task(rec: EntityRecord) -> str:
    state = (rec.current_state or {}).get("state", "proposed")
    deadline = (rec.attributes or {}).get("deadline")
    suffix = f" — deadline {deadline}" if deadline else ""
    return f"- **{rec.canonical_name}** (state={state}){suffix}"


def _bullet_decision(rec: EntityRecord) -> str:
    state = (rec.current_state or {}).get("state", "confirmed")
    rationale = (rec.attributes or {}).get("rationale") or ""
    suffix = f" — {rationale[:120]}" if rationale else ""
    return f"- **{rec.canonical_name}** (state={state}){suffix}"


def _bullet_default(rec: EntityRecord) -> str:
    return f"- **{rec.canonical_name}** ({rec.type})"


_BULLETS = {
    "Person": _bullet_person,
    "Project": _bullet_project,
    "Client": _bullet_client,
    "Risk": _bullet_risk,
    "OpenQuestion": _bullet_question,
    "Task": _bullet_task,
    "Decision": _bullet_decision,
    "Commitment": _bullet_default,
    "Dependency": _bullet_default,
}


# Limites por bucket no bloco final — evitam contexto inflado em grafos grandes.
_PER_TYPE_LIMITS = {
    "Person": 8,
    "Project": 5,
    "Client": 5,
    "Risk": 5,
    "OpenQuestion": 8,
    "Task": 6,
    "Decision": 5,
    "Commitment": 4,
    "Dependency": 4,
}


def build_context_block(
    document: NormalizedDocument,
    *,
    repository: GraphRepository,
    llm: LLMClient,
    max_relevant_entities: int = 15,
    max_recent: int = 5,
    max_questions: int = 8,
    max_risks: int = 5,
    max_projects: int = 5,
) -> tuple[str, dict[str, Any]]:
    """Monta o `context_block` de extração informada para `document`.

    Args:
        document: documento já normalizado (output do adapter).
        repository: conexão com o grafo persistido.
        llm: cliente LLM para gerar o embedding do documento.
        max_relevant_entities: top-K cross-type por cosine ao embedding do doc.
        max_recent / max_questions / max_risks / max_projects: limites por bucket.

    Returns:
        Tupla `(text, metadata)`. `text == ""` quando o grafo está vazio (primeiro
        documento) ou nada relevante foi encontrado — extrator deve operar como
        Fase 2 nesse caso. `metadata` carrega contadores para auditoria/log.
    """
    # 1. Embed do documento (truncado).
    text_for_embed = document.normalized_content[:_DOC_EMBED_MAX_CHARS]
    if not text_for_embed.strip():
        return "", {"empty": True, "reason": "document_empty"}

    embedding, _ = llm.embed(text_for_embed, stage="context_retrieval_embed")

    # 2. Queries paralelas no grafo.
    relevant = repository.find_entities_by_doc_embedding(
        embedding=embedding,
        top_k=max_relevant_entities,
        exclude_types=_HIDDEN_TYPES_FROM_CONTEXT,
    )
    recent = repository.recent_active_entities(limit=max_recent)
    questions = repository.find_open_questions(limit=max_questions)
    risks = repository.find_open_risks(limit=max_risks)
    projects = repository.find_active_projects(limit=max_projects)

    # 3. Coleta cross-bucket por id, deduplicando.
    by_id: dict[str, EntityRecord] = {}
    for rec, _ in relevant:
        if rec.type in _HIDDEN_TYPES_FROM_CONTEXT:
            continue
        by_id[str(rec.id)] = rec
    for rec in recent:
        if rec.type in _HIDDEN_TYPES_FROM_CONTEXT:
            continue
        by_id.setdefault(str(rec.id), rec)
    for rec in questions:
        by_id.setdefault(str(rec.id), rec)
    for rec in risks:
        by_id.setdefault(str(rec.id), rec)
    for rec in projects:
        by_id.setdefault(str(rec.id), rec)

    if not by_id:
        return "", {"empty": True, "reason": "graph_empty_or_no_match"}

    # 4. Agrupa por tipo respeitando limites.
    by_type: dict[str, list[EntityRecord]] = {}
    for rec in by_id.values():
        by_type.setdefault(rec.type, []).append(rec)

    # 5. Render. Ordem de seções é deliberada: pessoas primeiro (gancho de
    # ambiguidade mais comum), depois clientes/projetos (contexto), depois
    # tasks/decisões/riscos/perguntas (estado atual do trabalho).
    sections: list[tuple[str, str]] = [
        ("Person", "Pessoas conhecidas"),
        ("Client", "Clientes/parceiros"),
        ("Project", "Projetos ativos"),
        ("Task", "Tasks ativas"),
        ("Decision", "Decisões recentes"),
        ("Risk", "Riscos abertos (severidade alta/crítica)"),
        ("OpenQuestion", "Perguntas ainda sem resposta"),
        ("Commitment", "Commitments em aberto"),
        ("Dependency", "Dependências ativas"),
    ]

    lines: list[str] = ["", "## Contexto organizacional acumulado", ""]
    rendered_count = 0
    for type_key, heading in sections:
        items = by_type.get(type_key, [])
        if not items:
            continue
        limit = _PER_TYPE_LIMITS.get(type_key, 5)
        items = items[:limit]
        bullet_fn = _BULLETS.get(type_key, _bullet_default)
        lines.append(f"**{heading}:**")
        for rec in items:
            lines.append(bullet_fn(rec))
            rendered_count += 1
        lines.append("")

    if rendered_count == 0:
        return "", {"empty": True, "reason": "no_renderable_entities"}

    # 6. Instrução de uso — proteção contra alucinação de atributos do contexto.
    lines.extend(
        [
            "**Como usar este contexto:**",
            "Use o contexto acima APENAS para desambiguar. Se uma menção no documento "
            "parece se referir a uma entidade listada acima, prefira reusar o "
            "`canonical_name` dela (e adicione a forma vista no documento como alias). "
            "NÃO copie atributos do contexto para entidades novas — extraia atributos "
            "SOMENTE do texto do documento atual. Se o documento atualiza o estado de "
            "uma entidade conhecida (ex.: severity escala, task vai para 'done'), "
            "extraia a entidade com o estado novo — o sistema detecta como mudança.",
            "",
        ]
    )

    text = "\n".join(lines)
    metadata: dict[str, Any] = {
        "context_entities_count": rendered_count,
        "context_relevant_count": len(relevant),
        "context_questions_count": len(questions),
        "context_risks_count": len(risks),
        "context_projects_count": len(projects),
        "context_chars": len(text),
        "empty": False,
    }
    return text, metadata
