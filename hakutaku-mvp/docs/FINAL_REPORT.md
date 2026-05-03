# Relatório final — Hakutaku MVP

> Data: 2026-05-03 · Branch: `main` · Commit base: `c6fbd71` (Fase 3) → este (Fase 8)

Este documento é o estado **honesto** do projeto no fechamento. Lista o que está implementado, o que ficou parcial, e o que precisaria para produção.

---

## 1. Status por fase

| Fase | Status | Evidência |
|---|---|---|
| **0. Setup** | ✅ Completa | Estrutura, `CLAUDE.md`, `.gitignore`, `pyproject.toml`, `.env.example` |
| **1. Ontologia + schema** | ✅ Completa | 7 tabelas Supabase aplicadas via migration manual (SQL Editor); 10 tipos Pydantic + 14 relações + união discriminada `Entity`. Ver [SPEC.md](SPEC.md) |
| **2. LLM wrapper + extração** | ✅ Completa | `LLMClient` com cache SHA256 + log JSON + DB sink + retry tenacity; `extract_from_document` end-to-end validado; prompts versionados em YAML |
| **3. Grafo + entity resolution** | ✅ Completa | `GraphRepository` (psycopg + pgvector + event sourcing); funil 3 estágios `pg_trgm → pgvector → Haiku`; recalibração D009.1 documentada; snapshots HTML/JSON por documento |
| **4. Memória + aprendizado** | ⚠️ Parcial | Componentes prontos: `context_retriever` validado em `run_full_pipeline` (taxa de merge dispara em docs 2/3), `cross_linker` validado por código + smoke test. **`demo_learning.py` end-to-end NÃO rodou na sessão final** — ver D015 e §3 abaixo |
| **5. Reasoning + propostas** | ✅ Completa | 6 detectores SQL puros + `generate_proposals` via Sonnet; **validado empiricamente**: 6 findings → 5 propostas concretas sobre TechNova/Pedro Silva ($0.037, snapshot em `data/proposals/`) |
| **6. API FastAPI** | ✅ Completa | 14 rotas; 5 smoke testadas via curl (`/health`, `/stats`, `/graph`, `/entities?type=Person`, `/proposals`). Docs interativas em `/docs` |
| **7. Frontend Next.js** | ✅ Completa | 4 páginas (`/`, `/graph`, `/proposals`, 404); build limpo; `npm run dev` validado (HTTP 200, dashboard renderiza header + nav + cards de estado) |
| **8. Documentação** | ✅ Completa | `README.md` com runbook completo, `SPEC.md` (ontologia), `DECISIONS.md` (D001-D015), `ARCHITECTURE.md`, `PROMPTING.md`, este `FINAL_REPORT.md` |

---

## 2. Inventário de componentes

### Backend (Python 3.11 / FastAPI / Pydantic v2)

```
backend/src/hakutaku/
├── adapters/        # MeetingAdapter, ChatAdapter, NormalizedDocument
├── extraction/      # extract_from_document (com context_block opcional)
├── graph/
│   ├── repository.py   # GraphRepository (28 métodos públicos, todos com _ensure_alive)
│   └── ingester.py     # ingest_extraction (resolver + persistência + snapshot)
├── memory/
│   ├── entity_resolver.py    # funil 3 estágios D006/D009
│   ├── context_retriever.py  # build_context_block para Fase 4
│   └── cross_linker.py       # link_questions_to_decisions via Haiku
├── reasoning/
│   ├── detectors.py     # 6 detectores: orphan_tasks, escalating_risks,
│   │                    #   overdue_tasks, unanswered_questions,
│   │                    #   single_point_of_failure, blocked_dependencies
│   └── orchestrator.py  # run_reasoning_cycle
├── proposals/generator.py  # generate_proposals via Sonnet
├── llm/
│   ├── client.py        # LLMClient (cache + log + retry + cost)
│   └── prompts.py       # load_prompt (YAML → Prompt)
├── schemas/             # 6 módulos Pydantic — fonte de verdade
├── api/
│   ├── main.py          # FastAPI app (14 rotas)
│   └── schemas.py       # view models
└── config.py            # Settings via pydantic-settings

backend/scripts/
├── run_extraction.py     # CLI: 1 doc → ExtractionResult JSON
├── run_full_pipeline.py  # CLI: 3 docs → grafo + (--reason) + (--cross-link)
├── demo_learning.py      # ⚠️ ver D015 — código pronto, run end-to-end pendente
├── diag_truncate.py      # diagnóstico TRUNCATE/DELETE (sem custo LLM)
└── diag_idle_timeout.py  # diagnóstico idle 60s (sem custo LLM)
```

### Frontend (Next.js 14 + Tailwind + react-flow)

```
frontend/src/
├── app/
│   ├── layout.tsx      # nav (Dashboard / Grafo / Propostas)
│   ├── page.tsx        # / — stats cards + propostas em aberto + botão raciocínio
│   ├── graph/page.tsx  # /graph — react-flow com filtro por tipo
│   └── proposals/page.tsx  # /proposals — lista + filtros + ações (aceitar/descartar/resolver)
└── lib/api.ts          # cliente HTTP tipado
```

### Banco (Supabase Postgres 17)

Schema dedicado `hakutaku` em `db.sfzekiuqdcycnaynyzoq.supabase.co:5432` (direct connection):

| Tabela | Última contagem (sessão final) |
|---|---|
| `sources` | 3 |
| `entities` | 25 |
| `events` | 79 |
| `relations` | 24 |
| `proposals` | 5 |
| `patterns` | 0 (detector pós-MVP) |
| `llm_calls` | ~80 (varies) |

Extensões: `vector` (HNSW), `pg_trgm` (GIN trgm_ops), `pgcrypto`.

---

## 3. Limitações conhecidas

### 3.1. `demo_learning.py` não rodou end-to-end na sessão final
- O script existe e foi instrumentado contra todos os bugs encontrados nas tentativas anteriores: TCP keepalives, `_ensure_alive` em todos os métodos do repo, `MIN(uuid) → DISTINCT ON`, escape `{{` `}}` em YAML.
- A última tentativa autorizada foi cancelada para preservar orçamento.
- **Próxima sessão**: `cd backend && python -m scripts.demo_learning` deve passar. Se falhar, o stacktrace indicará o ponto exato (instrumentação de logs já está em lugar).
- Ver D015 para racional completo.

### 3.2. Idle timeout em chamadas LLM longas
- Mitigado por TCP keepalives (`keepalives_idle=20`) + `_ensure_alive()` em todos os métodos públicos do `GraphRepository`.
- Em runs de 3+ documentos com cache miss (extração de 30-40s cada), pode ainda haver janelas onde o pooler do Supabase (apesar de usarmos direct connection) descarta a conexão. O `_ensure_alive` reabre transparentemente — **observação empírica via `data/logs/calls/`** se acontecer.

### 3.3. Resolução de aliases em relações cross-doc
- Quando o extrator gera relação com `to_alias` que não está no doc atual (ex.: Person → "Status conta TechNova" — title de outra reunião), a aresta é descartada com nota.
- Isso causou ~8 relações perdidas por execução do doc 3 (visível no log `phase5_pipeline_run2.log`).
- Solução pós-MVP: depois do mapeamento local, fazer 1 lookup contra `entities.canonical_name` global antes de descartar.

### 3.4. Detector de `BehavioralPattern` não implementado
- Schema da tabela `patterns` existe, tipo `BehavioralPattern` na ontologia também — mas o detector que popularia (ex.: "Pedro tem 3 commitments quebrados em 30 dias") fica para pós-MVP.

### 3.5. Aprendizado de feedback não retroalimenta
- O painel `/proposals` permite aceitar/descartar/resolver, mas essas decisões só atualizam `status` no banco.
- Não há ciclo onde "propostas descartadas" ajustam thresholds de detectores ou reweight de prompts.

### 3.6. Sem testes automatizados
- Validação foi via runs end-to-end + scripts diagnósticos (`diag_truncate`, `diag_idle_timeout`).
- Não há `pytest` rodando; `pyproject.toml` tem dep mas sem suíte escrita.

---

## 4. O que precisaria para produção

### Curto prazo (1-2 semanas)
1. **Suíte pytest** cobrindo: schemas Pydantic, detectores (com fixtures de grafo), `entity_resolver` em zona cinza, prompt loader.
2. **Auth** — pelo menos um header `X-API-Key` na FastAPI.
3. **Rate limiting** — Sonnet via `slowapi` ou middleware nativo.
4. **CI** (GitHub Actions): lint (ruff), type check (mypy), test (pytest), e talvez um smoke test do pipeline em DB de teste.
5. **Logging estruturado** — substituir os `print(...)` que sobraram nos diagnósticos por `logging` com formatter JSON (já comecei via `_log` em `repository.py`).

### Médio prazo (1-2 meses)
1. **Detector de `BehavioralPattern`** — querying `events` para padrões longitudinais.
2. **Pipeline assíncrono** — POST `/pipeline/ingest` retorna 202 + job_id; processa em worker (Celery/Arq) e expõe `/jobs/{id}` para polling/SSE.
3. **Diff-based proposal updates** — preservar propostas aceitas, recalcular o resto.
4. **Calibração de entity_resolver** — dataset rotulado de match/no-match (alinhado pela revisão humana de merges no painel) → tuning de thresholds.
5. **Multi-tenant** — particionamento por `tenant_id` em todas as tabelas + RLS no Postgres.
6. **Frontend**: paginação, busca, drill-down em entidades, replay de events na timeline.

### Longo prazo
1. **Fine-tuning** do extrator em dataset interno (após acumular > 100 docs).
2. **Escala vertical**: monitor de tamanho do grafo; migração para vector store dedicado (Pinecone/Weaviate) ao passar de ~10⁵ entidades (D002).
3. **Realtime updates** via Supabase realtime — frontend escuta mudanças no grafo.

---

## 5. Custo total da sessão

Agregado de `data/logs/calls/`:

| Métrica | Valor |
|---|---|
| Total LLM calls | 203 |
| Cache hits | 24 (12%) |
| Input tokens | 529,955 |
| Output tokens | 128,731 |
| **Custo total USD** | **$0.98** |

Por stage:

| Stage | Calls | Custo |
|---|---|---|
| `extraction` (Sonnet 4.5) | 32 | $0.6502 |
| `entity_resolution` (Haiku 4.5 zona cinza) | 84 | $0.2968 |
| `proposals_generation` (Sonnet 4.5) | 1 | $0.0373 |
| `context_retrieval_embed` (text-embedding-3-small) | 3 | $0.0000 |
| `entity_resolution_embed` (text-embedding-3-small) | 83 | $0.0000 |

Bem dentro do orçamento de US$ 5 mencionado no kickoff.

---

## 6. Como rodar (cheat sheet)

```bash
# Pré-req: .env preenchido em backend/.env (ANTHROPIC_API_KEY, OPENAI_API_KEY,
#          SUPABASE_DB_URL apontando para schema hakutaku já migrado)

# 1. Backend API + frontend (em terminais separados)
cd backend
.venv\Scripts\activate
python -m uvicorn hakutaku.api.main:app --host 127.0.0.1 --port 8000 --app-dir src --reload
# → docs em http://127.0.0.1:8000/docs

cd frontend
npm install
npm run dev
# → UI em http://localhost:3000

# 2. Pipeline standalone (3 docs → grafo + propostas)
cd backend
.venv\Scripts\activate
python -m scripts.run_full_pipeline --reset --reason
# Variantes:
#   --cross-link              # adiciona Q→D linking (Haiku)
#   sem --reset               # incrementa em cima do grafo existente

# 3. Demo de aprendizado (modo A vs B side-by-side, ~$0.30-0.50)
python -m scripts.demo_learning

# 4. Diagnósticos (sem custo LLM, < 1 min cada)
python -m scripts.diag_truncate       # valida TRUNCATE + DELETE iterativo
python -m scripts.diag_idle_timeout   # valida _ensure_alive em sleep 60s
```

---

## 7. Arquivos para revisão

| Documento | O quê |
|---|---|
| [README.md](../README.md) | Visão geral, stack, runbook completo |
| [SPEC.md](SPEC.md) | Ontologia (10 tipos, 14 relações, modelo temporal) |
| [DECISIONS.md](DECISIONS.md) | D001-D015 — todas as decisões com Contexto/Decisão/Trade-off/Justificativa |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Diagramas de camadas, fluxo de ingestão, reasoning cycle, limites |
| [PROMPTING.md](PROMPTING.md) | Versionamento, cache, structured output, custo com cache frio/quente |
| [FINAL_REPORT.md](FINAL_REPORT.md) | Este documento |

---

## 8. Reflexão honesta sobre o sprint

**O que funcionou bem:**
- Decompor em 8 fases sequenciais com checkpoint manual permitiu corrigir rota cedo (recalibração D009.1, ontologia operacional D005).
- Visibilidade-primeiro (artefatos JSON/HTML em `data/`) acelerou debug e evitou black box.
- `instructor` + Pydantic eliminou classe inteira de bugs de parsing.
- Cache local SHA256 reduziu custo de iteração drasticamente — sem ele, esse sprint custaria 5-10× mais.

**O que doeu:**
- Pulei diagnose para fix em duas ocasiões (idle timeout, MIN(uuid)) e perdi tempo. Ver `feedback_diagnose_before_fix.md` na memória.
- Confundi projeto Supabase (`mhlhcdzxqqlmejoionol` vs `sfzekiuqdcycnaynyzoq`) por não verificar DSN do `.env` antes de usar MCP.
- Não escrevi testes desde o dia 0; valida-tudo-no-final acumulou risco que apareceu na Fase 4-5.

**O que faria diferente:**
- Migrations versionadas via Supabase MCP desde a Fase 1 (em vez de SQL Editor manual) — `list_migrations` ficaria útil.
- Suíte pytest mínima desde a Fase 2 (ao menos contratos Pydantic e prompt loading).
- Cache de embeddings com versionamento explícito (hoje só hash do texto — mudou modelo, cache antigo continua válido por engano).
