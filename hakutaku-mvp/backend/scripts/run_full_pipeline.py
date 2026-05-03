"""Pipeline end-to-end Fase 3: 3 documentos → grafo Supabase + snapshots.

Ordem cronológica fixa:
    1) Reunião 1 — 24/03
    2) Chat       — 25-29/03
    3) Reunião 2  — 28/03

Para cada documento: adapter → extractor → ingester. Imprime estatísticas
progressivas (entidades criadas vs. mescladas, relações inseridas, custo,
caminho do snapshot HTML).

Uso:
    python -m scripts.run_full_pipeline                           # padrão (data/inputs/)
    python -m scripts.run_full_pipeline --reset                   # zera schema antes
    python -m scripts.run_full_pipeline --dir data/custom_inputs  # outra pasta

Flags úteis:
    --reset           — TRUNCATE do schema antes de rodar (uso em validação repetida).
    --dry-run         — corre adapter+extractor+resolver, NÃO escreve no Supabase.
    --no-snapshot     — pula geração de HTML/JSON snapshot.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Permite rodar como `python scripts/run_full_pipeline.py` sem instalar o package.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from hakutaku.adapters import ChatAdapter, MeetingAdapter, SourceAdapter  # noqa: E402
from hakutaku.config import get_settings  # noqa: E402
from hakutaku.extraction import extract_from_document  # noqa: E402
from hakutaku.graph import IngestStats, get_repository, ingest_extraction  # noqa: E402
from hakutaku.llm.client import get_llm_client  # noqa: E402

console = Console()


# =====================================================================
# Plano de execução
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
# Helpers de impressão
# =====================================================================
def _select_adapter(source_type: str) -> SourceAdapter:
    if source_type == "meeting":
        return MeetingAdapter()
    if source_type == "chat":
        return ChatAdapter()
    raise ValueError(f"source_type inválido: {source_type}")


def _print_step_header(idx: int, total: int, title: str, path: Path) -> None:
    console.rule(f"[bold cyan]Etapa {idx}/{total} — {title}[/]")
    console.print(f"[dim]Arquivo:[/] {path}")


def _print_extraction_summary(extraction: Any, call_meta: dict[str, Any]) -> None:
    by_type = Counter(e.type for e in extraction.entities)
    rel_by_type = Counter(r.relation_type for r in extraction.relations)

    table = Table(title="Extração", show_header=True, header_style="bold magenta")
    table.add_column("Métrica")
    table.add_column("Valor", justify="right")
    table.add_row("entidades", str(len(extraction.entities)))
    table.add_row("relações", str(len(extraction.relations)))
    table.add_row("input tokens", str(call_meta.get("input_tokens", "—")))
    table.add_row("output tokens", str(call_meta.get("output_tokens", "—")))
    table.add_row("cost (USD)", f"${call_meta.get('cost_usd', 0):.6f}")
    table.add_row("latency (ms)", str(call_meta.get("latency_ms", "—")))
    table.add_row("cache_hit", "✓" if call_meta.get("cache_hit") else "✗")
    console.print(table)

    if by_type:
        et = Table(title="Entidades por tipo", show_header=True, header_style="bold blue")
        et.add_column("Tipo")
        et.add_column("#", justify="right")
        for t, n in sorted(by_type.items(), key=lambda x: (-x[1], x[0])):
            et.add_row(t, str(n))
        console.print(et)
    if rel_by_type:
        rt = Table(title="Relações por tipo", show_header=True, header_style="bold green")
        rt.add_column("Tipo")
        rt.add_column("#", justify="right")
        for t, n in sorted(rel_by_type.items(), key=lambda x: (-x[1], x[0])):
            rt.add_row(t, str(n))
        console.print(rt)


def _print_ingest_summary(stats: IngestStats) -> None:
    t = Table(title="Ingestão", show_header=True, header_style="bold yellow")
    t.add_column("Métrica")
    t.add_column("Valor", justify="right")
    t.add_row("entidades total", str(stats.entities_total))
    t.add_row("  • criadas", str(stats.entities_created))
    t.add_row("  • mescladas", str(stats.entities_merged))
    t.add_row("relações total", str(stats.relations_total))
    t.add_row("  • criadas", str(stats.relations_created))
    t.add_row("  • puladas", str(stats.relations_skipped))
    console.print(t)

    rt = Table(title="Resolver — método de decisão", show_header=True, header_style="bold magenta")
    rt.add_column("Método")
    rt.add_column("#", justify="right")
    for k, n in sorted(stats.resolver_stats.by_method.items(), key=lambda x: (-x[1], x[0])):
        rt.add_row(k, str(n))
    if not stats.resolver_stats.by_method:
        rt.add_row("[dim]— vazio —[/]", "0")
    console.print(rt)

    if stats.notes:
        console.print(
            Panel(
                "\n".join(f"• {n}" for n in stats.notes),
                title="Notas (relações puladas, etc.)",
                border_style="red",
            )
        )

    snap_lines = []
    if stats.snapshot_json_path:
        snap_lines.append(f"[bold]json[/] {stats.snapshot_json_path}")
    if stats.snapshot_html_path:
        snap_lines.append(f"[bold]html[/] {stats.snapshot_html_path}")
    if snap_lines:
        console.print(Panel("\n".join(snap_lines), title="Snapshot", border_style="green"))


def _print_global_summary(
    docs_processed: int,
    total_call_meta: dict[str, Any],
    repo_stats: dict[str, int],
) -> None:
    t = Table(title="Resumo global", show_header=True, header_style="bold cyan")
    t.add_column("Métrica")
    t.add_column("Valor", justify="right")
    t.add_row("documentos", str(docs_processed))
    t.add_row("LLM calls (extração + resolver + embed)", str(total_call_meta["calls"]))
    t.add_row("input tokens", str(total_call_meta["input_tokens"]))
    t.add_row("output tokens", str(total_call_meta["output_tokens"]))
    t.add_row("cost total (USD)", f"${total_call_meta['cost_usd']:.6f}")
    t.add_row("entidades no grafo", str(repo_stats["entities"]))
    t.add_row("relações no grafo", str(repo_stats["relations"]))
    t.add_row("eventos registrados", str(repo_stats["events"]))
    console.print(t)


def _aggregate_logs(prev_count: int) -> dict[str, Any]:
    """Lê os logs de chamada do dia atual a partir de um cursor — soma tokens/custo.

    Retorna dict com {`calls`, `input_tokens`, `output_tokens`, `cost_usd`}.
    """
    import json

    logs_dir = get_settings().logs_dir
    if not logs_dir.exists():
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    files = sorted(logs_dir.rglob("*.json"))
    total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for f in files[prev_count:]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        total["calls"] += 1
        total["input_tokens"] += int(data.get("input_tokens") or 0)
        total["output_tokens"] += int(data.get("output_tokens") or 0)
        total["cost_usd"] += float(data.get("cost_usd") or 0.0)
    total["log_cursor"] = len(files)
    return total


# =====================================================================
# Main
# =====================================================================
def _resolve_input_path(base_dir: Path, filename: str) -> Path | None:
    candidate = base_dir / filename
    if candidate.exists():
        return candidate
    # fallback: aceita qualquer extensão ou nome parecido (apenas o stem precisa bater)
    stem = Path(filename).stem
    for p in base_dir.glob(f"{stem}.*"):
        if p.is_file():
            return p
    # fallback ainda mais largo: prefix match
    matches = list(base_dir.glob(f"{stem}*"))
    return matches[0] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Roda o pipeline completo da Fase 3.")
    parser.add_argument(
        "--dir",
        type=Path,
        default=get_settings().data_dir / "inputs",
        help="Pasta com os 3 arquivos de input.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="TRUNCATE no schema hakutaku antes de processar (uso em validação).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Roda extração mas NÃO escreve no Supabase nem gera snapshot.",
    )
    args = parser.parse_args()

    base_dir: Path = args.dir
    if not base_dir.exists():
        console.print(f"[red]Pasta de inputs não encontrada:[/] {base_dir}")
        return 2

    # Valida arquivos antes de gastar tempo/api.
    plan: list[tuple[dict[str, Any], Path]] = []
    missing: list[str] = []
    for step in PIPELINE:
        path = _resolve_input_path(base_dir, step["filename"])
        if path is None:
            missing.append(step["filename"])
        else:
            plan.append((step, path))

    if missing:
        console.print(
            Panel(
                "Arquivos esperados não encontrados em " + str(base_dir) + ":\n"
                + "\n".join(f"  • {m}" for m in missing)
                + "\n\nCrie esses arquivos (ou use --dir) antes de rodar.",
                title="Inputs faltando",
                border_style="red",
            )
        )
        return 2

    # Componentes.
    if args.dry_run:
        console.print("[yellow]Dry-run:[/] sem escrita no Supabase.")
        repo = None
    else:
        try:
            repo = get_repository()
        except (ValueError, Exception) as exc:  # psycopg.OperationalError extends Exception
            console.print(
                Panel(
                    f"Não foi possível conectar ao Supabase:\n{exc}\n\n"
                    "Verifique SUPABASE_DB_URL no .env (formato postgresql://user:pwd@host:port/db).",
                    title="Erro de conexão",
                    border_style="red",
                )
            )
            return 3

    if repo and args.reset:
        console.print("[yellow]Reset:[/] truncando schema hakutaku…")
        repo.truncate_all()

    llm = get_llm_client()

    # Persistência de chamadas LLM em hakutaku.llm_calls (auditoria via SQL).
    # Só pluga o sink quando há repo (sem dry-run).
    if repo and not args.dry_run:
        llm.attach_db_sink(repo.insert_llm_call)

    log_cursor = len(list(get_settings().logs_dir.rglob("*.json"))) if get_settings().logs_dir.exists() else 0
    grand_total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    for idx, (step, path) in enumerate(plan, start=1):
        _print_step_header(idx, len(plan), step["title"], path)

        raw = path.read_text(encoding="utf-8")
        adapter = _select_adapter(step["type"])
        doc = adapter.parse(raw, hints={"title": step["title"]})

        console.print(
            f"[dim]source_id={doc.source_id} • occurred_at={doc.occurred_at_str} "
            f"• chars={len(raw)}[/]"
        )

        # ATENÇÃO: a row em `sources` precisa existir ANTES da primeira chamada
        # LLM, porque `hakutaku.llm_calls.source_id` tem FK para `sources.id` e
        # o sink do LLMClient tenta gravar imediatamente. Inverter essa ordem
        # produz ForeignKeyViolation + InFailedSqlTransaction (transação morta).
        if repo and not args.dry_run:
            repo.upsert_source(
                source_id=doc.source_id,
                source_type=doc.source_type,
                title=step["title"],
                raw_content=doc.raw_content,
                metadata=doc.metadata,
                occurred_at=doc.occurred_at,
            )

        # Tagging do source_id em todas as chamadas LLM emitidas durante este doc
        # (extração + embeddings do resolver + Haiku da resolução de zona cinza).
        llm.set_source_context(doc.source_id)
        try:
            extraction = extract_from_document(doc, save=True)
            _print_extraction_summary(extraction, extraction.call_metadata)

            if repo and not args.dry_run:
                stats = ingest_extraction(
                    extraction,
                    repository=repo,
                    llm=llm,
                    source_occurred_at=doc.occurred_at,
                    snapshot_label=step["label"],
                )
                _print_ingest_summary(stats)
        finally:
            llm.set_source_context(None)

        # Atualiza acumulador de chamadas LLM lendo logs.
        delta = _aggregate_logs(log_cursor)
        log_cursor = delta.get("log_cursor", log_cursor)
        for k in ("calls", "input_tokens", "output_tokens", "cost_usd"):
            grand_total[k] += delta[k]

    if repo and not args.dry_run:
        repo_stats = repo.stats()
    else:
        repo_stats = {"entities": 0, "relations": 0, "events": 0}

    console.rule("[bold cyan]Fim do pipeline[/]")
    _print_global_summary(len(plan), grand_total, repo_stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
