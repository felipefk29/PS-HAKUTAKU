"""Cross-source linking: vincula `OpenQuestion` ⟶ `Decision` posteriores (Fase 4 / D013).

Mecanismo de aprendizado: o sistema percebe que uma decisão tomada em um documento
mais recente responde uma pergunta aberta de documento anterior — e materializa
essa ligação como aresta `answers` no grafo, mais transição de estado da pergunta
para `answered`.

Este é um dos quatro mecanismos de aprendizado descritos em SPEC §7. Diferente
da extração contextualizada (D012), que opera DURANTE a extração de cada doc,
o cross-linker é um passo de BACKFILL que roda DEPOIS de toda a ingestão. É
caro o suficiente (1 chamada Haiku por par candidato) para não rodar em todo
pipeline — fica atrás de flag em `run_full_pipeline.py` e roda sempre em
`demo_learning.py` no modo "com memória".

Funil:
1. Lista todas as `OpenQuestion` com state='open' E sem aresta `answers` apontando
   para elas (filtro SQL — questão respondida durante a extração não passa).
2. Para cada Q, busca top-K `Decision` com `first_seen_at >= Q.first_seen_at` e
   cosine similarity ≥ `min_cosine` (filtro SQL via pgvector).
3. Para cada par (Q, D) candidato, chama Haiku 4.5 com prompt em
   `prompts/answers_question.yaml`. Output: `verdict ∈ {yes, no, maybe}`,
   confidence, reason.
4. Em `verdict='yes'`: insere aresta `answers` (D ⟶ Q) e transita Q para
   `state='answered'` via `repository.transition_state` (gera `status_changed`
   event para auditoria).
5. Para uma única Q, para no primeiro `yes` (uma pergunta tem uma resposta
   canônica — o resto vira ruído).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hakutaku.config import get_settings
from hakutaku.graph.repository import EntityRecord, GraphRepository
from hakutaku.llm.client import LLMClient
from hakutaku.llm.prompts import load_prompt
from hakutaku.schemas import RelationType


DEFAULT_MIN_COSINE = 0.5
DEFAULT_TOP_K = 3


# =====================================================================
# Output schemas
# =====================================================================
class _AnswerVerdict(BaseModel):
    """Resposta do Haiku via instructor."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["yes", "no", "maybe"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)


@dataclass
class LinkedAnswer:
    """Resultado de um link Q→D bem-sucedido."""

    question_id: UUID
    question_name: str
    decision_id: UUID
    decision_name: str
    cosine_similarity: float
    verdict_confidence: float
    reason: str
    relation_id: UUID
    decision_first_seen: datetime


@dataclass
class CrossLinkerStats:
    """Métricas agregadas — usadas pelo demo e pela CLI."""

    questions_considered: int = 0
    candidate_pairs: int = 0
    haiku_calls: int = 0
    verdict_yes: int = 0
    verdict_no: int = 0
    verdict_maybe: int = 0
    links_created: int = 0
    questions_marked_answered: int = 0
    linked: list[LinkedAnswer] = field(default_factory=list)


# =====================================================================
# Linker
# =====================================================================
def link_questions_to_decisions(
    *,
    repository: GraphRepository,
    llm: LLMClient,
    min_cosine: float = DEFAULT_MIN_COSINE,
    top_k_per_question: int = DEFAULT_TOP_K,
) -> CrossLinkerStats:
    """Roda o cross-linker em todo o grafo. Idempotente: questões com `answers`
    pré-existente são puladas via filtro SQL.

    Args:
        repository: conexão com o grafo.
        llm: cliente para chamar Haiku.
        min_cosine: piso de cosine similarity para considerar candidato.
        top_k_per_question: quantos candidatos por pergunta vão para o Haiku.

    Returns:
        `CrossLinkerStats` com tudo que aconteceu — útil para o demo imprimir.
    """
    settings = get_settings()
    stats = CrossLinkerStats()

    questions = repository.list_open_questions_with_embeddings(
        exclude_already_answered=True
    )
    if not questions:
        return stats

    prompt = load_prompt("answers_question")
    haiku_model = settings.anthropic_model_light

    for q_record, q_embedding, q_first_seen in questions:
        stats.questions_considered += 1

        candidates = repository.find_decision_candidates_for_question(
            question_first_seen_at=q_first_seen,
            question_embedding=q_embedding,
            top_k=top_k_per_question,
            min_cosine=min_cosine,
        )
        stats.candidate_pairs += len(candidates)

        if not candidates:
            continue

        for decision_record, cosine_sim, _decision_first_seen in candidates:
            verdict = _ask_haiku(
                question=q_record,
                decision=decision_record,
                prompt=prompt,
                llm=llm,
                model=haiku_model,
                repository=repository,
            )
            stats.haiku_calls += 1

            if verdict.verdict == "yes":
                stats.verdict_yes += 1
                link = _persist_link(
                    repository=repository,
                    question=q_record,
                    decision=decision_record,
                    cosine_sim=cosine_sim,
                    verdict=verdict,
                )
                if link is not None:
                    stats.linked.append(link)
                    stats.links_created += 1
                    stats.questions_marked_answered += 1
                # Para a primeira "yes" — uma pergunta tem uma resposta.
                break
            elif verdict.verdict == "maybe":
                stats.verdict_maybe += 1
                # Não cria aresta — mas também não tenta outros (evitar
                # cascata de chamadas caras quando o sinal é fraco).
                continue
            else:
                stats.verdict_no += 1
                continue

    return stats


# =====================================================================
# Helpers
# =====================================================================
def _ask_haiku(
    *,
    question: EntityRecord,
    decision: EntityRecord,
    prompt,
    llm: LLMClient,
    model: str,
    repository: GraphRepository,
) -> _AnswerVerdict:
    # source_excerpt fica em events, não em attributes — buscar via repo.
    q_excerpt = (
        repository.get_entity_source_excerpt(question.id)
        or "(trecho não disponível)"
    )
    d_excerpt = (
        repository.get_entity_source_excerpt(decision.id)
        or "(trecho não disponível)"
    )
    rationale = (decision.attributes or {}).get("rationale") or "(sem rationale registrado)"

    system, user = prompt.format(
        question_text=question.canonical_name,
        question_first_seen="(data registrada no grafo)",
        question_excerpt=q_excerpt,
        decision_text=decision.canonical_name,
        decision_first_seen="(data registrada no grafo)",
        decision_rationale=rationale,
        decision_excerpt=d_excerpt,
    )

    verdict, _ = llm.extract_structured(
        system=system,
        user=user,
        response_model=_AnswerVerdict,
        model=model,
        stage="cross_link_answer",
        max_tokens=300,
        temperature=0.0,
        log_extras={
            "prompt_template_version": prompt.version,
            "question_id": str(question.id),
            "decision_id": str(decision.id),
        },
    )
    return verdict


def _persist_link(
    *,
    repository: GraphRepository,
    question: EntityRecord,
    decision: EntityRecord,
    cosine_sim: float,
    verdict: _AnswerVerdict,
) -> LinkedAnswer | None:
    """Insere aresta `answers` (Decision → OpenQuestion) e transita Q para 'answered'.

    Retorna None se a aresta já existia (idempotência via UNIQUE no schema).
    """
    occurred_at = datetime.now(timezone.utc)
    excerpt = (
        f"[cross_link verdict={verdict.verdict} conf={verdict.confidence:.2f}] "
        f"{verdict.reason}"
    )[:1000]

    rel_id = repository.insert_relation(
        from_entity=decision.id,
        to_entity=question.id,
        relation_type=RelationType.ANSWERS,
        attributes={
            "verdict_confidence": float(verdict.confidence),
            "cosine_similarity": float(cosine_sim),
            "reason": verdict.reason,
            "method": "cross_linker_haiku",
        },
        source_id=None,  # cross-source: nenhuma fonte única gerou esta aresta
        confidence=float(verdict.confidence),
        source_excerpt=excerpt,
        occurred_at=occurred_at,
    )
    if rel_id is None:
        return None

    repository.transition_state(
        entity_id=question.id,
        new_state="answered",
        trigger="cross_linker",
        source_id=None,
        source_excerpt=excerpt,
        occurred_at=occurred_at,
    )

    return LinkedAnswer(
        question_id=question.id,
        question_name=question.canonical_name,
        decision_id=decision.id,
        decision_name=decision.canonical_name,
        cosine_similarity=cosine_sim,
        verdict_confidence=float(verdict.confidence),
        reason=verdict.reason,
        relation_id=rel_id,
        decision_first_seen=occurred_at,
    )
