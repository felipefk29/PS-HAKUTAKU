"""Demonstração de aprendizado: pipeline 2x — sem memória (A) vs com memória (B).

Este é o "money shot" da Fase 4. Roda os 3 documentos do desafio em duas
configurações distintas e compara lado a lado o que mudou:

- **Modo A — sem memória**: extractor recebe `repository=None`, então o prompt
  vai SEM `## Contexto organizacional acumulado`. Cross-linker NÃO roda.
  Esperado: mais entidades, mais duplicatas, zero relações cross-source
  baseadas em retrieval, e mais chamadas Haiku no resolver.

- **Modo B — com memória**: extractor recebe `repository=repo`, monta context
  block do grafo acumulado. Cross-linker roda no fim. Esperado: menos
  duplicatas, mais merges automáticos (auto_high), relações cross-source via
  context block, e respostas Q→D via cross-linker.

Reset entre modos é EXPLÍCITO (truncate full schema) — parte do desenho.

Métricas-chave:
- entidades / relações / eventos
- duplicatas (filtro conservador: similarity > 0.8 AND convergência por tipo)
- relações cross-source (endpoints criados em sources diferentes)
- chamadas Haiku no resolver (proxy do custo de ambiguidade)
- haiku_calls_economizadas = A.haiku - B.haiku (sinal forte de aprendizado)
- relações `answers` (criadas pelo extractor + cross-linker)

Uso:
    python -m scripts.demo_learning
    python -m scripts.demo_learning --inputs-dir data/inputs
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Permite rodar como `python scripts/demo_learning.py` sem instalar o package.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rich.columns import Columns  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from hakutaku.adapters import ChatAdapter, MeetingAdapter, SourceAdapter  # noqa: E402
from hakutaku.config import get_settings  # noqa: E402
from hakutaku.extraction import extract_from_document  # noqa: E402
from hakutaku.graph import GraphRepository, get_repository, ingest_extraction  # noqa: E402
from hakutaku.llm.client import LLMClient, get_llm_client  # noqa: E402
from hakutaku.memory import CrossLinkerStats, link_questions_to_decisions  # noqa: E402

console = Console()


# =====================================================================
# Plano canônico — espelha run_full_pipeline.py
# =====================================================================
PIPELINE = [
    {
        "label": "reuniao_01",
        "filename": "meeting_01_24-03.txt",
        "type": "meeting",
        "title": "Reunião 1 — 24/03 (kickoff TechNova)",
    },
    {
        "label": "chat",
        "filename": "chat_25-29-03.txt",
        "type": "chat",
        "title": "Chat — 25 a 29/03",
    },
    {
        "label": "reuniao_02",
        "filename": "meeting_02_28-03.txt",
        "type": "meeting",
        "title": "Reunião 2 — 28/03 (status TechNova)",
    },
]


# =====================================================================
# Helpers
# =====================================================================
def _select_adapter(source_type: str) -> SourceAdapter:
    if source_type == "meeting":
        return MeetingAdapter()
    if source_type == "chat":
        return ChatAdapter()
    raise ValueError(f"source_type inválido: {source_type}")


def _resolve_input_path(base_dir: Path, filename: str) -> Path | None:
    candidate = base_dir / filename
    if candidate.exists():
        return candidate
    stem = Path(filename).stem
    for p in base_dir.glob(f"{stem}.*"):
        if p.is_file():
            return p
    matches = list(base_dir.glob(f"{stem}*"))
    return matches[0] if matches else None


def _build_plan(base_dir: Path) -> list[tuple[dict[str, Any], Path]]:
    plan: list[tuple[dict[str, Any], Path]] = []
    missing: list[str] = []
    for step in PIPELINE:
        path = _resolve_input_path(base_dir, step["filename"])
        if path is None:
            missing.append(step["filename"])
        else:
            plan.append((step, path))
    if missing:
        raise FileNotFoundError(
            f"Inputs faltando em {base_dir}: {missing}"
        )
    return plan


def _aggregate_logs_since(cursor: int) -> tuple[dict[str, Any], int]:
    """Soma tokens/custo dos logs desde `cursor`. Retorna (agregado, novo_cursor)."""
    logs_dir = get_settings().logs_dir
    if not logs_dir.exists():
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}, cursor
    files = sorted(logs_dir.rglob("*.json"))
    total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for f in files[cursor:]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        total["calls"] += 1
        total["input_tokens"] += int(data.get("input_tokens") or 0)
        total["output_tokens"] += int(data.get("output_tokens") or 0)
        total["cost_usd"] += float(data.get("cost_usd") or 0.0)
    return total, len(files)


# =====================================================================
# Pipeline runner por modo
# =====================================================================
def _run_one_mode(
    *,
    repo: GraphRepository,
    llm: LLMClient,
    plan: list[tuple[dict[str, Any], Path]],
    with_memory: bool,
    with_cross_link: bool,
    label: str,
) -> CrossLinkerStats | None:
    """Trunca schema e roda os 3 documentos. Retorna stats do cross-linker (ou None)."""
    console.rule(f"[bold]{label}[/]")
    console.print(f"[yellow]Reset:[/] truncando schema hakutaku…")
    repo.truncate_all()

    for idx, (step, path) in enumerate(plan, start=1):
        console.print(f"[dim]{idx}/{len(plan)} — {step['title']}[/]")
        raw = path.read_text(encoding="utf-8")
        adapter = _select_adapter(step["type"])
        doc = adapter.parse(raw, hints={"title": step["title"]})

        repo.upsert_source(
            source_id=doc.source_id,
            source_type=doc.source_type,
            title=step["title"],
            raw_content=doc.raw_content,
            metadata=doc.metadata,
            occurred_at=doc.occurred_at,
        )

        llm.set_source_context(doc.source_id)
        try:
            extraction = extract_from_document(
                doc,
                repository=(repo if with_memory else None),
                save=True,
            )
            ingest_extraction(
                extraction,
                repository=repo,
                llm=llm,
                source_occurred_at=doc.occurred_at,
                snapshot_label=f"{step['label']}_{label.lower().replace(' ', '_')}",
            )
        finally:
            llm.set_source_context(None)

    cross_stats: CrossLinkerStats | None = None
    if with_cross_link:
        console.print("[cyan]Rodando cross-linker (question → decision via Haiku)…[/]")
        cross_stats = link_questions_to_decisions(repository=repo, llm=llm)
        console.print(
            f"[dim]Cross-linker: {cross_stats.questions_considered} perguntas, "
            f"{cross_stats.candidate_pairs} pares, {cross_stats.haiku_calls} Haiku, "
            f"{cross_stats.links_created} links criados.[/]"
        )

    return cross_stats


# =====================================================================
# Coleta de métricas pós-modo
# =====================================================================
def _collect_metrics(
    repo: GraphRepository,
    *,
    cross_stats: CrossLinkerStats | None,
    cost_breakdown: dict[str, Any],
) -> dict[str, Any]:
    base = repo.stats()
    raw_pairs = repo.find_duplicate_pairs(min_similarity=0.8)
    confirmed = _filter_conservative_duplicates(raw_pairs)
    cross_source = repo.count_cross_source_relations()
    haiku_resolver = repo.count_haiku_resolver_calls()
    method_dist = repo.count_resolver_decisions_by_method()
    answers = repo.list_answers_relations()

    # Distribuição de tipos de entidades (snapshot leve para o relatório).
    full = repo.get_full_graph()
    type_counts = dict(Counter(e["type"] for e in full["entities"]))

    return {
        "entities": int(base["entities"]),
        "relations": int(base["relations"]),
        "events": int(base["events"]),
        "type_counts": type_counts,
        "duplicates_raw_count": len(raw_pairs),
        "duplicates_confirmed_count": len(confirmed),
        "duplicates_confirmed_sample": [
            {
                "type": p["type1"],
                "name1": p["name1"],
                "name2": p["name2"],
                "sim": float(p["sim"]),
            }
            for p in confirmed[:10]
        ],
        "cross_source_relations": int(cross_source),
        "haiku_resolver_calls": int(haiku_resolver),
        "resolver_method_distribution": {str(k): int(v) for k, v in method_dist.items()},
        "answers_relations_count": len(answers),
        "answers_relations": [
            {
                "rel_id": str(a["rel_id"]),
                "question": a["question_name"],
                "decision": a["decision_name"],
                "decision_rationale": a.get("decision_rationale") or "",
                "question_state": a.get("question_state") or "",
                "rel_attrs": a.get("rel_attrs") or {},
                "rel_confidence": float(a.get("rel_confidence") or 0.0),
            }
            for a in answers
        ],
        "cross_link_stats": _serialize_cross_stats(cross_stats),
        "cost": cost_breakdown,
    }


def _filter_conservative_duplicates(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """AND de convergência por tipo — evita 'TechNova vs TechCorp' (~0.75 sim) virar duplicata.

    - Person/Client: aliases1 ∩ aliases2 (incluindo canonical_name) precisa ser não-vazio.
    - Risk: severity precisa coincidir E ser não-null.
    - Outros tipos: similarity > 0.8 já basta (raros e o threshold filtra bem).
    """
    confirmed: list[dict[str, Any]] = []
    for p in pairs:
        t = p["type1"]
        names_1 = {(p["name1"] or "").lower(), *((p.get("aliases1") or []))}
        names_2 = {(p["name2"] or "").lower(), *((p.get("aliases2") or []))}
        names_1 = {n.lower() for n in names_1 if n}
        names_2 = {n.lower() for n in names_2 if n}

        if t in ("Person", "Client"):
            if not (names_1 & names_2):
                continue
        elif t == "Risk":
            sev1 = (p.get("attrs1") or {}).get("severity")
            sev2 = (p.get("attrs2") or {}).get("severity")
            if sev1 is None or sev2 is None or sev1 != sev2:
                continue
        # Outros tipos passam pelo threshold de similaridade puro.

        confirmed.append(p)
    return confirmed


def _serialize_cross_stats(s: CrossLinkerStats | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "questions_considered": s.questions_considered,
        "candidate_pairs": s.candidate_pairs,
        "haiku_calls": s.haiku_calls,
        "verdict_yes": s.verdict_yes,
        "verdict_no": s.verdict_no,
        "verdict_maybe": s.verdict_maybe,
        "links_created": s.links_created,
        "questions_marked_answered": s.questions_marked_answered,
        "linked": [
            {
                "question_id": str(link.question_id),
                "question_name": link.question_name,
                "decision_id": str(link.decision_id),
                "decision_name": link.decision_name,
                "cosine_similarity": link.cosine_similarity,
                "verdict_confidence": link.verdict_confidence,
                "reason": link.reason,
                "relation_id": str(link.relation_id),
            }
            for link in s.linked
        ],
    }


# =====================================================================
# Render
# =====================================================================
def _render_side_by_side(
    metrics_a: dict[str, Any],
    metrics_b: dict[str, Any],
    haiku_savings: int,
) -> None:
    rows = [
        ("entidades no grafo", metrics_a["entities"], metrics_b["entities"]),
        ("relações no grafo", metrics_a["relations"], metrics_b["relations"]),
        ("eventos registrados", metrics_a["events"], metrics_b["events"]),
        (
            "duplicatas brutas (sim>0.8)",
            metrics_a["duplicates_raw_count"],
            metrics_b["duplicates_raw_count"],
        ),
        (
            "duplicatas confirmadas (filtro conservador)",
            metrics_a["duplicates_confirmed_count"],
            metrics_b["duplicates_confirmed_count"],
        ),
        (
            "relações cross-source",
            metrics_a["cross_source_relations"],
            metrics_b["cross_source_relations"],
        ),
        (
            "chamadas Haiku no resolver",
            metrics_a["haiku_resolver_calls"],
            metrics_b["haiku_resolver_calls"],
        ),
        (
            "relações `answers` no grafo",
            metrics_a["answers_relations_count"],
            metrics_b["answers_relations_count"],
        ),
    ]

    t = Table(
        title="Comparação A (sem memória) × B (com memória)",
        show_header=True,
        header_style="bold cyan",
        title_style="bold cyan",
    )
    t.add_column("Métrica", style="bold")
    t.add_column("A (sem memória)", justify="right", style="red")
    t.add_column("B (com memória)", justify="right", style="green")
    t.add_column("Δ", justify="right", style="bold yellow")
    for label, a, b in rows:
        delta = b - a
        delta_str = f"{delta:+d}" if delta != 0 else "0"
        t.add_row(label, str(a), str(b), delta_str)
    console.print(t)

    # haiku_calls_economizadas — sinal forte de aprendizado pedido pelo user.
    panel_body = (
        f"[bold]haiku_calls_economizadas[/] = "
        f"A.haiku ({metrics_a['haiku_resolver_calls']}) "
        f"− B.haiku ({metrics_b['haiku_resolver_calls']}) = "
        f"[bold green]{haiku_savings}[/]\n\n"
        f"[dim]Quando o context block ajuda, o resolver auto-decide mais "
        f"(auto_high) e menos casos caem na zona cinza onde o Haiku precisa "
        f"deliberar. Esta é evidência concreta de aprendizado: o sistema "
        f"está mais decisivo a cada documento.[/]"
    )
    console.print(Panel(panel_body, title="Métrica destacada", border_style="green"))

    # Distribuição de método de resolução (auto_high vs llm vs auto_low vs bypass).
    md_a = metrics_a.get("resolver_method_distribution", {})
    md_b = metrics_b.get("resolver_method_distribution", {})
    methods = sorted(set(md_a) | set(md_b))
    if methods:
        m = Table(title="Resolver — método de decisão (acumulado)", header_style="bold magenta")
        m.add_column("Método")
        m.add_column("A", justify="right")
        m.add_column("B", justify="right")
        for k in methods:
            m.add_row(k, str(md_a.get(k, 0)), str(md_b.get(k, 0)))
        console.print(m)


def _render_concrete_example(metrics_b: dict[str, Any]) -> None:
    """Mostra o exemplo concreto: OpenQuestion ↔ Decision via `answers`.

    Prioriza pares cuja pergunta menciona "REST" ou "GraphQL" (caso canônico do
    desafio), com fallback para qualquer link encontrado no modo B.
    """
    answers = metrics_b.get("answers_relations") or []
    if not answers:
        console.print(
            Panel(
                "[red]Nenhuma relação `answers` encontrada no modo B.[/] "
                "Cross-linker não casou nenhuma pergunta com nenhuma decisão. "
                "Investigar: thresholds, embeddings, ou ausência de Decisions "
                "que respondam às OpenQuestions.",
                title="Exemplo concreto Q→D",
                border_style="red",
            )
        )
        return

    # Prioriza REST/GraphQL.
    def _score(a: dict[str, Any]) -> int:
        text = (a.get("question") or "") + " " + (a.get("decision") or "")
        text = text.lower()
        score = 0
        if "rest" in text:
            score += 2
        if "graphql" in text:
            score += 2
        return score

    best = sorted(answers, key=_score, reverse=True)[0]
    rel_attrs = best.get("rel_attrs") or {}
    method = rel_attrs.get("method", "extraction")
    body = (
        f"[bold cyan]Pergunta (OpenQuestion):[/]\n"
        f"  {best['question']}\n"
        f"  [dim]state agora: {best.get('question_state', '?')}[/]\n\n"
        f"[bold green]Decisão (Decision):[/]\n"
        f"  {best['decision']}\n"
        f"  [dim]rationale: {best.get('decision_rationale', '—')[:200]}[/]\n\n"
        f"[bold]Aresta `answers`:[/]\n"
        f"  método: {method}\n"
        f"  confidence: {best.get('rel_confidence', 0):.3f}\n"
    )
    if "reason" in rel_attrs:
        body += f"  reason: {rel_attrs['reason']}\n"
    if "cosine_similarity" in rel_attrs:
        body += f"  cosine: {rel_attrs['cosine_similarity']:.3f}\n"

    console.print(Panel(body, title="Exemplo concreto: pergunta fechada por decisão", border_style="green"))

    if len(answers) > 1:
        console.print(f"[dim]+ {len(answers) - 1} outra(s) relação(ões) `answers` no grafo.[/]")


def _render_cost_summary(
    cost_a: dict[str, Any],
    cost_b: dict[str, Any],
    cost_total: dict[str, Any],
) -> None:
    t = Table(title="Custo Fase 4 (extração + resolver + embeddings + cross-linker)", header_style="bold yellow")
    t.add_column("Bucket")
    t.add_column("calls", justify="right")
    t.add_column("in tokens", justify="right")
    t.add_column("out tokens", justify="right")
    t.add_column("USD", justify="right")
    for label, c in [("A (sem memória)", cost_a), ("B (com memória)", cost_b), ("TOTAL", cost_total)]:
        t.add_row(
            label,
            str(c["calls"]),
            str(c["input_tokens"]),
            str(c["output_tokens"]),
            f"${c['cost_usd']:.4f}",
        )
    console.print(t)


def _save_report(
    metrics_a: dict[str, Any],
    metrics_b: dict[str, Any],
    *,
    haiku_savings: int,
    out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"demo_learning_report_{ts}.json"
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "haiku_calls_economizadas": haiku_savings,
        "mode_a_no_memory": metrics_a,
        "mode_b_with_memory": metrics_b,
    }
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


# =====================================================================
# Main
# =====================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Demo de aprendizado: A (sem memória) vs B (com memória).")
    parser.add_argument(
        "--inputs-dir",
        type=Path,
        default=get_settings().data_dir / "inputs",
        help="Pasta com os 3 arquivos de input.",
    )
    args = parser.parse_args()

    try:
        plan = _build_plan(args.inputs_dir)
    except FileNotFoundError as exc:
        console.print(Panel(str(exc), title="Inputs faltando", border_style="red"))
        return 2

    repo = get_repository()
    llm = get_llm_client()
    llm.attach_db_sink(repo.insert_llm_call)

    # Cursor inicial dos logs — tudo depois disso é da Fase 4 demo.
    logs_dir = get_settings().logs_dir
    cursor = len(list(logs_dir.rglob("*.json"))) if logs_dir.exists() else 0
    cursor_start = cursor

    # Modo A: sem memória, sem cross-linker.
    _run_one_mode(
        repo=repo,
        llm=llm,
        plan=plan,
        with_memory=False,
        with_cross_link=False,
        label="MODO A — sem memória",
    )
    cost_a, cursor = _aggregate_logs_since(cursor)
    metrics_a = _collect_metrics(repo, cross_stats=None, cost_breakdown=cost_a)

    # Modo B: com memória, com cross-linker.
    cross_stats_b = _run_one_mode(
        repo=repo,
        llm=llm,
        plan=plan,
        with_memory=True,
        with_cross_link=True,
        label="MODO B — com memória + cross-linker",
    )
    cost_b, cursor = _aggregate_logs_since(cursor)
    metrics_b = _collect_metrics(repo, cross_stats=cross_stats_b, cost_breakdown=cost_b)

    cost_total, _ = _aggregate_logs_since(cursor_start)
    haiku_savings = metrics_a["haiku_resolver_calls"] - metrics_b["haiku_resolver_calls"]

    console.rule("[bold cyan]Comparação final[/]")
    _render_side_by_side(metrics_a, metrics_b, haiku_savings)
    _render_concrete_example(metrics_b)
    _render_cost_summary(cost_a, cost_b, cost_total)

    report_path = _save_report(
        metrics_a,
        metrics_b,
        haiku_savings=haiku_savings,
        out_dir=get_settings().data_dir,
    )
    console.print(Panel(f"[bold green]Relatório salvo:[/] {report_path}", border_style="green"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
