"""Camada de acesso ao Supabase (Postgres + pgvector).

Conexão via `psycopg` (síncrono, simples). Toda query usa `SET search_path =
hakutaku, extensions, public` para que a aplicação não precise qualificar
nomes de tabela manualmente.

Princípios:
- Toda escrita que muda o grafo grava também um `event` na mesma transação
  (event sourcing — D004).
- `insert_relation` é idempotente via `ON CONFLICT DO NOTHING` (UNIQUE
  (from_entity, to_entity, relation_type) já existe na migration).
- Embeddings são serializados como literal `[v1,v2,...]` aceito pelo `pgvector`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hakutaku.config import get_settings
from hakutaku.schemas import Entity, EventType, RelationType


_log = logging.getLogger(__name__)
# Garante que o WARNING aparece mesmo sem configuração explícita do app.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


# =====================================================================
# DTOs
# =====================================================================
@dataclass(frozen=True)
class EntityRecord:
    """Linha da tabela `entities` retornada de queries."""

    id: UUID
    type: str
    canonical_name: str
    aliases: list[str]
    attributes: dict[str, Any]
    current_state: dict[str, Any]
    confidence: float

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> EntityRecord:
        return cls(
            id=row["id"],
            type=row["type"],
            canonical_name=row["canonical_name"],
            aliases=list(row.get("aliases") or []),
            attributes=dict(row.get("attributes") or {}),
            current_state=dict(row.get("current_state") or {}),
            confidence=float(row.get("confidence", 1.0)),
        )


@dataclass(frozen=True)
class EntityCandidate:
    """Candidato em busca de similaridade — guarda trgm e cosine brutos.

    O `combined_score` é calculado pelo resolver com pesos que dependem do
    `type` da entidade nova (paráfrases do extrator pesam diferente para
    Risk/Decision/Task vs Person/Client). Manter os scores brutos aqui
    permite que log/auditoria preserve a origem de cada eixo.
    """

    record: EntityRecord
    trgm_score: float
    cosine_score: float

    def combined(self, *, cosine_weight: float, trgm_weight: float) -> float:
        return cosine_weight * self.cosine_score + trgm_weight * self.trgm_score


# =====================================================================
# Helpers
# =====================================================================
def _vector_literal(vec: list[float]) -> str:
    """Serializa float list para o literal aceito por pgvector: `[v1,v2,...]`."""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


def _safe_backend_pid(conn: psycopg.Connection) -> int | None:
    """Lê backend_pid sem quebrar se a conexão já estiver indisponível."""
    try:
        return conn.info.backend_pid
    except Exception:
        return None


def _parse_vector(value: Any) -> list[float] | None:
    """Parse de retorno do pgvector. Sem o adapter pgvector-python registrado,
    `SELECT embedding` volta como string `"[v1,v2,...]"` ou pode ser None.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [float(x) for x in value]
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        return []
    try:
        return [float(x) for x in s.split(",")]
    except ValueError:
        return None


def _diff_attributes(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Retorna `{attr: {old, new}}` apenas para campos que o caller passou em `new`.

    Itera só sobre `new.keys()`: se uma chave existe em `old` mas não em `new`,
    significa que o caller (`_to_attributes_for_update` filtra None) decidiu
    não mexer naquele atributo — gravar `attribute_changed` com `new=null`
    seria mentira sobre o estado real do entity, já que `merged_attributes`
    preserva o valor antigo.
    """
    diffs: dict[str, dict[str, Any]] = {}
    for k, nv in new.items():
        ov = old.get(k)
        if ov != nv:
            diffs[k] = {"old": ov, "new": nv}
    return diffs


def _entity_to_attributes(entity: Entity) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separa `(attributes, current_state)` a partir do Pydantic.

    `state` (quando existe) vai para `current_state`; o resto vai para `attributes`.
    Campos meta (id, canonical_name, aliases, source_excerpt, confidence, type) não
    entram em nenhum dos dois — são colunas próprias.
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
            attributes[k] = v
    return attributes, current_state


# =====================================================================
# Repository
# =====================================================================
class GraphRepository:
    """Acesso síncrono ao Postgres do Supabase via `psycopg`.

    Cria uma conexão long-lived. `close()` deve ser chamado no shutdown — em
    scripts curtos (Fase 3) `with get_repository()` resolve.
    """

    SCHEMA_SETUP = "SET search_path = hakutaku, extensions, public;"

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError(
                "SUPABASE_DB_URL ausente — preencha .env com a connection string do Postgres."
            )
        self._dsn = dsn
        self._conn = self._open_connection()

    def _open_connection(self) -> psycopg.Connection:
        # TCP keepalives são essenciais com Supabase: sem eles, uma chamada LLM
        # longa (>30s) deixa o socket idle, o pooler mata, e a próxima escrita
        # falha com `server closed the connection unexpectedly`.
        conn = psycopg.connect(
            self._dsn,
            autocommit=False,
            row_factory=dict_row,
            keepalives=1,
            keepalives_idle=20,
            keepalives_interval=10,
            keepalives_count=3,
        )
        with conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
        conn.commit()
        return conn

    def _ensure_alive(self) -> None:
        """Ping leve; reabre a conexão se cair. Chamado no topo das escritas.

        Loga via `_log.warning` quando reabre — evidência se o bug de
        idle-timeout/pooler-kill voltar.
        """
        if self._conn.closed:
            old_pid = _safe_backend_pid(self._conn)
            self._conn = self._open_connection()
            _log.warning(
                "[_ensure_alive] conexão estava morta (closed=True), reabrindo "
                "(old_pid=%s new_pid=%s)",
                old_pid,
                self._conn.info.backend_pid,
            )
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1;")
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            old_pid = _safe_backend_pid(self._conn)
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = self._open_connection()
            _log.warning(
                "[_ensure_alive] conexão estava morta (SELECT 1 falhou: %s), "
                "reabrindo (old_pid=%s new_pid=%s)",
                type(exc).__name__,
                old_pid,
                self._conn.info.backend_pid,
            )

    def close(self) -> None:
        if not self._conn.closed:
            self._conn.close()

    def __enter__(self) -> GraphRepository:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------
    def upsert_source(
        self,
        *,
        source_id: UUID,
        source_type: str,
        title: str,
        raw_content: str,
        metadata: dict[str, Any] | None,
        occurred_at: datetime | None,
    ) -> UUID:
        """Insere ou atualiza um `source` por id. Retorna o uuid persistido.

        Se `source_id` já existe, atualiza apenas `processed_at` para refletir reprocessamento.
        """
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                INSERT INTO hakutaku.sources
                    (id, source_type, title, raw_content, metadata, occurred_at, processed_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE
                    SET processed_at = now(),
                        metadata     = EXCLUDED.metadata,
                        title        = EXCLUDED.title
                RETURNING id;
                """,
                (
                    str(source_id),
                    source_type,
                    title,
                    raw_content,
                    Jsonb(metadata or {}),
                    occurred_at,
                ),
            )
            row = cur.fetchone()
        self._conn.commit()
        assert row is not None
        return row["id"]

    # ------------------------------------------------------------------
    # entities — search & write
    # ------------------------------------------------------------------
    def find_similar_entities(
        self,
        *,
        name: str,
        entity_type: str,
        embedding: list[float],
        top_k: int = 10,
        min_trgm: float = 0.2,
        cosine_weight: float = 0.6,
        trgm_weight: float = 0.4,
    ) -> list[EntityCandidate]:
        """Busca candidatos por similaridade lexical (`pg_trgm`) + cosine (`pgvector`).

        Filtra por tipo (blocking forte: nunca matchamos Person com Project).
        Pesos do `ORDER BY` são parametrizáveis para que o resolver consiga
        priorizar embedding em tipos parafraseados (Risk/Decision/Task) e
        manter trgm relevante em nomes próprios (Person/Client).

        `min_trgm` desce para 0.2 — nomes parafraseados como
        "Risco de churn TechNova por SLA não cumprido" vs "Churn da TechNova"
        compartilham poucos trigramas e ficavam de fora com 0.3.
        """
        vec_literal = _vector_literal(embedding)
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, type, canonical_name, aliases, attributes, current_state,
                       confidence,
                       similarity(canonical_name, %(name)s) AS trgm_score,
                       1 - (embedding <=> %(vec)s::vector) AS cosine_score
                FROM hakutaku.entities
                WHERE type = %(type)s
                  AND (
                       similarity(canonical_name, %(name)s) > %(min_trgm)s
                    OR %(name)s = ANY(aliases)
                    OR embedding <=> %(vec)s::vector < 0.5
                  )
                ORDER BY (
                    %(cw)s * (1 - (embedding <=> %(vec)s::vector))
                  + %(tw)s * similarity(canonical_name, %(name)s)
                ) DESC
                LIMIT %(k)s;
                """,
                {
                    "name": name,
                    "type": entity_type,
                    "vec": vec_literal,
                    "min_trgm": min_trgm,
                    "k": top_k,
                    "cw": cosine_weight,
                    "tw": trgm_weight,
                },
            )
            rows = cur.fetchall()

        candidates: list[EntityCandidate] = []
        for r in rows:
            candidates.append(
                EntityCandidate(
                    record=EntityRecord.from_row(r),
                    trgm_score=float(r.get("trgm_score") or 0.0),
                    cosine_score=float(r.get("cosine_score") or 0.0),
                )
            )
        return candidates

    def insert_entity(
        self,
        *,
        entity: Entity,
        embedding: list[float],
        source_id: UUID,
        source_excerpt: str,
        occurred_at: datetime,
    ) -> UUID:
        """Cria entidade nova e dispara evento `entity_created` na mesma transação.

        Retorna o UUID gerado pelo Postgres.
        """
        attributes, current_state = _entity_to_attributes(entity)
        vec_literal = _vector_literal(embedding)
        canonical = entity.canonical_name
        aliases = list(entity.aliases or [])

        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                INSERT INTO hakutaku.entities
                    (type, canonical_name, aliases, attributes, current_state,
                     embedding, confidence)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                RETURNING id;
                """,
                (
                    entity.type,
                    canonical,
                    aliases,
                    Jsonb(attributes),
                    Jsonb(current_state),
                    vec_literal,
                    float(entity.confidence),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            new_id: UUID = row["id"]

            cur.execute(
                """
                INSERT INTO hakutaku.events
                    (entity_id, event_type, payload, source_id, source_excerpt, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    str(new_id),
                    EventType.ENTITY_CREATED.value,
                    Jsonb(
                        {
                            "type": entity.type,
                            "canonical_name": canonical,
                            "attributes": attributes,
                            "current_state": current_state,
                            "confidence": float(entity.confidence),
                        }
                    ),
                    str(source_id),
                    source_excerpt,
                    occurred_at,
                ),
            )
        self._conn.commit()
        return new_id

    def update_entity(
        self,
        *,
        entity_id: UUID,
        new_attributes: dict[str, Any],
        new_current_state: dict[str, Any],
        new_aliases: list[str],
        new_canonical_name: str | None,
        source_id: UUID,
        source_excerpt: str,
        occurred_at: datetime,
    ) -> None:
        """Aplica diff e grava um evento por mudança detectada.

        Comportamento:
        - Diferenças em `attributes` viram um único evento `attribute_changed` agregado.
        - Mudança em `current_state.state` vira `status_changed` (semântica distinta).
        - Aliases novos são adicionados (nunca removidos).
        - `canonical_name` só é trocado se o novo for mais longo (heurística simples
          de "nome mais completo gana"). Não gera evento próprio.
        """
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, type, canonical_name, aliases, attributes, current_state, confidence
                FROM hakutaku.entities WHERE id = %s;
                """,
                (str(entity_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"Entity {entity_id} não encontrada.")

            current = EntityRecord.from_row(row)
            attr_diffs = _diff_attributes(current.attributes, new_attributes)
            old_state = current.current_state.get("state")
            target_state = new_current_state.get("state", old_state)
            state_changed = old_state != target_state and target_state is not None

            merged_attributes = {**current.attributes, **new_attributes}
            merged_state = {**current.current_state, **new_current_state}
            merged_aliases = list(dict.fromkeys([*current.aliases, *new_aliases]))

            updated_canonical = current.canonical_name
            if new_canonical_name and len(new_canonical_name) > len(current.canonical_name):
                # Adiciona o nome antigo como alias para preservar rastreabilidade.
                if current.canonical_name not in merged_aliases:
                    merged_aliases.append(current.canonical_name)
                updated_canonical = new_canonical_name

            cur.execute(
                """
                UPDATE hakutaku.entities
                   SET canonical_name  = %s,
                       aliases         = %s,
                       attributes      = %s,
                       current_state   = %s,
                       last_updated_at = now()
                 WHERE id = %s;
                """,
                (
                    updated_canonical,
                    merged_aliases,
                    Jsonb(merged_attributes),
                    Jsonb(merged_state),
                    str(entity_id),
                ),
            )

            if attr_diffs:
                cur.execute(
                    """
                    INSERT INTO hakutaku.events
                        (entity_id, event_type, payload, source_id, source_excerpt, occurred_at)
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (
                        str(entity_id),
                        EventType.ATTRIBUTE_CHANGED.value,
                        Jsonb({"diffs": attr_diffs}),
                        str(source_id),
                        source_excerpt,
                        occurred_at,
                    ),
                )

            if state_changed:
                cur.execute(
                    """
                    INSERT INTO hakutaku.events
                        (entity_id, event_type, payload, source_id, source_excerpt, occurred_at)
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (
                        str(entity_id),
                        EventType.STATUS_CHANGED.value,
                        Jsonb(
                            {
                                "old_status": old_state,
                                "new_status": target_state,
                                "trigger": "extraction",
                            }
                        ),
                        str(source_id),
                        source_excerpt,
                        occurred_at,
                    ),
                )

        self._conn.commit()

    def insert_event(
        self,
        *,
        entity_id: UUID,
        event_type: EventType,
        payload: dict[str, Any],
        source_id: UUID | None,
        source_excerpt: str | None,
        occurred_at: datetime,
    ) -> UUID:
        """Insere evento avulso (uso para `entity_merged` e similares)."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                INSERT INTO hakutaku.events
                    (entity_id, event_type, payload, source_id, source_excerpt, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    str(entity_id),
                    event_type.value,
                    Jsonb(payload),
                    str(source_id) if source_id else None,
                    source_excerpt,
                    occurred_at,
                ),
            )
            row = cur.fetchone()
        self._conn.commit()
        assert row is not None
        return row["id"]

    # ------------------------------------------------------------------
    # relations
    # ------------------------------------------------------------------
    def insert_relation(
        self,
        *,
        from_entity: UUID,
        to_entity: UUID,
        relation_type: RelationType,
        attributes: dict[str, Any] | None,
        source_id: UUID | None,
        confidence: float,
        source_excerpt: str | None,
        occurred_at: datetime,
    ) -> UUID | None:
        """Idempotente via UNIQUE (from_entity, to_entity, relation_type).

        Retorna o uuid da relação criada (nova) ou `None` se a aresta já existia.
        Em caso de criação, dispara `relation_added` no entity de origem.
        """
        if from_entity == to_entity:
            return None

        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                INSERT INTO hakutaku.relations
                    (from_entity, to_entity, relation_type, attributes, source_id, confidence)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (from_entity, to_entity, relation_type) DO NOTHING
                RETURNING id;
                """,
                (
                    str(from_entity),
                    str(to_entity),
                    relation_type.value,
                    Jsonb(attributes or {}),
                    str(source_id) if source_id else None,
                    float(confidence),
                ),
            )
            row = cur.fetchone()
            new_id: UUID | None = row["id"] if row else None

            if new_id is not None:
                cur.execute(
                    """
                    INSERT INTO hakutaku.events
                        (entity_id, event_type, payload, source_id, source_excerpt, occurred_at)
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (
                        str(from_entity),
                        EventType.RELATION_ADDED.value,
                        Jsonb(
                            {
                                "from_entity": str(from_entity),
                                "to_entity": str(to_entity),
                                "relation_type": relation_type.value,
                                "attributes": attributes or {},
                            }
                        ),
                        str(source_id) if source_id else None,
                        source_excerpt,
                        occurred_at,
                    ),
                )
        self._conn.commit()
        return new_id

    # ------------------------------------------------------------------
    # leitura
    # ------------------------------------------------------------------
    def get_entity_history(self, entity_id: UUID) -> list[dict[str, Any]]:
        """Eventos de uma entidade em ordem cronológica."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, entity_id, event_type, payload, source_id,
                       source_excerpt, occurred_at, recorded_at
                FROM hakutaku.events
                WHERE entity_id = %s
                ORDER BY occurred_at ASC, recorded_at ASC;
                """,
                (str(entity_id),),
            )
            return list(cur.fetchall())

    def find_entities_by_name(
        self, name: str, entity_type: str | None = None, limit: int = 5
    ) -> list[EntityRecord]:
        """Busca por nome canônico ou alias (case-insensitive). Útil para validação."""
        sql = (
            """
            SELECT id, type, canonical_name, aliases, attributes, current_state, confidence
            FROM hakutaku.entities
            WHERE (LOWER(canonical_name) = LOWER(%(name)s) OR %(name)s = ANY(aliases))
            """
        )
        params: dict[str, Any] = {"name": name, "limit": limit}
        if entity_type:
            sql += " AND type = %(type)s"
            params["type"] = entity_type
        sql += " LIMIT %(limit)s;"

        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(sql, params)
            return [EntityRecord.from_row(r) for r in cur.fetchall()]

    def get_full_graph(self) -> dict[str, Any]:
        """Snapshot serializável do grafo inteiro — para visualização e auditoria."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, type, canonical_name, aliases, attributes, current_state,
                       confidence, first_seen_at, last_updated_at
                FROM hakutaku.entities
                ORDER BY first_seen_at ASC;
                """
            )
            entities = list(cur.fetchall())

            cur.execute(
                """
                SELECT id, from_entity, to_entity, relation_type, attributes,
                       source_id, confidence, created_at
                FROM hakutaku.relations
                ORDER BY created_at ASC;
                """
            )
            relations = list(cur.fetchall())

        snapshot = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "entities": [
                {
                    "id": str(e["id"]),
                    "type": e["type"],
                    "canonical_name": e["canonical_name"],
                    "aliases": list(e.get("aliases") or []),
                    "attributes": dict(e.get("attributes") or {}),
                    "current_state": dict(e.get("current_state") or {}),
                    "confidence": float(e.get("confidence") or 1.0),
                    "first_seen_at": e["first_seen_at"].isoformat() if e["first_seen_at"] else None,
                    "last_updated_at": (
                        e["last_updated_at"].isoformat() if e["last_updated_at"] else None
                    ),
                }
                for e in entities
            ],
            "relations": [
                {
                    "id": str(r["id"]),
                    "from_entity": str(r["from_entity"]),
                    "to_entity": str(r["to_entity"]),
                    "relation_type": r["relation_type"],
                    "attributes": dict(r.get("attributes") or {}),
                    "source_id": str(r["source_id"]) if r.get("source_id") else None,
                    "confidence": float(r.get("confidence") or 1.0),
                }
                for r in relations
            ],
        }
        return snapshot

    # ------------------------------------------------------------------
    # memory / context retrieval (Fase 4)
    # ------------------------------------------------------------------
    def find_entities_by_doc_embedding(
        self,
        *,
        embedding: list[float],
        top_k: int = 15,
        exclude_types: list[str] | None = None,
    ) -> list[tuple[EntityRecord, float]]:
        """Top-K entidades cross-type por cosine similarity ao embedding do documento.

        Retorna `[(EntityRecord, cosine_score)]` ordenado por similaridade decrescente.
        `exclude_types` permite, por exemplo, esconder `BehavioralPattern` em contextos
        de extração (LLM não deve ver padrões internos do sistema).
        """
        vec_literal = _vector_literal(embedding)
        sql = (
            """
            SELECT id, type, canonical_name, aliases, attributes, current_state, confidence,
                   1 - (embedding <=> %(vec)s::vector) AS cosine_score
            FROM hakutaku.entities
            WHERE embedding IS NOT NULL
            """
        )
        params: dict[str, Any] = {"vec": vec_literal, "k": top_k}
        if exclude_types:
            sql += " AND type <> ALL(%(excl)s)"
            params["excl"] = list(exclude_types)
        sql += " ORDER BY embedding <=> %(vec)s::vector ASC LIMIT %(k)s;"

        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(sql, params)
            rows = cur.fetchall()

        return [
            (EntityRecord.from_row(r), float(r.get("cosine_score") or 0.0))
            for r in rows
        ]

    def recent_active_entities(self, *, limit: int = 5) -> list[EntityRecord]:
        """Top-N entidades por `last_updated_at DESC` — sinal de "o que está vivo agora"."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, type, canonical_name, aliases, attributes, current_state, confidence
                FROM hakutaku.entities
                ORDER BY last_updated_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            return [EntityRecord.from_row(r) for r in cur.fetchall()]

    def find_open_questions(self, *, limit: int = 10) -> list[EntityRecord]:
        """OpenQuestions com state='open' (default — ausência também conta como aberta)."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, type, canonical_name, aliases, attributes, current_state, confidence
                FROM hakutaku.entities
                WHERE type = 'OpenQuestion'
                  AND COALESCE(current_state->>'state', 'open') = 'open'
                ORDER BY first_seen_at ASC
                LIMIT %s;
                """,
                (limit,),
            )
            return [EntityRecord.from_row(r) for r in cur.fetchall()]

    def find_open_risks(
        self,
        *,
        limit: int = 5,
        severities: list[str] | None = None,
    ) -> list[EntityRecord]:
        """Riscos não-mitigados/aceitos. `severities` filtra (default: high+critical)."""
        sev = severities or ["high", "critical"]
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, type, canonical_name, aliases, attributes, current_state, confidence
                FROM hakutaku.entities
                WHERE type = 'Risk'
                  AND COALESCE(attributes->>'severity', 'medium') = ANY(%s)
                  AND COALESCE(current_state->>'state', 'identified')
                      NOT IN ('mitigated', 'accepted')
                ORDER BY first_seen_at DESC
                LIMIT %s;
                """,
                (sev, limit),
            )
            return [EntityRecord.from_row(r) for r in cur.fetchall()]

    def find_active_projects(self, *, limit: int = 5) -> list[EntityRecord]:
        """Projects com state='active' (default quando ausente)."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, type, canonical_name, aliases, attributes, current_state, confidence
                FROM hakutaku.entities
                WHERE type = 'Project'
                  AND COALESCE(current_state->>'state', 'active') = 'active'
                ORDER BY first_seen_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            return [EntityRecord.from_row(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # cross-source linking (Fase 4)
    # ------------------------------------------------------------------
    def list_open_questions_with_embeddings(
        self,
        *,
        exclude_already_answered: bool = True,
    ) -> list[tuple[EntityRecord, list[float], datetime]]:
        """Para o cross-linker: questões abertas + embedding parseado + first_seen_at.

        Por default exclui questões que já têm uma aresta `answers` apontando
        para elas — cross-linker não deve gerar `answers` duplicado quando a
        extração já capturou um na mesma fonte.
        """
        sql = (
            """
            SELECT e.id, e.type, e.canonical_name, e.aliases, e.attributes,
                   e.current_state, e.confidence, e.first_seen_at,
                   e.embedding::text AS embedding_text
            FROM hakutaku.entities e
            WHERE e.type = 'OpenQuestion'
              AND e.embedding IS NOT NULL
              AND COALESCE(e.current_state->>'state', 'open') = 'open'
            """
        )
        if exclude_already_answered:
            sql += (
                "  AND NOT EXISTS (\n"
                "      SELECT 1 FROM hakutaku.relations r\n"
                "      WHERE r.relation_type = 'answers' AND r.to_entity = e.id\n"
                "  )\n"
            )
        sql += " ORDER BY e.first_seen_at ASC;"

        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(sql)
            rows = cur.fetchall()

        out: list[tuple[EntityRecord, list[float], datetime]] = []
        for r in rows:
            emb = _parse_vector(r.get("embedding_text"))
            if emb is None:
                continue
            out.append((EntityRecord.from_row(r), emb, r["first_seen_at"]))
        return out

    def find_decision_candidates_for_question(
        self,
        *,
        question_first_seen_at: datetime,
        question_embedding: list[float],
        top_k: int = 3,
        min_cosine: float = 0.5,
    ) -> list[tuple[EntityRecord, float, datetime]]:
        """Decisions criadas APÓS uma OpenQuestion, ordenadas por cosine similarity.

        Filtragem temporal estrita: só consideramos `first_seen_at >= question.first_seen_at`
        (uma decisão tomada antes da pergunta não pode respondê-la).
        """
        vec_literal = _vector_literal(question_embedding)
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT id, type, canonical_name, aliases, attributes, current_state,
                       confidence, first_seen_at,
                       1 - (embedding <=> %(vec)s::vector) AS cosine_score
                FROM hakutaku.entities
                WHERE type = 'Decision'
                  AND embedding IS NOT NULL
                  AND first_seen_at >= %(after)s
                  AND 1 - (embedding <=> %(vec)s::vector) >= %(min_sim)s
                ORDER BY embedding <=> %(vec)s::vector ASC
                LIMIT %(k)s;
                """,
                {
                    "vec": vec_literal,
                    "after": question_first_seen_at,
                    "min_sim": min_cosine,
                    "k": top_k,
                },
            )
            rows = cur.fetchall()

        return [
            (
                EntityRecord.from_row(r),
                float(r.get("cosine_score") or 0.0),
                r["first_seen_at"],
            )
            for r in rows
        ]

    def get_entity_source_excerpt(self, entity_id: UUID) -> str | None:
        """Source_excerpt do `entity_created` event — útil para reapresentar
        contexto da entidade fora da fonte original (ex.: cross-linker).
        """
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT source_excerpt FROM hakutaku.events
                WHERE entity_id = %s AND event_type = 'entity_created'
                ORDER BY occurred_at ASC LIMIT 1;
                """,
                (str(entity_id),),
            )
            row = cur.fetchone()
        return (row or {}).get("source_excerpt")

    def transition_state(
        self,
        *,
        entity_id: UUID,
        new_state: str,
        trigger: str,
        source_id: UUID | None = None,
        source_excerpt: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str | None:
        """Atualiza `current_state.state` e emite `status_changed`. Retorna estado anterior.

        Idempotente: se `new_state` já é o atual, não escreve nada.
        """
        ts = occurred_at or datetime.now(timezone.utc)
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                "SELECT current_state FROM hakutaku.entities WHERE id = %s;",
                (str(entity_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"Entity {entity_id} não encontrada.")
            current_state = dict(row.get("current_state") or {})
            old_state = current_state.get("state")
            if old_state == new_state:
                return old_state

            current_state["state"] = new_state
            cur.execute(
                """
                UPDATE hakutaku.entities
                   SET current_state = %s,
                       last_updated_at = now()
                 WHERE id = %s;
                """,
                (Jsonb(current_state), str(entity_id)),
            )
            cur.execute(
                """
                INSERT INTO hakutaku.events
                    (entity_id, event_type, payload, source_id, source_excerpt, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    str(entity_id),
                    EventType.STATUS_CHANGED.value,
                    Jsonb({"old_status": old_state, "new_status": new_state, "trigger": trigger}),
                    str(source_id) if source_id else None,
                    source_excerpt,
                    ts,
                ),
            )
        self._conn.commit()
        return old_state

    # ------------------------------------------------------------------
    # demo metrics (Fase 4)
    # ------------------------------------------------------------------
    def find_duplicate_pairs(
        self, *, min_similarity: float = 0.8
    ) -> list[dict[str, Any]]:
        """Pares de entidades do MESMO tipo com `similarity(canonical_name) > min_similarity`.

        Retorna brutos — caller (demo) aplica filtro conservador (alias overlap,
        severity match etc.) por tipo.
        """
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT
                    e1.id AS id1, e1.type AS type1,
                    e1.canonical_name AS name1, e1.aliases AS aliases1,
                    e1.attributes AS attrs1, e1.current_state AS state1,
                    e2.id AS id2, e2.type AS type2,
                    e2.canonical_name AS name2, e2.aliases AS aliases2,
                    e2.attributes AS attrs2, e2.current_state AS state2,
                    similarity(e1.canonical_name, e2.canonical_name) AS sim
                FROM hakutaku.entities e1
                JOIN hakutaku.entities e2
                  ON e1.id < e2.id
                 AND e1.type = e2.type
                WHERE similarity(e1.canonical_name, e2.canonical_name) > %s
                ORDER BY sim DESC;
                """,
                (min_similarity,),
            )
            return list(cur.fetchall())

    def count_cross_source_relations(self) -> int:
        """Relações onde os dois endpoints foram criados em sources diferentes.

        Sinal forte de aprendizado: o sistema percebeu que entidades de docs
        distintos pertencem ao mesmo grafo e ligou-as.

        Implementação: `DISTINCT ON (entity_id) ... ORDER BY entity_id, occurred_at`
        em vez de `MIN(source_id)` — Postgres não tem agregação MIN/MAX para UUID,
        e mesmo se tivesse, MIN textual não respeitaria ordem temporal. DISTINCT ON
        pega a primeira linha por entity_id na ordem cronológica.
        """
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                WITH first_source AS (
                    SELECT DISTINCT ON (entity_id)
                           entity_id, source_id AS first_source_id
                    FROM hakutaku.events
                    WHERE event_type = 'entity_created' AND source_id IS NOT NULL
                    ORDER BY entity_id, occurred_at ASC, recorded_at ASC
                )
                SELECT COUNT(*) AS n
                FROM hakutaku.relations r
                JOIN first_source f1 ON f1.entity_id = r.from_entity
                JOIN first_source f2 ON f2.entity_id = r.to_entity
                WHERE f1.first_source_id IS DISTINCT FROM f2.first_source_id;
                """
            )
            row = cur.fetchone()
        return int((row or {}).get("n", 0))

    def count_haiku_resolver_calls(self) -> int:
        """Quantos merges precisaram de Haiku — proxy do custo de ambiguidade.

        Quando o context block ajuda, o resolver auto-decide mais (auto_high)
        e este número CAI. É a métrica `haiku_calls_economizadas` da Fase 4.
        """
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM hakutaku.events
                WHERE event_type = 'entity_merged'
                  AND payload->>'decision_method' = 'llm';
                """
            )
            row = cur.fetchone()
        return int((row or {}).get("n", 0))

    def count_resolver_decisions_by_method(self) -> dict[str, int]:
        """Distribuição de `decision_method` em eventos `entity_merged`."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT payload->>'decision_method' AS method, COUNT(*) AS n
                FROM hakutaku.events
                WHERE event_type = 'entity_merged'
                GROUP BY method;
                """
            )
            return {str(r["method"]): int(r["n"]) for r in cur.fetchall()}

    # ------------------------------------------------------------------
    # proposals (Fase 5)
    # ------------------------------------------------------------------
    def insert_proposal(
        self,
        *,
        proposal_type: str,
        title: str,
        description: str,
        justification: dict[str, Any],
        priority: int,
        related_entities: list[UUID],
    ) -> UUID:
        """Persiste uma proposta gerada pelo módulo de raciocínio.

        `related_entities` deve conter UUIDs já validados contra o grafo
        (filtragem feita pelo orchestrator para descartar IDs alucinados).
        """
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                INSERT INTO hakutaku.proposals
                    (proposal_type, title, description, justification, priority,
                     related_entities)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    proposal_type,
                    title,
                    description,
                    Jsonb(justification or {}),
                    int(priority),
                    [str(e) for e in (related_entities or [])],
                ),
            )
            row = cur.fetchone()
        self._conn.commit()
        assert row is not None
        return row["id"]

    def list_proposals(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Lista propostas (default: todas), ordenadas por priority desc, created_at desc."""
        self._ensure_alive()
        params: list[Any] = []
        sql = (
            "SELECT id, proposal_type, title, description, justification, priority, "
            "       status, related_entities, created_at "
            "FROM hakutaku.proposals "
        )
        if status:
            sql += "WHERE status = %s "
            params.append(status)
        sql += "ORDER BY priority DESC, created_at DESC LIMIT %s;"
        params.append(int(limit))
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(sql, params)
            return list(cur.fetchall())

    def clear_proposals(self) -> int:
        """Remove TODAS as propostas. Usado pelo reasoning orchestrator
        quando `clear_existing=True`. Retorna # de linhas deletadas."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute("DELETE FROM hakutaku.proposals;")
            n = cur.rowcount
        self._conn.commit()
        return int(n or 0)

    # ------------------------------------------------------------------
    # cross-source / answers (Fase 4)
    # ------------------------------------------------------------------
    def list_answers_relations(self) -> list[dict[str, Any]]:
        """Relações `answers` com nomes/excerpts dos dois endpoints — para apresentação."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
                SELECT r.id AS rel_id, r.from_entity, r.to_entity,
                       r.attributes AS rel_attrs, r.confidence AS rel_confidence,
                       d.canonical_name AS decision_name,
                       d.attributes->>'rationale' AS decision_rationale,
                       q.canonical_name AS question_name,
                       q.current_state->>'state' AS question_state,
                       q.first_seen_at AS question_first_seen,
                       d.first_seen_at AS decision_first_seen
                FROM hakutaku.relations r
                JOIN hakutaku.entities d ON d.id = r.from_entity
                JOIN hakutaku.entities q ON q.id = r.to_entity
                WHERE r.relation_type = 'answers'
                ORDER BY r.created_at ASC;
                """
            )
            return list(cur.fetchall())

    # ------------------------------------------------------------------
    # llm_calls — sink consumido pelo LLMClient
    # ------------------------------------------------------------------
    def insert_llm_call(self, record: dict[str, Any]) -> None:
        """Persiste uma chamada LLM em `hakutaku.llm_calls`.

        Compatível com a forma do dict produzido por `LLMClient._log_call`. Campos
        ausentes viram NULL.

        Em caso de erro (FK violation, conexão caída, etc.) faz `rollback()` na
        conexão antes de re-levantar — caso contrário o psycopg deixa a transação
        em estado abortado e a próxima query falha com `InFailedSqlTransaction`.
        O chamador (LLMClient._log_call) captura, loga em stderr e segue.

        IMPORTANTE: este método é chamado pelo `LLMClient` *imediatamente* após
        chamadas LLM longas (>30s). Sem `_ensure_alive()` aqui, a conexão pode
        ter morrido por idle durante o LLM call e a primeira escrita falha. Foi
        a causa do `server closed unexpectedly` no run 1 do `demo_learning`.
        """
        self._ensure_alive()
        try:
            with self._conn.cursor() as cur:
                cur.execute(self.SCHEMA_SETUP)
                cur.execute(
                    """
                    INSERT INTO hakutaku.llm_calls
                        (stage, model, prompt_template_version,
                         input_tokens, output_tokens, cost_usd, latency_ms,
                         input_payload, output_payload, cache_hit, source_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        record.get("stage"),
                        record.get("model"),
                        record.get("prompt_template_version"),
                        record.get("input_tokens"),
                        record.get("output_tokens"),
                        record.get("cost_usd"),
                        record.get("latency_ms"),
                        Jsonb(record.get("input") or {}),
                        Jsonb(
                            record.get("output")
                            if isinstance(record.get("output"), dict)
                            else {"value": record.get("output")}
                        ),
                        bool(record.get("cache_hit", False)),
                        str(record["source_id"]) if record.get("source_id") else None,
                    ),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def stats(self) -> dict[str, int]:
        """Contagens rápidas para CLI/log."""
        self._ensure_alive()
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute("SELECT COUNT(*) AS n FROM hakutaku.entities;")
            total_entities = (cur.fetchone() or {}).get("n", 0)
            cur.execute("SELECT COUNT(*) AS n FROM hakutaku.relations;")
            total_relations = (cur.fetchone() or {}).get("n", 0)
            cur.execute("SELECT COUNT(*) AS n FROM hakutaku.events;")
            total_events = (cur.fetchone() or {}).get("n", 0)
        return {
            "entities": int(total_entities),
            "relations": int(total_relations),
            "events": int(total_events),
        }

    def truncate_all(self) -> None:
        """Limpa todas as tabelas do schema (uso EXCLUSIVO de scripts de reset).

        Estratégia padrão: ``TRUNCATE ... RESTART IDENTITY CASCADE`` — instantâneo,
        idiomático, isso é o que o `diag_truncate` validou que funciona limpo via
        psycopg ↔ Supabase quando a conexão está saudável.

        Fallback: se ``TRUNCATE`` cair com ``OperationalError`` ou
        ``InterfaceError`` (transitividade observada nos runs 2-3 da Fase 4 —
        possível Supabase blip ou pooler kill momentâneo), reabre a conexão e
        tenta DELETE iterativo. DELETE em tabelas vazias roda em <100ms e não
        toma ACCESS EXCLUSIVE. O `WARNING` log fica como evidência empírica
        para refinarmos a regra depois com mais ocorrências.
        """
        self._ensure_alive()

        truncate_sql = """
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

        try:
            with self._conn.cursor() as cur:
                cur.execute(self.SCHEMA_SETUP)
                cur.execute(truncate_sql)
            self._conn.commit()
            return
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            _log.warning(
                "[truncate_all] TRUNCATE CASCADE falhou (%s: %s), "
                "reabrindo conexão e usando DELETE iterativo como fallback.",
                type(exc).__name__,
                str(exc)[:200],
            )
            try:
                self._conn.rollback()
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = self._open_connection()

            ordered_tables = [
                "events",
                "relations",
                "proposals",
                "patterns",
                "llm_calls",
                "entities",
                "sources",
            ]
            with self._conn.cursor() as cur:
                cur.execute(self.SCHEMA_SETUP)
                for table in ordered_tables:
                    cur.execute(f"DELETE FROM hakutaku.{table};")
            self._conn.commit()


# =====================================================================
# Singleton lazy
# =====================================================================
@lru_cache(maxsize=1)
def get_repository() -> GraphRepository:
    return GraphRepository(dsn=get_settings().supabase_db_url)
