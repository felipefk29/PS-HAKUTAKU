"""Orquestrador de ingestão: ExtractionResult → Supabase + snapshots.

Pré-condição: a row em `hakutaku.sources` correspondente a `extraction.source_id`
já deve existir. O caller (run_full_pipeline) é responsável — precisa criar
o source ANTES de qualquer chamada LLM, porque `hakutaku.llm_calls.source_id`
tem FK para `sources.id` e o `LLMClient.db_sink` dispara INSERTs durante a
extração.

Fluxo:
1. Para cada entidade extraída:
   - Resolver decide create vs merge (com embedding e candidatos).
   - Persiste insert/update e mantém mapa `alias→entity_id`.
2. Para cada relação extraída:
   - Mapeia `from_alias`/`to_alias` para uuids reais usando o mapa local.
   - Insere a aresta (idempotente via UNIQUE).
3. Salva snapshot JSON e HTML (Pyvis) em `data/graph_snapshots/`.

Mantemos um mapa local por documento: aliases textuais não são únicos no grafo,
mas dentro do mesmo documento tendem a ser. Quando há conflito (ex.: dois
"Pedro" no mesmo doc) usamos o último resolvido — o extrator deveria ter
deduplicado antes; se não fez, registramos em `notes`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from hakutaku.config import get_settings
from hakutaku.graph.repository import GraphRepository
from hakutaku.llm.client import LLMClient
from hakutaku.memory.entity_resolver import (
    ResolutionDecision,
    ResolverStats,
    resolve_entity,
)
from hakutaku.schemas import (
    Entity,
    EventType,
    ExtractionResult,
    ProposedRelation,
    RelationType,
)


# =====================================================================
# Stats
# =====================================================================
@dataclass
class IngestStats:
    """Resumo do que aconteceu na ingestão de um documento."""

    source_id: UUID
    source_title: str
    entities_total: int = 0
    entities_created: int = 0
    entities_merged: int = 0
    relations_total: int = 0
    relations_created: int = 0
    relations_skipped: int = 0
    resolver_stats: ResolverStats = field(default_factory=ResolverStats)
    snapshot_json_path: Path | None = None
    snapshot_html_path: Path | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": str(self.source_id),
            "source_title": self.source_title,
            "entities_total": self.entities_total,
            "entities_created": self.entities_created,
            "entities_merged": self.entities_merged,
            "relations_total": self.relations_total,
            "relations_created": self.relations_created,
            "relations_skipped": self.relations_skipped,
            "resolver_methods": dict(self.resolver_stats.by_method),
            "resolver_actions": dict(self.resolver_stats.by_action),
            "snapshot_json": str(self.snapshot_json_path) if self.snapshot_json_path else None,
            "snapshot_html": str(self.snapshot_html_path) if self.snapshot_html_path else None,
            "notes": self.notes,
        }


# =====================================================================
# Helpers
# =====================================================================
def _alias_keys(entity: Entity) -> list[str]:
    """Chaves usadas para resolver `from_alias`/`to_alias` em relações.

    Inclui canonical_name + aliases. Tudo lowercase para tolerar inconsistência.
    """
    keys = [entity.canonical_name]
    keys.extend(entity.aliases or [])
    return [k.strip().lower() for k in keys if k]


def _to_attributes_for_update(entity: Entity) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Retorna (attributes, current_state, novos_aliases) prontos para update.

    Reaproveita a mesma separação do repository; dupla aqui porque o ingester
    precisa do payload antes de chamar update_entity.
    """
    dump = entity.model_dump(mode="json", exclude_none=False)
    meta_keys = {"id", "canonical_name", "aliases", "source_excerpt", "confidence", "type"}
    attributes: dict[str, Any] = {}
    current_state: dict[str, Any] = {}
    for k, v in dump.items():
        if k in meta_keys:
            continue
        if k == "state":
            current_state["state"] = v
        else:
            # Não sobrescrevemos atributo existente com None — deixa antigos quietos.
            if v is not None:
                attributes[k] = v
    return attributes, current_state, list(entity.aliases or [])


# =====================================================================
# Snapshot writers
# =====================================================================
def _write_json_snapshot(snapshot: dict[str, Any], out_dir: Path, ts: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ts}.json"
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


_TYPE_COLORS = {
    "Person": "#4f46e5",
    "Project": "#0ea5e9",
    "Client": "#f97316",
    "Task": "#10b981",
    "Decision": "#8b5cf6",
    "Risk": "#ef4444",
    "OpenQuestion": "#eab308",
    "Dependency": "#64748b",
    "Commitment": "#ec4899",
    "BehavioralPattern": "#7c3aed",
}


def _write_html_snapshot(snapshot: dict[str, Any], out_dir: Path, ts: str) -> Path:
    """Renderiza grafo com Pyvis. Falha graciosamente se Pyvis não estiver instalado.

    Retorna o path mesmo no fallback (que é um HTML simples com lista de nós/arestas).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ts}.html"

    try:
        from pyvis.network import Network  # type: ignore
    except ImportError:
        path.write_text(_render_fallback_html(snapshot), encoding="utf-8")
        return path

    net = Network(
        height="800px",
        width="100%",
        directed=True,
        bgcolor="#0b1220",
        font_color="#e2e8f0",
        notebook=False,
        cdn_resources="remote",
    )
    net.barnes_hut(spring_length=180)

    for e in snapshot["entities"]:
        title_lines = [
            f"<b>{e['type']}</b>: {e['canonical_name']}",
            f"id: {e['id']}",
            f"aliases: {', '.join(e['aliases']) or '—'}",
            f"state: {json.dumps(e['current_state'], ensure_ascii=False)}",
            f"attributes: {json.dumps(e['attributes'], ensure_ascii=False)[:300]}",
        ]
        net.add_node(
            e["id"],
            label=f"{e['canonical_name']}\n[{e['type']}]",
            title="<br/>".join(title_lines),
            color=_TYPE_COLORS.get(e["type"], "#94a3b8"),
            shape="dot",
            size=18,
        )

    for r in snapshot["relations"]:
        net.add_edge(
            r["from_entity"],
            r["to_entity"],
            label=r["relation_type"],
            title=f"confidence: {r['confidence']:.2f}",
            color="#94a3b8",
            arrows="to",
        )

    # Pyvis tenta abrir o template; gravamos diretamente em string.
    html_content = net.generate_html(notebook=False)
    path.write_text(html_content, encoding="utf-8")
    return path


def _render_fallback_html(snapshot: dict[str, Any]) -> str:
    """HTML estático mínimo se Pyvis não estiver disponível."""
    rows_e = "".join(
        f"<tr><td>{e['type']}</td><td>{e['canonical_name']}</td>"
        f"<td>{', '.join(e['aliases'])}</td>"
        f"<td><code>{json.dumps(e['current_state'], ensure_ascii=False)}</code></td></tr>"
        for e in snapshot["entities"]
    )
    rows_r = "".join(
        f"<tr><td>{r['relation_type']}</td><td>{r['from_entity'][:8]}…</td>"
        f"<td>{r['to_entity'][:8]}…</td><td>{r['confidence']:.2f}</td></tr>"
        for r in snapshot["relations"]
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Hakutaku Snapshot</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui; padding: 24px; background:#0b1220; color:#e2e8f0; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; font-size: 13px; }}
  th, td {{ border-bottom: 1px solid #1e293b; padding: 6px 10px; text-align: left; }}
  th {{ color:#7dd3fc; }}
  code {{ color:#facc15; }}
  h1, h2 {{ color:#f8fafc; }}
</style></head>
<body>
<h1>Snapshot — {snapshot['generated_at']}</h1>
<p><i>Pyvis não está instalado — usando HTML fallback.</i></p>
<h2>Entidades ({len(snapshot['entities'])})</h2>
<table><thead><tr><th>Type</th><th>Canonical name</th><th>Aliases</th><th>State</th></tr></thead>
<tbody>{rows_e}</tbody></table>
<h2>Relações ({len(snapshot['relations'])})</h2>
<table><thead><tr><th>Type</th><th>From</th><th>To</th><th>Conf.</th></tr></thead>
<tbody>{rows_r}</tbody></table>
</body></html>"""


# =====================================================================
# Ingester
# =====================================================================
def ingest_extraction(
    extraction: ExtractionResult,
    *,
    repository: GraphRepository,
    llm: LLMClient,
    source_occurred_at: datetime | None = None,
    snapshot_label: str | None = None,
) -> IngestStats:
    """Ingere uma extração no grafo e gera snapshots.

    Args:
        extraction: resultado da extração (entidades + relações).
        repository: conexão Supabase.
        llm: client para embeddings + Haiku resolver.
        source_occurred_at: timestamp do documento (override do extraction.extracted_at).
        snapshot_label: prefixo para arquivos de snapshot (default = source_title slug).

    Pré-condição:
        A row em `hakutaku.sources` correspondente a `extraction.source_id` já
        precisa existir antes desta chamada — caso contrário a FK de
        `hakutaku.llm_calls.source_id` (ou `events.source_id`) é violada
        durante chamadas LLM internas (resolver). O caller deve invocar
        `repository.upsert_source(...)` primeiro.
    """
    stats = IngestStats(
        source_id=extraction.source_id,
        source_title=extraction.source_title,
    )

    occurred_at = source_occurred_at or extraction.extracted_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    # Entidades.
    alias_to_uuid: dict[str, UUID] = {}

    for entity in extraction.entities:
        stats.entities_total += 1
        decision, embedding = resolve_entity(
            entity,
            repository=repository,
            llm=llm,
            source_title=extraction.source_title,
            occurred_at=occurred_at,
        )
        stats.resolver_stats.record(decision)

        if decision.action == "create":
            new_id = repository.insert_entity(
                entity=entity,
                embedding=embedding,
                source_id=extraction.source_id,
                source_excerpt=entity.source_excerpt,
                occurred_at=occurred_at,
            )
            stats.entities_created += 1
            for k in _alias_keys(entity):
                alias_to_uuid[k] = new_id
        else:
            assert decision.target_id is not None
            target_id = decision.target_id
            attributes, current_state, new_aliases = _to_attributes_for_update(entity)

            repository.update_entity(
                entity_id=target_id,
                new_attributes=attributes,
                new_current_state=current_state,
                new_aliases=new_aliases,
                new_canonical_name=entity.canonical_name,
                source_id=extraction.source_id,
                source_excerpt=entity.source_excerpt,
                occurred_at=occurred_at,
            )

            # Evento `entity_merged` separado para auditar a decisão.
            repository.insert_event(
                entity_id=target_id,
                event_type=EventType.ENTITY_MERGED,
                payload={
                    "merged_into": str(target_id),
                    "merged_from": {
                        "canonical_name": entity.canonical_name,
                        "aliases": entity.aliases,
                        "type": entity.type,
                    },
                    "similarity_score": decision.similarity_score,
                    "decision_method": decision.decision_method,
                    "reasoning": decision.reasoning,
                    "candidates_considered": decision.candidates_considered,
                },
                source_id=extraction.source_id,
                source_excerpt=entity.source_excerpt,
                occurred_at=occurred_at,
            )
            stats.entities_merged += 1
            for k in _alias_keys(entity):
                alias_to_uuid[k] = target_id

    # Relações.
    for rel in extraction.relations:
        stats.relations_total += 1
        if not _ingest_relation(
            rel,
            repository=repository,
            alias_to_uuid=alias_to_uuid,
            source_id=extraction.source_id,
            occurred_at=occurred_at,
            stats=stats,
        ):
            stats.relations_skipped += 1

    # Snapshots.
    label = snapshot_label or _slugify(extraction.source_title)
    ts = occurred_at.strftime("%Y%m%dT%H%M%S") + f"_{label}"
    snapshot_dir = get_settings().data_dir / "graph_snapshots"
    full_graph = repository.get_full_graph()
    stats.snapshot_json_path = _write_json_snapshot(full_graph, snapshot_dir, ts)
    stats.snapshot_html_path = _write_html_snapshot(full_graph, snapshot_dir, ts)

    return stats


def _ingest_relation(
    rel: ProposedRelation,
    *,
    repository: GraphRepository,
    alias_to_uuid: dict[str, UUID],
    source_id: UUID,
    occurred_at: datetime,
    stats: IngestStats,
) -> bool:
    """Resolve aliases para uuids e insere a aresta. Retorna True se persistida."""
    from_key = rel.from_alias.strip().lower()
    to_key = rel.to_alias.strip().lower()
    from_id = alias_to_uuid.get(from_key)
    to_id = alias_to_uuid.get(to_key)

    if from_id is None or to_id is None:
        stats.notes.append(
            f"Relação descartada — alias não resolvido: "
            f"{rel.relation_type} {rel.from_alias!r} → {rel.to_alias!r}"
        )
        return False

    if from_id == to_id:
        stats.notes.append(
            f"Relação descartada — self-loop após resolução: "
            f"{rel.relation_type} sobre {rel.from_alias!r}"
        )
        return False

    rel_type = rel.relation_type if isinstance(rel.relation_type, RelationType) else RelationType(
        rel.relation_type
    )
    new_rel_id = repository.insert_relation(
        from_entity=from_id,
        to_entity=to_id,
        relation_type=rel_type,
        attributes=rel.attributes,
        source_id=source_id,
        confidence=rel.confidence,
        source_excerpt=rel.source_excerpt,
        occurred_at=occurred_at,
    )
    if new_rel_id is not None:
        stats.relations_created += 1
        return True
    return False


def _slugify(text: str) -> str:
    keep = []
    for ch in text.lower():
        if ch.isalnum():
            keep.append(ch)
        elif ch in " -_":
            keep.append("-")
    slug = "".join(keep).strip("-")
    return slug[:60] or "snapshot"
