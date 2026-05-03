"""CLI: roda adapter + extractor sobre um arquivo de input e mostra resumo.

Uso:
    python -m scripts.run_extraction --source data/inputs/meeting_01.txt --type meeting

Saída:
    - Tabela rich com contagem de entidades por tipo, relações por tipo, e
      tokens / custo / latência da chamada LLM.
    - Caminho absoluto do JSON gravado em data/extractions/.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# Permite rodar como script standalone (sem instalar o package).
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from hakutaku.adapters import ChatAdapter, MeetingAdapter, NormalizedDocument, SourceAdapter  # noqa: E402
from hakutaku.extraction import extract_from_document, save_extraction  # noqa: E402

console = Console()


def _select_adapter(source_type: str) -> SourceAdapter:
    if source_type == "meeting":
        return MeetingAdapter()
    if source_type == "chat":
        return ChatAdapter()
    raise ValueError(f"source_type inválido: {source_type}")


def _print_doc_panel(doc: NormalizedDocument) -> None:
    meta_lines = [
        f"[bold]source_id[/]      {doc.source_id}",
        f"[bold]title[/]          {doc.title}",
        f"[bold]occurred_at[/]    {doc.occurred_at_str}",
        f"[bold]chars[/]          {doc.metadata.get('char_count', len(doc.raw_content))}",
    ]
    if "participants" in doc.metadata:
        meta_lines.append(f"[bold]participants[/]   {', '.join(doc.metadata['participants']) or '—'}")
    if "speaker_turns" in doc.metadata:
        meta_lines.append(f"[bold]speaker_turns[/]  {doc.metadata['speaker_turns']}")
    if "message_count" in doc.metadata:
        meta_lines.append(f"[bold]messages[/]       {doc.metadata['message_count']}")
    console.print(Panel("\n".join(meta_lines), title=f"Document ({doc.source_type})", border_style="cyan"))


def _print_extraction_summary(entities, relations, call_meta, output_path: Path) -> None:
    by_type = Counter(e.type for e in entities)
    rel_by_type = Counter(r.relation_type for r in relations)

    et = Table(title="Entities by type", show_header=True, header_style="bold magenta")
    et.add_column("Type")
    et.add_column("Count", justify="right")
    for t, n in sorted(by_type.items(), key=lambda x: (-x[1], x[0])):
        et.add_row(t, str(n))
    if not by_type:
        et.add_row("[dim]— none —[/]", "0")

    rt = Table(title="Relations by type", show_header=True, header_style="bold green")
    rt.add_column("Type")
    rt.add_column("Count", justify="right")
    for t, n in sorted(rel_by_type.items(), key=lambda x: (-x[1], x[0])):
        rt.add_row(t, str(n))
    if not rel_by_type:
        rt.add_row("[dim]— none —[/]", "0")

    ct = Table(title="LLM call", show_header=True, header_style="bold yellow")
    ct.add_column("Field")
    ct.add_column("Value", justify="right")
    ct.add_row("input_tokens", str(call_meta.get("input_tokens", "—")))
    ct.add_row("output_tokens", str(call_meta.get("output_tokens", "—")))
    ct.add_row("cost_usd", f"${call_meta.get('cost_usd', 0):.6f}")
    ct.add_row("latency_ms", str(call_meta.get("latency_ms", "—")))
    ct.add_row("cache_hit", "✓" if call_meta.get("cache_hit") else "✗")

    console.print(et)
    console.print(rt)
    console.print(ct)
    console.print(Panel(f"[green]Saved:[/] {output_path}", border_style="green"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Roda extração end-to-end sobre um arquivo de input.")
    parser.add_argument("--source", required=True, type=Path, help="Caminho do arquivo de input.")
    parser.add_argument("--type", required=True, choices=["meeting", "chat"], help="Tipo de fonte.")
    args = parser.parse_args()

    if not args.source.exists():
        console.print(f"[red]Arquivo não encontrado:[/] {args.source}")
        return 1

    raw = args.source.read_text(encoding="utf-8")
    adapter = _select_adapter(args.type)
    doc = adapter.parse(raw, hints={"title": args.source.stem})
    _print_doc_panel(doc)

    console.print("[dim]Extraindo...[/]")
    result = extract_from_document(doc, save=False)
    output_path = save_extraction(result)

    _print_extraction_summary(
        entities=result.entities,
        relations=result.relations,
        call_meta=result.call_metadata,
        output_path=output_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
