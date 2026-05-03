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
        self._conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
        self._conn.commit()

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

        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(sql, params)
            return [EntityRecord.from_row(r) for r in cur.fetchall()]

    def get_full_graph(self) -> dict[str, Any]:
        """Snapshot serializável do grafo inteiro — para visualização e auditoria."""
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
        """
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
        """Limpa todas as tabelas do schema. Uso EXCLUSIVO de scripts de reset."""
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA_SETUP)
            cur.execute(
                """
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
            )
        self._conn.commit()


# =====================================================================
# Singleton lazy
# =====================================================================
@lru_cache(maxsize=1)
def get_repository() -> GraphRepository:
    return GraphRepository(dsn=get_settings().supabase_db_url)
