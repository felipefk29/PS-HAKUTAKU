# Arquitetura — Hakutaku MVP

## 1. Camadas

```
┌──────────────────────────────────────────────────────────────────┐
│                      Frontend (Next.js 14)                       │
│  /  (dashboard)  ·  /graph  (react-flow)  ·  /proposals          │
└─────────────┬──────────────────────────────┬─────────────────────┘
              │ HTTP                          │
┌─────────────▼──────────────────────────────▼─────────────────────┐
│                    Backend API (FastAPI)                         │
│  /health · /stats · /graph · /entities · /proposals              │
│  POST /pipeline/{ingest,reason,cross-link}                       │
└─────────────┬──────────────────────────────┬─────────────────────┘
              │                              │
┌─────────────▼─────────────┐  ┌─────────────▼─────────────┐
│   Extraction pipeline     │  │   Reasoning + proposals   │
│ adapter → context block   │  │  6 detectors → Sonnet     │
│   → extractor → ingester  │  │     → ProposalsBatch      │
└─────────────┬─────────────┘  └─────────────┬─────────────┘
              │                              │
┌─────────────▼──────────────────────────────▼─────────────────────┐
│   Memory: entity_resolver · context_retriever · cross_linker     │
└─────────────────────────────┬────────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────────┐
│              GraphRepository (psycopg + pgvector)                │
│  hakutaku.{sources, entities, events, relations,                 │
│            proposals, patterns, llm_calls}                       │
└─────────────────────────────┬────────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────────┐
│          Supabase Postgres 17 + pgvector + pg_trgm               │
└──────────────────────────────────────────────────────────────────┘

           ┌───────────── shared infra ─────────────┐
           │  LLMClient: cache (SHA256) + log JSON  │
           │            + DB sink (llm_calls)       │
           │  Anthropic SDK · OpenAI embeddings     │
           │  prompts/*.yaml (versionados)          │
           └────────────────────────────────────────┘
```

## 2. Fluxo de uma ingestão

```
1. POST /pipeline/ingest {source_type, title, raw_content}
                   │
                   ▼
2. Adapter.parse(raw_content)
   → NormalizedDocument {source_id, normalized_content, metadata}
                   │
                   ▼
3. repository.upsert_source(source_id, ...)
   (DEVE vir antes de chamadas LLM — events.source_id tem FK)
                   │
                   ▼
4. extract_from_document(doc, repository=repo)
       │
       ├─► build_context_block(doc, repo, llm)
       │     → embed doc → top-15 entidades cross-type por cosine
       │     + top-5 recentes + open questions + open risks + active projects
       │     → texto PT-BR estruturado
       │
       ├─► load_prompt("extraction")
       │     → format(system, user) com {context_block} injetado
       │
       └─► llm.extract_structured(response_model=ExtractedContent)
             → instructor envolve Anthropic com tool-use; valida Pydantic
             → cache hit/miss em data/cache/llm/{sha256}.json
             → log em data/logs/calls/{date}/{time}_{stage}_{uid}.json
             → DB sink em hakutaku.llm_calls
                   │
                   ▼
5. ExtractionResult {entities, relations, notes, call_metadata}
   → salva em data/extractions/{source_id}_{ts}.json
                   │
                   ▼
6. ingest_extraction(extraction, repo, llm)
       │
       ├─► para cada entity:
       │     resolve_entity → ResolutionDecision (create | merge | bypass)
       │       Estágios: pg_trgm (blocking) → pgvector (rerank)
       │                 → Haiku LLM (zona cinza)
       │     persiste insert/update + event entity_created/attribute_changed/
       │       status_changed/entity_merged
       │     mantém map alias → uuid (local ao doc)
       │
       └─► para cada relation:
             mapeia from_alias/to_alias → uuids via map local
             insert_relation (idempotente via UNIQUE)
             event relation_added
                   │
                   ▼
7. snapshot JSON + HTML em data/graph_snapshots/{ts}_{label}.{json,html}
   (Pyvis para HTML interativo; fallback estático se ausente)
```

## 3. Loop de aprendizado

A memória do sistema é construída em **4 mecanismos** (SPEC §7):

1. **Entity resolution histórica** — toda mention é candidata a merge contra o grafo
   acumulado. Ver D006/D009 (funil 3 estágios).

2. **Extração contextualizada** (D012) — antes de chamar o extrator, recuperamos
   top-K entidades relevantes e injetamos como `context_block`. O extrator
   reusa `canonical_name` em vez de criar duplicatas, e detecta atualizações
   de estado (Risk escala, Task vai pra done) em vez de gerar entidade nova.

3. **Cross-source linking** (D013) — pós-ingestão, o `cross_linker` casa
   `OpenQuestion` (estado=open) com `Decision` posterior via cosine sim +
   Haiku verdict. Cria aresta `answers` e transita Q para `state='answered'`.

4. **Padrões longitudinais** — `events` é a fonte da verdade temporal.
   `BehavioralPattern` é entidade de primeira classe (SPEC §2.10), gerada
   pelo módulo de raciocínio quando evidências cruzam threshold. (Detector
   ainda não implementado — TODO pós-MVP.)

A invariante de aprendizado: a curva *(entidades novas / total)* por documento
processado deve ser **decrescente**. Validado empiricamente pelo `demo_learning.py`
(ver Fase 4 metrics).

## 4. Reasoning cycle

```
run_reasoning_cycle(repo, llm)
    │
    ├─► run_all_detectors(repo)
    │       └─► 6 detectores em sequência, cada um query SQL pura:
    │             • orphan_tasks
    │             • escalating_risks (open high/critical + escalation events)
    │             • overdue_tasks
    │             • unanswered_questions (>= 7 days)
    │             • single_point_of_failure (>= 3 items per person)
    │             • blocked_dependencies
    │           Cada detector emite Finding {detector, severity, description,
    │             related_entities, evidence}
    │
    ├─► generate_proposals(findings, llm)
    │       │
    │       ├─► render findings_block (texto agrupado por detector,
    │       │     incluindo entity_ids reais)
    │       │
    │       └─► llm.extract_structured(response_model=ProposalsBatch)
    │             → Sonnet via instructor com prompts/proposals.yaml v1.0.0
    │             → ProposalsBatch {proposals, summary}
    │
    ├─► (opcional) clear_existing → DELETE FROM hakutaku.proposals
    │
    ├─► para cada Proposal:
    │       _filter_known_entity_ids → discarta UUIDs alucinados
    │       repository.insert_proposal → row em hakutaku.proposals
    │
    └─► snapshot JSON em data/proposals/reasoning_cycle_{ts}.json
        (findings + proposals + stats — auditoria visual)
```

## 5. Camada de dados

### Schema `hakutaku` (Supabase Postgres 17)

| Tabela | Papel |
|---|---|
| `sources` | documentos brutos ingeridos (transcript meeting / chat thread) |
| `entities` | nós do grafo (10 tipos discriminados por `type`); inclui `embedding vector(1536)` |
| `events` | log imutável de mudanças — fonte da verdade temporal |
| `relations` | arestas direcionadas; UNIQUE(from, to, type) garante idempotência |
| `proposals` | output do reasoning (alert / suggestion / action × priority 1-5) |
| `patterns` | padrões longitudinais (TODO pós-MVP — schema pronto) |
| `llm_calls` | auditoria server-side de toda chamada LLM (custo, latência, tokens) |

Extensões: `vector` (HNSW índice em embeddings), `pg_trgm` (índice GIN em
canonical_name para blocking lexical), `pgcrypto` (gen_random_uuid).

### Conexão psycopg

- Direct connection (`db.<ref>.supabase.co:5432`), NÃO pooler.
- TCP keepalives (`keepalives_idle=20`) para sobreviver idle de chamadas LLM longas.
- `_ensure_alive()` antes de cada operação pública: ping `SELECT 1` e reabre
  conexão se necessário. Logger.warning quando reabre — evidência empírica
  para ocorrências futuras.
- `truncate_all` usa `TRUNCATE ... RESTART IDENTITY CASCADE` com fallback
  para DELETE iterativo em caso de transitório (`OperationalError`/`InterfaceError`).

## 6. Limites e o que está fora do escopo do MVP

- **Auth multi-tenant** — não há autenticação. Single-tenant local.
- **Streaming** — extração e reasoning são síncronos (request HTTP segura ~30-60s).
- **Detector de BehavioralPattern** — schema existe mas o detector não.
- **Aprendizado de feedback** — proposals aceitas vs descartadas não retroalimentam
  os detectores. Próximo passo lógico.
- **Fine-tuning** — toda inteligência é via prompt + structured output.
- **Sharding/escala** — Postgres + pgvector escala vertical até ~10⁵ entidades
  (D002). Acima disso, considerar vector store dedicado.
- **Diff-based proposal updates** — atualmente cada ciclo de raciocínio dropa
  todas as propostas (`clear_existing=True`). Em produção, valeria preservar
  aceitas e diff incremental.
