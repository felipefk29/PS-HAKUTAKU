-- =====================================================================
-- Hakutaku MVP — initial schema (Phase 1)
-- Materializa a ontologia descrita em docs/SPEC.md.
--
-- Convenções:
--   * Todas as tabelas vivem no schema dedicado `hakutaku` para não
--     poluir `public` (a instância Supabase é compartilhada com outro
--     projeto).
--   * Discriminadores (type, event_type, relation_type, etc.) são TEXT
--     sem CHECK estrito — a vocabulário oficial é validado pelos modelos
--     Pydantic na borda da aplicação. O DB enforça shape, não vocabulário,
--     para que evoluir a ontologia não exija migration.
--   * UUIDs gerados pelo DB. Pydantic NUNCA inventa IDs.
--   * Toda mudança de estado vai por `events` — `entities.current_state`
--     é projeção materializada e regenerável.
-- =====================================================================

-- Schema dedicado.
CREATE SCHEMA IF NOT EXISTS hakutaku;

-- Extensões (idempotentes; ficam no schema padrão da instância).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Resolve `vector`, `gin_trgm_ops`, `gen_random_uuid` sem qualificar.
SET search_path = hakutaku, extensions, public;


-- ---------------------------------------------------------------------
-- sources — documentos brutos ingeridos
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hakutaku.sources (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type   TEXT NOT NULL CHECK (source_type IN ('meeting', 'chat')),
  title         TEXT NOT NULL,
  raw_content   TEXT NOT NULL,
  metadata      JSONB DEFAULT '{}'::jsonb,
  occurred_at   TIMESTAMPTZ,
  processed_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sources_type_idx        ON hakutaku.sources (source_type);
CREATE INDEX IF NOT EXISTS sources_occurred_at_idx ON hakutaku.sources (occurred_at DESC NULLS LAST);


-- ---------------------------------------------------------------------
-- entities — nós do grafo (todos os tipos da ontologia)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hakutaku.entities (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type            TEXT NOT NULL,            -- Person | Project | Client | Task | Decision | Risk | OpenQuestion | Dependency | Commitment | BehavioralPattern
  canonical_name  TEXT NOT NULL,
  aliases         TEXT[] DEFAULT '{}',
  attributes      JSONB DEFAULT '{}'::jsonb,
  current_state   JSONB DEFAULT '{}'::jsonb,
  embedding       vector(1536),               -- OpenAI text-embedding-3-small
  confidence      FLOAT DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  first_seen_at   TIMESTAMPTZ DEFAULT now(),
  last_updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entities_type_idx       ON hakutaku.entities (type);
CREATE INDEX IF NOT EXISTS entities_name_trgm_idx  ON hakutaku.entities USING gin (canonical_name gin_trgm_ops);
-- HNSW: melhor recall em datasets pequenos/médios sem precisar pre-popular o índice.
CREATE INDEX IF NOT EXISTS entities_embedding_idx  ON hakutaku.entities USING hnsw (embedding vector_cosine_ops);


-- ---------------------------------------------------------------------
-- events — modelo temporal (fonte da verdade do histórico)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hakutaku.events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id       UUID NOT NULL REFERENCES hakutaku.entities (id) ON DELETE CASCADE,
  event_type      TEXT NOT NULL,    -- entity_created | attribute_changed | status_changed | relation_added | relation_removed | entity_merged
  payload         JSONB NOT NULL,
  source_id       UUID REFERENCES hakutaku.sources (id) ON DELETE SET NULL,
  source_excerpt  TEXT,
  occurred_at     TIMESTAMPTZ NOT NULL,
  recorded_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_entity_idx   ON hakutaku.events (entity_id);
CREATE INDEX IF NOT EXISTS events_occurred_idx ON hakutaku.events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_type_idx     ON hakutaku.events (event_type);
CREATE INDEX IF NOT EXISTS events_source_idx   ON hakutaku.events (source_id);


-- ---------------------------------------------------------------------
-- relations — arestas do grafo
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hakutaku.relations (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_entity    UUID NOT NULL REFERENCES hakutaku.entities (id) ON DELETE CASCADE,
  to_entity      UUID NOT NULL REFERENCES hakutaku.entities (id) ON DELETE CASCADE,
  relation_type  TEXT NOT NULL,
  attributes     JSONB DEFAULT '{}'::jsonb,
  source_id      UUID REFERENCES hakutaku.sources (id) ON DELETE SET NULL,
  confidence     FLOAT DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  created_at     TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT relations_no_self_loop CHECK (from_entity <> to_entity),
  UNIQUE (from_entity, to_entity, relation_type)
);

CREATE INDEX IF NOT EXISTS relations_from_idx ON hakutaku.relations (from_entity);
CREATE INDEX IF NOT EXISTS relations_to_idx   ON hakutaku.relations (to_entity);
CREATE INDEX IF NOT EXISTS relations_type_idx ON hakutaku.relations (relation_type);


-- ---------------------------------------------------------------------
-- proposals — output do reasoning
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hakutaku.proposals (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  proposal_type     TEXT NOT NULL CHECK (proposal_type IN ('alert', 'suggestion', 'action')),
  title             TEXT NOT NULL,
  description       TEXT NOT NULL,
  justification     JSONB NOT NULL,
  priority          INT  NOT NULL CHECK (priority BETWEEN 1 AND 5),
  status            TEXT DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'dismissed', 'resolved')),
  related_entities  UUID[] DEFAULT '{}',
  created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS proposals_status_idx     ON hakutaku.proposals (status);
CREATE INDEX IF NOT EXISTS proposals_priority_idx   ON hakutaku.proposals (priority DESC);
CREATE INDEX IF NOT EXISTS proposals_created_at_idx ON hakutaku.proposals (created_at DESC);


-- ---------------------------------------------------------------------
-- patterns — memória de padrões longitudinais detectados
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hakutaku.patterns (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pattern_type        TEXT NOT NULL,
  description         TEXT NOT NULL,
  evidence            JSONB NOT NULL,
  subject_entity_id   UUID REFERENCES hakutaku.entities (id) ON DELETE CASCADE,
  confidence          FLOAT DEFAULT 0.0 CHECK (confidence BETWEEN 0 AND 1),
  status              TEXT DEFAULT 'emerging' CHECK (status IN ('emerging', 'confirmed', 'weakening', 'dissolved')),
  created_at          TIMESTAMPTZ DEFAULT now(),
  last_reinforced_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS patterns_type_idx    ON hakutaku.patterns (pattern_type);
CREATE INDEX IF NOT EXISTS patterns_subject_idx ON hakutaku.patterns (subject_entity_id);
CREATE INDEX IF NOT EXISTS patterns_status_idx  ON hakutaku.patterns (status);


-- ---------------------------------------------------------------------
-- llm_calls — auditoria de toda chamada LLM
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hakutaku.llm_calls (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  stage                    TEXT NOT NULL,
  model                    TEXT NOT NULL,
  prompt_template_version  TEXT,
  input_tokens             INT,
  output_tokens            INT,
  cost_usd                 NUMERIC(10, 6),
  latency_ms               INT,
  input_payload            JSONB,
  output_payload           JSONB,
  cache_hit                BOOLEAN DEFAULT FALSE,
  source_id                UUID REFERENCES hakutaku.sources (id) ON DELETE SET NULL,
  created_at               TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_calls_stage_idx      ON hakutaku.llm_calls (stage);
CREATE INDEX IF NOT EXISTS llm_calls_model_idx      ON hakutaku.llm_calls (model);
CREATE INDEX IF NOT EXISTS llm_calls_created_at_idx ON hakutaku.llm_calls (created_at DESC);
CREATE INDEX IF NOT EXISTS llm_calls_source_idx     ON hakutaku.llm_calls (source_id);
