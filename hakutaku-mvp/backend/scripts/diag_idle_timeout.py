"""scripts/diag_idle_timeout.py — stress test do bug "idle timeout durante LLM".

Reproduz o caso do run 1 do demo_learning sem usar LLM:

  1. Abre conexão, registra backend_pid
  2. INSERT inicial em hakutaku.sources (confirma escrita ok)
  3. SLEEP 60s (simula chamada LLM longa)
  4. INSERT "naked" — direto via cursor SEM passar por _ensure_alive.
     Captura exceção. Confirma se a conexão sobreviveu ao idle.
  5. Roda _ensure_alive() explicitamente. O print interno revela se
     ele detectou conexão morta e reabriu.
  6. INSERT "guarded" — após _ensure_alive. Deve passar SEMPRE.
  7. Compara backend_pid antes e depois de _ensure_alive.
  8. Tabela rich resumindo.

Sem custo (não usa LLM). ~70 segundos de execução.

Exit codes:
    0  — naked passou (idle timeout não reprodutível em 60s nesse setup)
         OU naked falhou e _ensure_alive recuperou (fix funciona)
    1  — _ensure_alive falhou em recuperar (fix não funciona, escalar)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Permite rodar como `python -m scripts.diag_idle_timeout` sem instalar.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import psycopg  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from hakutaku.graph.repository import get_repository  # noqa: E402

console = Console()

SLEEP_SECONDS = 60


def _direct_insert(repo, label: str) -> str:
    """INSERT direto via repo._conn — sem _ensure_alive. Pode levantar
    OperationalError/InterfaceError se a conexão tiver morrido."""
    with repo._conn.cursor() as cur:
        cur.execute(repo.SCHEMA_SETUP)
        cur.execute(
            """
            INSERT INTO hakutaku.sources (source_type, title, raw_content)
            VALUES ('chat', %s, %s)
            RETURNING id;
            """,
            (f"diag_idle_timeout ({label})", "diagnostic content"),
        )
        row = cur.fetchone()
    repo._conn.commit()
    return str(row["id"])


def _safe_pid(conn) -> int | None:
    try:
        return conn.info.backend_pid
    except Exception:
        return None


def main() -> int:
    results: dict[str, dict] = {}
    console.rule(f"[bold cyan]diag_idle_timeout — sleep {SLEEP_SECONDS}s stress test[/]")

    # --- Open repo ---
    console.print("[dim]Opening repository...[/]")
    try:
        repo = get_repository()
    except Exception as exc:
        console.print(f"[red]Falha abrindo repositório:[/] {type(exc).__name__}: {exc}")
        return 1

    pid_initial = _safe_pid(repo._conn)
    info = repo._conn.info
    safe = {"host": info.host, "port": info.port, "dbname": info.dbname, "user": info.user}
    console.print(
        Panel(
            f"backend_pid: {pid_initial}\nserver_version: {info.server_version}\n"
            f"connection_info: {safe}",
            title="Connection info",
            border_style="cyan",
        )
    )

    # --- ETAPA 1 — INSERT inicial ---
    console.rule("[bold]ETAPA 1 — INSERT inicial[/]")
    t0 = time.monotonic()
    try:
        id1 = _direct_insert(repo, "etapa-1")
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[green]OK[/] id={id1} pid={pid_initial} ({dur}ms)")
        results["INSERT inicial"] = {"ok": True, "detail": f"id={id1} pid={pid_initial}", "ms": dur}
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[red]ERRO:[/] {type(exc).__name__}: {exc}")
        results["INSERT inicial"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:80]}",
            "ms": dur,
        }
        _print_summary(results)
        return 1

    # --- ETAPA 2 — sleep ---
    console.rule(f"[bold]ETAPA 2 — sleep {SLEEP_SECONDS}s (simula LLM call)[/]")
    console.print(f"[dim]Idle por {SLEEP_SECONDS}s a partir de agora...[/]")
    sleep_start = time.monotonic()
    time.sleep(SLEEP_SECONDS)
    actual_sleep = time.monotonic() - sleep_start
    console.print(f"[dim]...idle terminou ({actual_sleep:.1f}s)[/]")
    results["sleep"] = {"ok": True, "detail": f"slept_for={actual_sleep:.1f}s", "ms": int(actual_sleep * 1000)}

    # --- ETAPA 3 — INSERT naked (sem _ensure_alive) ---
    console.rule("[bold]ETAPA 3 — INSERT naked (sem passar por _ensure_alive)[/]")
    pid_pre_naked = _safe_pid(repo._conn)
    closed_pre_naked = repo._conn.closed
    console.print(f"[dim]pid={pid_pre_naked} closed={closed_pre_naked}[/]")
    naked_survived = False
    naked_exc_type: str | None = None
    t0 = time.monotonic()
    try:
        id_naked = _direct_insert(repo, "etapa-3-naked")
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[green]OK[/] id={id_naked} ({dur}ms) — conexão sobreviveu ao idle")
        naked_survived = True
        results["INSERT naked"] = {
            "ok": True,
            "detail": f"id={id_naked} | conn sobreviveu",
            "ms": dur,
        }
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        dur = int((time.monotonic() - t0) * 1000)
        naked_exc_type = type(exc).__name__
        console.print(
            f"[yellow]ESPERADO/ESPERÁVEL — naked INSERT falhou:[/] {naked_exc_type}"
        )
        console.print(f"[dim]{str(exc)[:200]}[/]")
        console.print(f"[dim]conn.closed agora: {repo._conn.closed}[/]")
        # tentamos rollback defensivamente — não deve quebrar mais.
        try:
            repo._conn.rollback()
        except Exception:
            pass
        results["INSERT naked"] = {
            "ok": False,
            "detail": f"{naked_exc_type} | conn died after {SLEEP_SECONDS}s idle",
            "ms": dur,
        }
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[red]ERRO INESPERADO:[/] {type(exc).__name__}: {exc}")
        results["INSERT naked"] = {
            "ok": False,
            "detail": f"unexpected: {type(exc).__name__}",
            "ms": dur,
        }

    # --- ETAPA 4 — _ensure_alive() ---
    console.rule("[bold]ETAPA 4 — _ensure_alive() explícito[/]")
    pid_pre_ensure = _safe_pid(repo._conn)
    t0 = time.monotonic()
    try:
        repo._ensure_alive()
        dur = int((time.monotonic() - t0) * 1000)
        pid_post_ensure = _safe_pid(repo._conn)
        pid_changed = pid_pre_ensure != pid_post_ensure
        console.print(
            f"[green]OK[/] ({dur}ms) pid_pre={pid_pre_ensure} → pid_post={pid_post_ensure} "
            f"{'[bold yellow](pid mudou — reabriu)[/]' if pid_changed else '[dim](pid igual — conexão estava viva)[/]'}"
        )
        results["_ensure_alive"] = {
            "ok": True,
            "detail": f"pid_pre={pid_pre_ensure} pid_post={pid_post_ensure} reopened={pid_changed}",
            "ms": dur,
        }
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[red]ERRO em _ensure_alive:[/] {type(exc).__name__}: {exc}")
        results["_ensure_alive"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:80]}",
            "ms": dur,
        }

    # --- ETAPA 5 — INSERT guarded (após _ensure_alive) ---
    console.rule("[bold]ETAPA 5 — INSERT após _ensure_alive[/]")
    t0 = time.monotonic()
    try:
        id_guarded = _direct_insert(repo, "etapa-5-guarded")
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[green]OK[/] id={id_guarded} ({dur}ms)")
        results["INSERT guarded"] = {
            "ok": True,
            "detail": f"id={id_guarded} | recuperação OK",
            "ms": dur,
        }
    except Exception as exc:
        dur = int((time.monotonic() - t0) * 1000)
        console.print(f"[red]ERRO:[/] {type(exc).__name__}: {exc}")
        results["INSERT guarded"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:80]}",
            "ms": dur,
        }

    _print_summary(results)

    # --- Exit code logic ---
    naked_ok = results.get("INSERT naked", {}).get("ok", False)
    guarded_ok = results.get("INSERT guarded", {}).get("ok", False)
    pid_changed = "reopened=True" in results.get("_ensure_alive", {}).get("detail", "")

    if naked_ok and guarded_ok:
        console.print(
            f"[bold green]Cenário A:[/] conexão sobreviveu {SLEEP_SECONDS}s de idle "
            "sem precisar de recovery. Keepalives + setup atual estão suficientes "
            "para esse intervalo. (Idle timeout não foi reprodutível com este setup.)"
        )
        return 0
    if not naked_ok and guarded_ok and pid_changed:
        console.print(
            f"[bold green]Cenário B (tese confirmada):[/] conexão MORREU após "
            f"{SLEEP_SECONDS}s idle (naked falhou). _ensure_alive DETECTOU e "
            "reabriu (pid mudou). INSERT subsequente passou. **O fix funciona.**"
        )
        return 0
    if guarded_ok:
        console.print(
            "[bold yellow]Cenário misto:[/] guarded passou mas evidência ambígua. "
            "Verifique manualmente os pids antes/depois e o log do _ensure_alive."
        )
        return 0
    console.print(
        "[bold red]Cenário ruim:[/] mesmo após _ensure_alive, INSERT falhou. "
        "Investigar conectividade base ou role permissions antes de prosseguir."
    )
    return 1


def _print_summary(results: dict[str, dict]) -> None:
    console.rule("[bold cyan]Resumo[/]")
    table = Table(title="diag_idle_timeout — resumo", header_style="bold cyan")
    table.add_column("Etapa")
    table.add_column("Resultado", justify="center")
    table.add_column("Detalhe")
    table.add_column("ms", justify="right")
    for etapa in [
        "INSERT inicial",
        "sleep",
        "INSERT naked",
        "_ensure_alive",
        "INSERT guarded",
    ]:
        r = results.get(etapa)
        if r is None:
            mark, detail, ms = "[yellow]—[/]", "(skipped)", "0"
        elif r["ok"]:
            mark, detail, ms = "[green]OK[/]", r["detail"][:90], str(r["ms"])
        else:
            mark, detail, ms = "[red]ERRO[/]", r["detail"][:90], str(r["ms"])
        table.add_row(etapa, mark, detail, ms)
    console.print(table)


if __name__ == "__main__":
    raise SystemExit(main())
