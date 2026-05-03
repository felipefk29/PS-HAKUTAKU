"""scripts/diag_truncate.py — diagnóstico isolado da hipótese:
TRUNCATE CASCADE pelo psycopg derruba a conexão; DELETE iterativo passa.

Sem chamadas LLM. Sem custo. Roda em segundos.

Etapas:
  1. INSERT row em hakutaku.sources (confirma escrita básica funciona)
  2. DELETE iterativo via repo.truncate_all() (estado atual de produção)
  3. INSERT outra row (recria o que truncar)
  4. TRUNCATE ... RESTART IDENTITY CASCADE direto via cur.execute (try/except)
  5. SELECT count(*) na MESMA conexão (pós-mortem: conexão sobreviveu?)

Exit codes:
    0 — tudo passou (não há problema observável)
    1 — DELETE passa, TRUNCATE falha (tese confirmada)
    2 — até DELETE falhou (problema mais grave)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Permite rodar como `python -m scripts.diag_truncate` sem instalar o package.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from hakutaku.graph.repository import get_repository  # noqa: E402

console = Console()


TRUNCATE_SQL = """
TRUNCATE
  hakutaku.events,
  hakutaku.relations,
  hakutaku.entities,
  hakutaku.proposals,
  hakutaku.patterns,
  hakutaku.llm_calls,
  hakutaku.sources
RESTART IDENTITY CASCADE;
"""


def _insert_test_source(repo, label: str):
    """Insere row mínima em hakutaku.sources. Retorna o uuid gerado.

    Acessa `repo._conn` diretamente — esse script é diagnóstico, não produção,
    então não vamos adicionar accessor público só pra isso.
    """
    with repo._conn.cursor() as cur:
        cur.execute(repo.SCHEMA_SETUP)
        cur.execute(
            """
            INSERT INTO hakutaku.sources (source_type, title, raw_content)
            VALUES ('chat', %s, %s)
            RETURNING id;
            """,
            (f"diag_truncate test row ({label})", "diagnostic content"),
        )
        row = cur.fetchone()
    repo._conn.commit()
    return row["id"]


def _count_tables(repo) -> dict[str, int]:
    counts: dict[str, int] = {}
    tables = [
        "sources", "entities", "events", "relations",
        "proposals", "patterns", "llm_calls",
    ]
    with repo._conn.cursor() as cur:
        cur.execute(repo.SCHEMA_SETUP)
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS n FROM hakutaku.{t};")
            row = cur.fetchone()
            counts[t] = int((row or {}).get("n", 0))
    return counts


def main() -> int:
    results: dict[str, dict] = {}

    console.rule("[bold cyan]diag_truncate — isolated TRUNCATE/DELETE diagnostic[/]")

    # --- Abrir repositório ---------------------------------------------
    console.print("[dim]Opening repository...[/]")
    try:
        repo = get_repository()
    except Exception as exc:
        console.print(f"[red]Failed to open repository:[/] {type(exc).__name__}: {exc}")
        return 2

    info = repo._conn.info
    safe_info = {
        "host": info.host,
        "port": info.port,
        "dbname": info.dbname,
        "user": info.user,
    }
    # SSL info via parameter_status (libpq runtime params).
    ssl_status = info.parameter_status("ssl_library") or "unknown"
    console.print(
        Panel(
            f"backend_pid: {info.backend_pid}\n"
            f"server_version: {info.server_version}\n"
            f"transaction_status: {info.transaction_status}\n"
            f"ssl_library_param: {ssl_status}\n"
            f"connection_info: {safe_info}",
            title="Connection info",
            border_style="cyan",
        )
    )

    # --- ETAPA 1 — INSERT inicial --------------------------------------
    console.rule("[bold]ETAPA 1 — INSERT row de teste em hakutaku.sources[/]")
    t0 = time.monotonic()
    try:
        new_id = _insert_test_source(repo, "etapa-1")
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[green]OK[/] id={new_id} ({dur}ms)")
        results["INSERT (1)"] = {"ok": True, "detail": f"id={new_id}", "ms": dur}
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[red]ERRO:[/] {type(exc).__name__}: {exc}")
        results["INSERT (1)"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:100]}",
            "ms": dur,
        }
        # Sem INSERT inicial não dá pra continuar testando truncate.
        _print_summary(results)
        return 2

    # --- ETAPA 2 — DELETE iterativo (repo.truncate_all atual) ---------
    console.rule("[bold]ETAPA 2 — DELETE iterativo (repo.truncate_all)[/]")
    t0 = time.monotonic()
    delete_ok = False
    try:
        repo.truncate_all()
        dur = int((time.monotonic() - t0) * 1000)
        counts = _count_tables(repo)
        all_zero = all(v == 0 for v in counts.values())
        console.print(f"[green]DELETE iterativo OK[/] ({dur}ms)")
        console.print(f"[dim]contagens pós-DELETE:[/] {counts}")
        results["DELETE iterativo"] = {
            "ok": True,
            "detail": f"all_zero={all_zero} counts={counts}",
            "ms": dur,
        }
        delete_ok = True
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[red]ERRO no DELETE:[/] {type(exc).__name__}: {str(exc)[:200]}")
        console.print(f"[red]conn.closed após erro: {repo._conn.closed}[/]")
        results["DELETE iterativo"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:100]} | closed={repo._conn.closed}",
            "ms": dur,
        }

    # --- ETAPA 3 — INSERT novamente -------------------------------------
    console.rule("[bold]ETAPA 3 — INSERT row novamente para ter o que truncar[/]")
    t0 = time.monotonic()
    try:
        new_id_2 = _insert_test_source(repo, "etapa-3")
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[green]OK[/] id={new_id_2} ({dur}ms)")
        results["INSERT (2)"] = {"ok": True, "detail": f"id={new_id_2}", "ms": dur}
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[red]ERRO:[/] {type(exc).__name__}: {exc}")
        results["INSERT (2)"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:100]}",
            "ms": dur,
        }

    # --- ETAPA 4 — TRUNCATE CASCADE direto -----------------------------
    console.rule("[bold]ETAPA 4 — TRUNCATE CASCADE direto via psycopg[/]")
    pid_before = repo._conn.info.backend_pid
    console.print(f"[dim]conn.closed antes:[/] {repo._conn.closed}")
    console.print(f"[dim]backend_pid antes:[/] {pid_before}")
    t0 = time.monotonic()
    truncate_ok = False
    truncate_exc_type: str | None = None
    try:
        with repo._conn.cursor() as cur:
            cur.execute(repo.SCHEMA_SETUP)
            cur.execute(TRUNCATE_SQL)
        repo._conn.commit()
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[green]TRUNCATE CASCADE OK[/] ({dur}ms)")
        console.print(f"[dim]conn.closed depois:[/] {repo._conn.closed}")
        results["TRUNCATE CASCADE"] = {
            "ok": True,
            "detail": f"closed={repo._conn.closed}",
            "ms": dur,
        }
        truncate_ok = True
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        truncate_exc_type = type(exc).__name__
        console.print(f"[red]ERRO no TRUNCATE:[/] {truncate_exc_type}")
        console.print(f"[red]Mensagem:[/] {str(exc)[:400]}")
        console.print(f"[red]conn.closed depois:[/] {repo._conn.closed}")
        # Tentar rollback é seguro mesmo em conexão morta.
        try:
            repo._conn.rollback()
        except Exception:
            pass
        results["TRUNCATE CASCADE"] = {
            "ok": False,
            "detail": f"{truncate_exc_type}: {str(exc)[:100]} | closed={repo._conn.closed}",
            "ms": dur,
        }

    # --- ETAPA 5 — Pós-mortem -------------------------------------------
    console.rule("[bold]ETAPA 5 — Pós-mortem: SELECT na MESMA conexão[/]")
    t0 = time.monotonic()
    try:
        with repo._conn.cursor() as cur:
            cur.execute(repo.SCHEMA_SETUP)
            cur.execute("SELECT COUNT(*) AS n FROM hakutaku.sources;")
            row = cur.fetchone()
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[green]SELECT pós-mortem OK[/] count={row['n']} ({dur}ms)")
        console.print("[green]Conexão sobreviveu ao TRUNCATE.[/]")
        results["SELECT pós-mortem"] = {
            "ok": True,
            "detail": f"count={row['n']} | conn_alive=True",
            "ms": dur,
        }
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[red]ERRO no SELECT pós-mortem:[/] {type(exc).__name__}")
        console.print(f"[red]Mensagem:[/] {str(exc)[:400]}")
        console.print(f"[red]conn.closed:[/] {repo._conn.closed}")
        console.print(
            "[red bold]Conexão MORREU após TRUNCATE — confirma a tese.[/]"
        )
        results["SELECT pós-mortem"] = {
            "ok": False,
            "detail": (
                f"{type(exc).__name__}: {str(exc)[:80]} | conn_alive=False"
            ),
            "ms": dur,
        }

    _print_summary(results)

    # Exit code conforme spec.
    if delete_ok and truncate_ok:
        console.print(
            "[bold green]Tudo passou — não há problema observável.[/] "
            "Conexão psycopg ↔ Supabase está saudável para AMBAS as estratégias."
        )
        return 0
    if delete_ok and not truncate_ok:
        console.print(
            f"[bold yellow]Tese confirmada:[/] DELETE passa, TRUNCATE CASCADE falha "
            f"(causa: {truncate_exc_type}). Próximo passo: aplicar fix sobre a "
            "estratégia de conexão (item 6 — _ensure_alive antes de cada operação)."
        )
        return 1
    console.print(
        "[bold red]Problema mais grave:[/] até DELETE iterativo falhou. "
        "Investigar conectividade base / role permissions."
    )
    return 2


def _print_summary(results: dict[str, dict]) -> None:
    console.rule("[bold cyan]Resumo[/]")
    table = Table(title="diag_truncate — resumo final", header_style="bold cyan")
    table.add_column("Etapa")
    table.add_column("Resultado", justify="center")
    table.add_column("Detalhe")
    table.add_column("ms", justify="right")
    for etapa in [
        "INSERT (1)",
        "DELETE iterativo",
        "INSERT (2)",
        "TRUNCATE CASCADE",
        "SELECT pós-mortem",
    ]:
        r = results.get(etapa)
        if r is None:
            mark = "[yellow]—[/]"
            detail = "(skipped)"
            ms = "0"
        elif r["ok"]:
            mark = "[green]OK[/]"
            detail = r["detail"][:90]
            ms = str(r["ms"])
        else:
            mark = "[red]ERRO[/]"
            detail = r["detail"][:90]
            ms = str(r["ms"])
        table.add_row(etapa, mark, detail, ms)
    console.print(table)


if __name__ == "__main__":
    raise SystemExit(main())
