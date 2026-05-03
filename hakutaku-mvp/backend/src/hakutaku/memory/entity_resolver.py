"""Entity resolution híbrida — pg_trgm + pgvector + LLM (D006).

Decisão por estágio:
1. Embed do nome canônico + aliases + sinais de atributo (papel, time, severity).
2. Repository.find_similar_entities → top-K candidatos do mesmo type.
3. Pesos do combined_score variam por tipo:
   - Risk/Decision/Task/Project (canonical_name parafraseado pelo extrator):
     0.85 cosine + 0.15 trgm.
   - Person/Client (nome próprio mais estável): 0.6 cosine + 0.4 trgm.
4. Se candidato com combined ≥ threshold_high (0.92): `merge` automático.
5. Se melhor combined < threshold_low (0.55): `create` automático.
6. Zona cinza: chama Claude Haiku 4.5 para decidir.

Tipos sem identidade estável (`OpenQuestion`, `BehavioralPattern`, `Commitment`)
sempre criam — nunca passam pelo funil. Veja docs/SPEC.md §2 e D006.

Calibragem pós-Fase 3: thresholds e pesos foram revisados depois que dois
casos quebraram o auto-create — Beatriz (combined ~0.74 com 0.6/0.4) e
Risk de churn TechNova (combined ~0.47, paráfrase forte). Ver D009 §revisão 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hakutaku.config import get_settings
from hakutaku.graph.repository import EntityCandidate, GraphRepository
from hakutaku.llm.client import LLMClient
from hakutaku.llm.prompts import load_prompt
from hakutaku.schemas import Entity


DEFAULT_THRESHOLD_HIGH = 0.92
# Calibragem pós-Fase 3: Risk paráfrase ("Risco de churn TechNova por SLA não
# cumprido" vs "Churn da TechNova") combina cosine ~0.65 + trgm ~0.20 → 0.47
# com pesos default. Pra trazer esses casos pra zona cinza precisamos baixar
# o piso. Threshold 0.55 é empírico — ajustável depois com mais dados.
DEFAULT_THRESHOLD_LOW = 0.55
DEFAULT_TOP_K = 8

# Tipos que NUNCA fazem dedupe — toda menção é nó novo.
_BYPASS_TYPES = frozenset({"OpenQuestion", "BehavioralPattern", "Commitment"})

# Pesos do combined_score por tipo. Tipos com canonical_name parafraseado pelo
# extrator (Risk, Decision, Task, Project) priorizam o sinal semântico do
# embedding sobre o lexical. Tipos com nome estável (Person, Client) mantêm
# trgm relevante porque variações tendem a ser tipográficas / sufixos curtos.
_PARAPHRASEABLE_TYPES = frozenset({"Risk", "Decision", "Task", "Project"})


def _weights_for(entity_type: str) -> tuple[float, float]:
    """Retorna `(cosine_weight, trgm_weight)` para o tipo dado."""
    if entity_type in _PARAPHRASEABLE_TYPES:
        return 0.85, 0.15
    return 0.6, 0.4


# =====================================================================
# Output schemas
# =====================================================================
class _LLMDecision(BaseModel):
    """Schema interno para a chamada Haiku via instructor."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["merge", "create"]
    target_id: UUID | None = Field(
        default=None, description="UUID do candidato escolhido. null se action=create."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=500)


@dataclass
class ResolutionDecision:
    """Decisão final do resolver, independente de quem decidiu (auto vs LLM)."""

    action: Literal["merge", "create"]
    target_id: UUID | None
    confidence: float
    reasoning: str
    # Auditoria do método de decisão e scores envolvidos.
    decision_method: Literal["bypass", "auto_high", "auto_low", "llm"]
    similarity_score: float | None = None
    candidates_considered: int = 0


@dataclass
class ResolverStats:
    """Acumulador de estatísticas — útil para o pipeline reportar resumo."""

    total: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    by_action: dict[str, int] = field(default_factory=dict)

    def record(self, decision: ResolutionDecision) -> None:
        self.total += 1
        self.by_method[decision.decision_method] = (
            self.by_method.get(decision.decision_method, 0) + 1
        )
        self.by_action[decision.action] = self.by_action.get(decision.action, 0) + 1


# =====================================================================
# Embedding helper
# =====================================================================
def _build_embedding_text(entity: Entity) -> str:
    """Texto que vai ao embedding — nome + aliases + sinais de atributo.

    Mantemos curto para evitar diluir o sinal do nome próprio. Inclui apenas
    campos que ajudam a desambiguar.
    """
    parts: list[str] = [entity.canonical_name]
    if entity.aliases:
        parts.append("aliases: " + ", ".join(entity.aliases[:5]))

    dump = entity.model_dump(mode="json", exclude_none=True)
    for key in ("role", "team", "client_type", "tier", "severity", "pattern_kind"):
        if key in dump and dump[key]:
            parts.append(f"{key}: {dump[key]}")

    return " | ".join(parts)


def _format_candidates_for_prompt(
    candidates: list[EntityCandidate],
    *,
    cosine_weight: float,
    trgm_weight: float,
) -> str:
    """Bloco textual dos candidatos para o prompt do LLM."""
    if not candidates:
        return "(nenhum)"
    lines = []
    for i, c in enumerate(candidates, start=1):
        combined = c.combined(cosine_weight=cosine_weight, trgm_weight=trgm_weight)
        lines.append(
            f"{i}. id={c.record.id} | name=\"{c.record.canonical_name}\" "
            f"| aliases={c.record.aliases or []} "
            f"| attributes={c.record.attributes} "
            f"| current_state={c.record.current_state} "
            f"| trgm={c.trgm_score:.3f} cosine={c.cosine_score:.3f} "
            f"combined={combined:.3f}"
        )
    return "\n".join(lines)


# =====================================================================
# Resolver
# =====================================================================
def resolve_entity(
    entity: Entity,
    *,
    repository: GraphRepository,
    llm: LLMClient,
    source_title: str,
    occurred_at: datetime,
    threshold_high: float = DEFAULT_THRESHOLD_HIGH,
    threshold_low: float = DEFAULT_THRESHOLD_LOW,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[ResolutionDecision, list[float]]:
    """Resolve uma entidade extraída contra o grafo atual.

    Returns:
        Tupla `(decision, embedding)`. O embedding é devolvido junto para que
        o ingester possa persistir entidades novas sem recomputar.
    """
    # Etapa 0: embedding (sempre necessário, seja para insert ou para matching).
    embed_text = _build_embedding_text(entity)
    embedding, _ = llm.embed(embed_text, stage="entity_resolution_embed")

    # Etapa 1: bypass para tipos sem identidade estável.
    if entity.type in _BYPASS_TYPES:
        return (
            ResolutionDecision(
                action="create",
                target_id=None,
                confidence=1.0,
                reasoning=f"Tipo {entity.type} bypassa entity resolution por convenção.",
                decision_method="bypass",
                similarity_score=None,
                candidates_considered=0,
            ),
            embedding,
        )

    # Pesos do combined_score dependem do tipo (paráfrase amigável → cosine pesa mais).
    cw, tw = _weights_for(entity.type)

    # Etapa 2: busca de candidatos.
    candidates = repository.find_similar_entities(
        name=entity.canonical_name,
        entity_type=entity.type,
        embedding=embedding,
        top_k=top_k,
        cosine_weight=cw,
        trgm_weight=tw,
    )

    if not candidates:
        return (
            ResolutionDecision(
                action="create",
                target_id=None,
                confidence=1.0,
                reasoning="Nenhum candidato no grafo.",
                decision_method="auto_low",
                similarity_score=0.0,
                candidates_considered=0,
            ),
            embedding,
        )

    best = candidates[0]
    best_score = best.combined(cosine_weight=cw, trgm_weight=tw)

    # Etapa 3: auto-merge em score alto.
    if best_score >= threshold_high:
        return (
            ResolutionDecision(
                action="merge",
                target_id=best.record.id,
                confidence=min(1.0, best_score),
                reasoning=(
                    f"Auto-merge: combined={best_score:.3f} ≥ {threshold_high} "
                    f"contra '{best.record.canonical_name}'."
                ),
                decision_method="auto_high",
                similarity_score=best_score,
                candidates_considered=len(candidates),
            ),
            embedding,
        )

    # Etapa 4: auto-create em score baixo.
    if best_score < threshold_low:
        return (
            ResolutionDecision(
                action="create",
                target_id=None,
                confidence=1.0 - best_score,
                reasoning=(
                    f"Score abaixo do threshold inferior ({best_score:.3f} < {threshold_low}). "
                    "Cria nó novo."
                ),
                decision_method="auto_low",
                similarity_score=best_score,
                candidates_considered=len(candidates),
            ),
            embedding,
        )

    # Etapa 5: zona cinza — LLM decide.
    settings = get_settings()
    prompt = load_prompt("entity_resolution")
    attributes_dump = entity.model_dump(mode="json", exclude_none=True)
    # Limpa campos meta para deixar o prompt mais legível.
    for k in ("id", "canonical_name", "aliases", "source_excerpt", "confidence", "type"):
        attributes_dump.pop(k, None)

    system, user = prompt.format(
        entity_type=entity.type,
        canonical_name=entity.canonical_name,
        aliases=entity.aliases or [],
        attributes=attributes_dump,
        source_excerpt=entity.source_excerpt,
        source_title=source_title,
        occurred_at=occurred_at.isoformat(),
        candidates_block=_format_candidates_for_prompt(
            candidates, cosine_weight=cw, trgm_weight=tw
        ),
    )

    decision_model, _ = llm.extract_structured(
        system=system,
        user=user,
        response_model=_LLMDecision,
        model=settings.anthropic_model_light,
        stage="entity_resolution",
        max_tokens=512,
        temperature=0.0,
    )

    target_id = decision_model.target_id
    # Sanidade: target_id precisa estar entre os candidatos. Caso contrário
    # caímos para create — modelo pode ter alucinado um UUID.
    if decision_model.action == "merge":
        valid_ids = {c.record.id for c in candidates}
        if target_id not in valid_ids:
            return (
                ResolutionDecision(
                    action="create",
                    target_id=None,
                    confidence=0.5,
                    reasoning=(
                        f"LLM sugeriu merge com id fora dos candidatos ({target_id}); "
                        "fallback para create."
                    ),
                    decision_method="llm",
                    similarity_score=best_score,
                    candidates_considered=len(candidates),
                ),
                embedding,
            )

    return (
        ResolutionDecision(
            action=decision_model.action,
            target_id=target_id if decision_model.action == "merge" else None,
            confidence=decision_model.confidence,
            reasoning=decision_model.reasoning,
            decision_method="llm",
            similarity_score=best_score,
            candidates_considered=len(candidates),
        ),
        embedding,
    )
