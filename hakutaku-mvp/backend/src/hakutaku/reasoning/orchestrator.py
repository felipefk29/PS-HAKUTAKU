"""Orquestrador da Fase 5: detectores → gerador → persistência → snapshot.

`run_reasoning_cycle` é o ponto único de entrada para "gerar propostas a
partir do estado atual do grafo". Idempotente via flag `clear_existing`:
quando True, trunca propostas anteriores antes de inserir as novas.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from hakutaku.config import get_settings
from hakutaku.graph.repository import GraphRepository
from hakutaku.llm.client import LLMClient
from hakutaku.proposals.generator import generate_proposals
from hakutaku.reasoning.detectors import run_all_detectors
from hakutaku.schemas.proposals import Finding, Proposal


@dataclass
class ReasoningStats:
    findings_count: int = 0
    findings_by_detector: dict[str, int] = field(default_factory=dict)
    proposals_generated: int = 0
    proposals_persisted: int = 0
    proposals_by_type: dict[str, int] = field(default_factory=dict)
    summary: str = ""
    call_metadata: dict[str, Any] = field(default_factory=dict)
    snapshot_path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["snapshot_path"] = str(self.snapshot_path) if self.snapshot_path else None
        return d


def run_reasoning_cycle(
    *,
    repository: GraphRepository,
    llm: LLMClient,
    clear_existing: bool = True,
    save_snapshot: bool = True,
) -> ReasoningStats:
    """Roda 1 ciclo: detectores → LLM → persistência → snapshot JSON.

    Args:
        repository: conexão com o grafo.
        llm: cliente LLM (Sonnet via instructor).
        clear_existing: se True, trunca `hakutaku.proposals` antes de inserir.
        save_snapshot: se True, escreve `data/proposals/{ts}.json` com
            findings + propostas para inspeção visual.
    """
    stats = ReasoningStats()

    # 1. Detectores
    findings: list[Finding] = run_all_detectors(repository)
    stats.findings_count = len(findings)
    for f in findings:
        stats.findings_by_detector[f.detector] = (
            stats.findings_by_detector.get(f.detector, 0) + 1
        )

    if not findings:
        stats.summary = "Nenhum finding detectado — grafo está limpo."
        if save_snapshot:
            stats.snapshot_path = _write_snapshot(stats, [], [], "")
        return stats

    # 2. Gerador via LLM
    batch, call_meta = generate_proposals(findings, llm=llm)
    stats.proposals_generated = len(batch.proposals)
    stats.summary = batch.summary or "(sem resumo do gerador)"
    stats.call_metadata = call_meta

    # 3. Persistência
    if clear_existing:
        repository.clear_proposals()

    persisted_ids: list[UUID] = []
    for proposal in batch.proposals:
        # Filtra related_entity_ids contra IDs reais — protege contra
        # alucinação de UUID (mesmo com instructor).
        valid_ids = _filter_known_entity_ids(repository, proposal.related_entity_ids)
        rel_id = repository.insert_proposal(
            proposal_type=str(proposal.proposal_type),  # use_enum_values devolve str
            title=proposal.title,
            description=proposal.description,
            justification=proposal.justification,
            priority=proposal.priority,
            related_entities=valid_ids,
        )
        persisted_ids.append(rel_id)
        stats.proposals_by_type[str(proposal.proposal_type)] = (
            stats.proposals_by_type.get(str(proposal.proposal_type), 0) + 1
        )
    stats.proposals_persisted = len(persisted_ids)

    # 4. Snapshot
    if save_snapshot:
        stats.snapshot_path = _write_snapshot(
            stats, findings, batch.proposals, batch.summary
        )

    return stats


def _filter_known_entity_ids(
    repository: GraphRepository, ids: list[UUID]
) -> list[UUID]:
    """Mantém apenas IDs que existem em hakutaku.entities."""
    if not ids:
        return []
    repository._ensure_alive()
    with repository._conn.cursor() as cur:
        cur.execute(repository.SCHEMA_SETUP)
        cur.execute(
            "SELECT id FROM hakutaku.entities WHERE id = ANY(%s);",
            ([str(i) for i in ids],),
        )
        rows = cur.fetchall()
    return [
        UUID(str(r["id"])) if not isinstance(r["id"], UUID) else r["id"]
        for r in rows
    ]


def _write_snapshot(
    stats: ReasoningStats,
    findings: list[Finding],
    proposals: list[Proposal],
    summary: str,
) -> Path:
    out_dir = get_settings().data_dir / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"reasoning_cycle_{ts}.json"
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats.as_dict(),
        "summary": summary,
        "findings": [f.model_dump(mode="json") for f in findings],
        "proposals": [p.model_dump(mode="json") for p in proposals],
    }
    path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path
