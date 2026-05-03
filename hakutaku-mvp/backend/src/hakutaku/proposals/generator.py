"""Gerador de propostas: findings detectados → batch tipado de Proposals.

Modelo: Claude Sonnet 4.5 via instructor (mesmo da extração — raciocínio
operacional pesado merece o modelo principal). Output: ProposalsBatch
validado por Pydantic.
"""

from __future__ import annotations

import json
from typing import Any

from hakutaku.config import get_settings
from hakutaku.llm.client import LLMClient
from hakutaku.llm.prompts import load_prompt
from hakutaku.schemas.proposals import Finding, ProposalsBatch


def generate_proposals(
    findings: list[Finding],
    *,
    llm: LLMClient,
    model: str | None = None,
) -> tuple[ProposalsBatch, dict[str, Any]]:
    """Roda o LLM sobre os findings e devolve um ProposalsBatch.

    Args:
        findings: lista de sinais detectados pelos detectores. Vazio → retorna
            batch vazio sem chamar LLM.
        llm: cliente para chamar Sonnet via instructor.
        model: override de modelo (default = `anthropic_model_heavy`).

    Returns:
        Tupla `(ProposalsBatch, call_metadata)`.
    """
    settings = get_settings()
    model = model or settings.anthropic_model_heavy

    if not findings:
        return ProposalsBatch(proposals=[], summary="Nenhum finding detectado."), {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 0,
            "cache_hit": False,
            "skipped": True,
        }

    prompt = load_prompt("proposals")
    findings_block = _render_findings_block(findings)
    system, user = prompt.format(findings_block=findings_block)

    batch, call_meta = llm.extract_structured(
        system=system,
        user=user,
        response_model=ProposalsBatch,
        model=model,
        stage="proposals_generation",
        max_tokens=4096,
        log_extras={
            "prompt_template_version": prompt.version,
            "findings_count": len(findings),
            "findings_detectors": sorted({f.detector for f in findings}),
        },
    )
    return batch, call_meta


def _render_findings_block(findings: list[Finding]) -> str:
    """Texto estruturado entregue ao LLM. Inclui IDs reais para o LLM citar."""
    by_detector: dict[str, list[Finding]] = {}
    for f in findings:
        by_detector.setdefault(f.detector, []).append(f)

    lines: list[str] = []
    for detector, fs in sorted(by_detector.items()):
        lines.append(f"### Detector: {detector} ({len(fs)} sinais)")
        for f in fs:
            lines.append(
                f"- severity={f.severity} | {f.description}"
            )
            for ent in f.related_entities:
                lines.append(f"    • entity_id={ent.id} | type={ent.type} | name=\"{ent.name}\"")
            if f.evidence:
                lines.append(f"    evidence: {json.dumps(f.evidence, ensure_ascii=False, default=str)}")
        lines.append("")
    return "\n".join(lines)
