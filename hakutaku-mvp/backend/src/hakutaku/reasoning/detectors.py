"""Detectores de padrões/sinais sobre o grafo (Fase 5).

Cada detector é uma função pura `(repository) -> list[Finding]`. Findings
viram input do gerador de propostas. Os detectores fazem queries SQL diretas
porque (a) precisam de operadores temporais/agregações que repository.py não
expõe, e (b) cada detector tem uma forma específica de evidência que não
generaliza bem em método de repo.

6 detectores cobrindo o desafio:

1. `detect_orphan_tasks` — Tasks ativas sem owner/assignee.
2. `detect_escalating_risks` — Riscos high/critical em aberto + risco com
   severity escalada via attribute_changed.
3. `detect_overdue_tasks` — Tasks com deadline < now() não fechadas.
4. `detect_unanswered_questions` — OpenQuestions abertas há mais de N dias.
5. `detect_single_point_of_failure` — Pessoas com >= N tasks/projetos.
6. `detect_blocked_dependencies` — Tasks em state='blocked' ou com
   `depends_on` que aponta para Task overdue/blocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from hakutaku.graph.repository import GraphRepository
from hakutaku.schemas.proposals import EntityRef, Finding


_SEV_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}

_SCHEMA_SETUP = "SET search_path = hakutaku, extensions, public;"


# =====================================================================
# Helpers
# =====================================================================
def _entity_ref(row: dict[str, Any], type_: str) -> EntityRef:
    return EntityRef(
        id=UUID(str(row["id"])) if not isinstance(row["id"], UUID) else row["id"],
        name=row["canonical_name"],
        type=type_,
    )


# =====================================================================
# Detectores
# =====================================================================
def detect_orphan_tasks(repo: GraphRepository) -> list[Finding]:
    """Tasks em estado ativo (não done/cancelled) sem `assigned_to` nem `owns`."""
    repo._ensure_alive()
    with repo._conn.cursor() as cur:
        cur.execute(_SCHEMA_SETUP)
        cur.execute(
            """
            SELECT e.id, e.canonical_name, e.attributes, e.current_state,
                   e.first_seen_at
            FROM hakutaku.entities e
            WHERE e.type = 'Task'
              AND COALESCE(e.current_state->>'state', 'proposed')
                  NOT IN ('done', 'cancelled')
              AND NOT EXISTS (
                  SELECT 1 FROM hakutaku.relations r
                  WHERE r.from_entity = e.id AND r.relation_type = 'assigned_to'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM hakutaku.relations r
                  WHERE r.to_entity = e.id AND r.relation_type = 'owns'
              )
            ORDER BY e.first_seen_at ASC;
            """
        )
        rows = cur.fetchall()

    findings: list[Finding] = []
    for r in rows:
        priority = (r.get("attributes") or {}).get("priority", "medium")
        sev_map = {"low": 2, "medium": 3, "high": 4, "critical": 5}
        sev = sev_map.get(priority, 3)
        findings.append(
            Finding(
                detector="orphan_tasks",
                severity=sev,
                description=(
                    f"Task sem owner: '{r['canonical_name']}' "
                    f"(prioridade={priority}, state={(r.get('current_state') or {}).get('state', 'proposed')})."
                ),
                related_entities=[_entity_ref(r, "Task")],
                evidence={
                    "priority": priority,
                    "state": (r.get("current_state") or {}).get("state"),
                    "deadline": (r.get("attributes") or {}).get("deadline"),
                },
            )
        )
    return findings


def detect_escalating_risks(repo: GraphRepository) -> list[Finding]:
    """Combinação de dois sinais:

    (a) Riscos com severity high/critical em estado aberto (identified).
    (b) Riscos cuja severity escalou via attribute_changed (low→medium, etc.).
    """
    findings: list[Finding] = []

    # --- (a) high/critical abertos ---
    open_high = repo.find_open_risks(limit=20, severities=["high", "critical"])
    for risk in open_high:
        sev = (risk.attributes or {}).get("severity", "medium")
        findings.append(
            Finding(
                detector="escalating_risks",
                severity=5 if sev == "critical" else 4,
                description=(
                    f"Risco aberto com severidade {sev}: '{risk.canonical_name}'."
                ),
                related_entities=[
                    EntityRef(id=risk.id, name=risk.canonical_name, type="Risk")
                ],
                evidence={
                    "severity": sev,
                    "state": (risk.current_state or {}).get("state"),
                    "signal": "open_high_severity",
                },
            )
        )

    # --- (b) escalation events ---
    repo._ensure_alive()
    with repo._conn.cursor() as cur:
        cur.execute(_SCHEMA_SETUP)
        cur.execute(
            """
            SELECT ev.entity_id, ev.payload, ev.occurred_at,
                   ent.canonical_name, ent.attributes, ent.current_state
            FROM hakutaku.events ev
            JOIN hakutaku.entities ent ON ent.id = ev.entity_id
            WHERE ev.event_type = 'attribute_changed'
              AND ent.type = 'Risk'
              AND ev.payload->'diffs'->'severity' IS NOT NULL
            ORDER BY ev.occurred_at DESC;
            """
        )
        rows = cur.fetchall()

    seen_ids = {f.related_entities[0].id for f in findings if f.related_entities}
    for r in rows:
        diffs = (r.get("payload") or {}).get("diffs") or {}
        sev_diff = diffs.get("severity") or {}
        old = sev_diff.get("old") or "medium"
        new = sev_diff.get("new") or old
        if _SEV_ORDER.get(new, 0) <= _SEV_ORDER.get(old, 0):
            continue  # não escalou
        eid = UUID(str(r["entity_id"])) if not isinstance(r["entity_id"], UUID) else r["entity_id"]
        if eid in seen_ids:
            continue  # já registramos via sinal (a)
        sev_score = 5 if new == "critical" else (4 if new == "high" else 3)
        findings.append(
            Finding(
                detector="escalating_risks",
                severity=sev_score,
                description=(
                    f"Risco escalado de '{old}' para '{new}': "
                    f"'{r['canonical_name']}'."
                ),
                related_entities=[
                    EntityRef(id=eid, name=r["canonical_name"], type="Risk")
                ],
                evidence={
                    "old_severity": old,
                    "new_severity": new,
                    "signal": "severity_escalation",
                    "occurred_at": str(r["occurred_at"]),
                },
            )
        )
    return findings


def detect_overdue_tasks(repo: GraphRepository) -> list[Finding]:
    """Tasks com `attributes.deadline` < now() e state ativo."""
    repo._ensure_alive()
    with repo._conn.cursor() as cur:
        cur.execute(_SCHEMA_SETUP)
        cur.execute(
            """
            SELECT id, canonical_name, attributes, current_state
            FROM hakutaku.entities
            WHERE type = 'Task'
              AND attributes->>'deadline' IS NOT NULL
              AND COALESCE(current_state->>'state', 'proposed')
                  NOT IN ('done', 'cancelled')
              AND (
                  CASE WHEN attributes->>'deadline' ~ '^\\d{4}-\\d{2}-\\d{2}'
                       THEN (attributes->>'deadline')::timestamptz < now()
                       ELSE false
                  END
              )
            ORDER BY (attributes->>'deadline') ASC;
            """
        )
        rows = cur.fetchall()

    findings: list[Finding] = []
    for r in rows:
        deadline = (r.get("attributes") or {}).get("deadline")
        priority = (r.get("attributes") or {}).get("priority", "medium")
        sev_map = {"low": 3, "medium": 4, "high": 5, "critical": 5}
        findings.append(
            Finding(
                detector="overdue_tasks",
                severity=sev_map.get(priority, 4),
                description=(
                    f"Task atrasada: '{r['canonical_name']}' (deadline={deadline}, "
                    f"prioridade={priority})."
                ),
                related_entities=[_entity_ref(r, "Task")],
                evidence={
                    "deadline": deadline,
                    "priority": priority,
                    "state": (r.get("current_state") or {}).get("state"),
                },
            )
        )
    return findings


def detect_unanswered_questions(
    repo: GraphRepository, *, days_threshold: int = 7
) -> list[Finding]:
    """OpenQuestions em state='open' há mais de `days_threshold` dias."""
    repo._ensure_alive()
    with repo._conn.cursor() as cur:
        cur.execute(_SCHEMA_SETUP)
        cur.execute(
            """
            SELECT id, canonical_name, current_state, first_seen_at,
                   EXTRACT(EPOCH FROM (now() - first_seen_at)) / 86400.0 AS days_open
            FROM hakutaku.entities
            WHERE type = 'OpenQuestion'
              AND COALESCE(current_state->>'state', 'open') = 'open'
              AND first_seen_at < now() - (%s || ' days')::interval
            ORDER BY first_seen_at ASC;
            """,
            (str(days_threshold),),
        )
        rows = cur.fetchall()

    findings: list[Finding] = []
    for r in rows:
        days = float(r.get("days_open") or 0)
        # Aging: mais dias abertos → maior severidade.
        if days >= 30:
            sev = 5
        elif days >= 14:
            sev = 4
        elif days >= 7:
            sev = 3
        else:
            sev = 2
        findings.append(
            Finding(
                detector="unanswered_questions",
                severity=sev,
                description=(
                    f"Pergunta aberta há {days:.0f} dias sem resposta: "
                    f"'{r['canonical_name']}'."
                ),
                related_entities=[_entity_ref(r, "OpenQuestion")],
                evidence={
                    "days_open": round(days, 1),
                    "first_seen_at": str(r["first_seen_at"]),
                },
            )
        )
    return findings


def detect_single_point_of_failure(
    repo: GraphRepository, *, threshold: int = 3
) -> list[Finding]:
    """Pessoas com `assigned_to` ou `owns` em >= `threshold` itens não-fechados."""
    repo._ensure_alive()
    with repo._conn.cursor() as cur:
        cur.execute(_SCHEMA_SETUP)
        cur.execute(
            """
            WITH person_load AS (
                -- Tasks designadas para a pessoa (assigned_to: Task -> Person)
                SELECT r.to_entity AS person_id, r.from_entity AS item_id
                FROM hakutaku.relations r
                JOIN hakutaku.entities item ON item.id = r.from_entity
                WHERE r.relation_type = 'assigned_to'
                  AND COALESCE(item.current_state->>'state', 'proposed')
                      NOT IN ('done', 'cancelled')

                UNION

                -- Itens "owns" da pessoa (owns: Person -> Task/Project)
                SELECT r.from_entity AS person_id, r.to_entity AS item_id
                FROM hakutaku.relations r
                JOIN hakutaku.entities item ON item.id = r.to_entity
                WHERE r.relation_type = 'owns'
                  AND COALESCE(item.current_state->>'state', 'proposed')
                      NOT IN ('done', 'cancelled')
            ),
            agg AS (
                SELECT person_id, COUNT(DISTINCT item_id) AS load_count,
                       array_agg(DISTINCT item_id) AS item_ids
                FROM person_load
                GROUP BY person_id
                HAVING COUNT(DISTINCT item_id) >= %s
            )
            SELECT a.person_id, a.load_count, a.item_ids,
                   p.canonical_name AS person_name
            FROM agg a
            JOIN hakutaku.entities p ON p.id = a.person_id
            WHERE p.type = 'Person'
            ORDER BY a.load_count DESC;
            """,
            (threshold,),
        )
        rows = cur.fetchall()

    findings: list[Finding] = []
    for r in rows:
        person_id = UUID(str(r["person_id"])) if not isinstance(r["person_id"], UUID) else r["person_id"]
        item_ids = [
            UUID(str(i)) if not isinstance(i, UUID) else i
            for i in (r.get("item_ids") or [])
        ]
        load = int(r["load_count"])
        sev = 5 if load >= 6 else (4 if load >= 4 else 3)
        # Resolve nomes dos itens em uma query.
        item_refs: list[EntityRef] = [
            EntityRef(id=person_id, name=r["person_name"], type="Person")
        ]
        if item_ids:
            with repo._conn.cursor() as cur2:
                cur2.execute(_SCHEMA_SETUP)
                cur2.execute(
                    """
                    SELECT id, type, canonical_name FROM hakutaku.entities
                    WHERE id = ANY(%s);
                    """,
                    ([str(i) for i in item_ids],),
                )
                for row2 in cur2.fetchall():
                    iid = UUID(str(row2["id"])) if not isinstance(row2["id"], UUID) else row2["id"]
                    item_refs.append(
                        EntityRef(id=iid, name=row2["canonical_name"], type=row2["type"])
                    )
        findings.append(
            Finding(
                detector="single_point_of_failure",
                severity=sev,
                description=(
                    f"Concentração de carga: {r['person_name']} é responsável "
                    f"por {load} itens em aberto."
                ),
                related_entities=item_refs,
                evidence={"load_count": load},
            )
        )
    return findings


def detect_blocked_dependencies(repo: GraphRepository) -> list[Finding]:
    """Tasks em state='blocked' ou com `depends_on` para Task overdue/blocked."""
    repo._ensure_alive()
    with repo._conn.cursor() as cur:
        cur.execute(_SCHEMA_SETUP)
        cur.execute(
            """
            SELECT e.id, e.canonical_name, e.attributes, e.current_state
            FROM hakutaku.entities e
            WHERE e.type = 'Task'
              AND e.current_state->>'state' = 'blocked';
            """
        )
        blocked_rows = cur.fetchall()

        cur.execute(
            """
            SELECT t.id AS task_id, t.canonical_name AS task_name,
                   d.id AS dep_id, d.canonical_name AS dep_name,
                   d.current_state->>'state' AS dep_state,
                   d.attributes->>'deadline' AS dep_deadline
            FROM hakutaku.entities t
            JOIN hakutaku.relations r
              ON r.from_entity = t.id AND r.relation_type = 'depends_on'
            JOIN hakutaku.entities d ON d.id = r.to_entity
            WHERE t.type = 'Task' AND d.type = 'Task'
              AND COALESCE(t.current_state->>'state', 'proposed')
                  NOT IN ('done', 'cancelled')
              AND (
                  d.current_state->>'state' = 'blocked'
                  OR (
                      d.attributes->>'deadline' IS NOT NULL
                      AND d.attributes->>'deadline' ~ '^\\d{4}-\\d{2}-\\d{2}'
                      AND (d.attributes->>'deadline')::timestamptz < now()
                      AND COALESCE(d.current_state->>'state', 'proposed')
                          NOT IN ('done', 'cancelled')
                  )
              );
            """
        )
        dep_rows = cur.fetchall()

    findings: list[Finding] = []

    for r in blocked_rows:
        findings.append(
            Finding(
                detector="blocked_dependencies",
                severity=4,
                description=f"Task em state='blocked': '{r['canonical_name']}'.",
                related_entities=[_entity_ref(r, "Task")],
                evidence={"signal": "task_state_blocked"},
            )
        )

    for r in dep_rows:
        task_id = UUID(str(r["task_id"])) if not isinstance(r["task_id"], UUID) else r["task_id"]
        dep_id = UUID(str(r["dep_id"])) if not isinstance(r["dep_id"], UUID) else r["dep_id"]
        findings.append(
            Finding(
                detector="blocked_dependencies",
                severity=4,
                description=(
                    f"'{r['task_name']}' depende de '{r['dep_name']}' que está "
                    f"{r['dep_state'] or 'overdue'}."
                ),
                related_entities=[
                    EntityRef(id=task_id, name=r["task_name"], type="Task"),
                    EntityRef(id=dep_id, name=r["dep_name"], type="Task"),
                ],
                evidence={
                    "dep_state": r["dep_state"],
                    "dep_deadline": r["dep_deadline"],
                    "signal": "depends_on_stuck",
                },
            )
        )

    return findings


# =====================================================================
# Registry
# =====================================================================
ALL_DETECTORS = [
    detect_orphan_tasks,
    detect_escalating_risks,
    detect_overdue_tasks,
    detect_unanswered_questions,
    detect_single_point_of_failure,
    detect_blocked_dependencies,
]


def run_all_detectors(repo: GraphRepository) -> list[Finding]:
    """Roda todos os detectores e devolve a lista concatenada de findings."""
    findings: list[Finding] = []
    for fn in ALL_DETECTORS:
        try:
            findings.extend(fn(repo))
        except Exception as exc:
            # Detector individual falha não derruba o ciclo — log e segue.
            print(
                f"[WARNING][reasoning] detector {fn.__name__} falhou: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
    return findings
