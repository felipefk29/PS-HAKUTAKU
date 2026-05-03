# Hakutaku — Organizational Intelligence Layer

> Transforma documentos organizacionais não-estruturados em um grafo ontológico que raciocina sobre si mesmo e propõe ações.

## Visão geral

Hakutaku ingere transcrições de reunião e threads de chat, extrai entidades tipadas (pessoas, tasks, decisões, riscos, projetos) usando LLMs com structured output, modela tudo como um grafo no Supabase com pgvector, e raciocina sobre o grafo para detectar padrões — gargalos, decisões esquecidas, dependências implícitas — gerando propostas acionáveis. A cada novo documento processado, o contexto acumulado melhora a qualidade da extração e do raciocínio: **o sistema aprende**.

Demonstração canônica: 3 documentos do desafio (1 reunião kickoff TechNova, 1 thread de chat, 1 reunião status) → grafo com 25 entidades / 24 relações / 79 eventos → 6 findings detectados → 5 propostas priorizadas em PT-BR citando entidades e contexto reais.

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.11+, FastAPI, Pydantic v2 |
| LLM (geração) | Claude Sonnet 4.5 (extração + raciocínio), Claude Haiku 4.5 (entity resolution + cross-linker) |
| Structured output | `instructor` over Anthropic SDK |
| Embeddings | OpenAI `text-embedding-3-small` |
| Banco | Supabase (Postgres 17 + pgvector + pg_trgm) |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind, react-flow |

## Como rodar

### Pré-requisitos
- Python 3.11+ (testado em 3.11.9)
- Node 18+ (testado em 22.20.0)
- Conta Supabase com schema `hakutaku` aplicado (ver [migration inicial](supabase/migrations/0001_initial_schema.sql))
- Chaves: Anthropic + OpenAI

### 1. Backend

```bash
cd backend

# Setup do venv (uma vez)
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell
# ou: source .venv/bin/activate  # Unix
pip install -e .

# Configurar .env (copie .env.example e preencha)
cp .env.example .env
# Edite .env com:
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...
#   SUPABASE_URL=...
#   SUPABASE_KEY=...
#   SUPABASE_DB_URL=postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres
```

### 2. Pipeline de ingestão (CLI)

Coloque os 3 documentos em `data/inputs/`:
- `meeting_01_24-03.txt`
- `chat_25-29-03.txt`
- `meeting_02_28-03.txt`

```bash
# Pipeline completo: 3 docs → grafo + propostas
python -m scripts.run_full_pipeline --reset --reason

# Variantes:
python -m scripts.run_full_pipeline --reset                # só extração+ingest
python -m scripts.run_full_pipeline --reset --cross-link   # + cross-linker
python -m scripts.run_full_pipeline --reset --cross-link --reason  # tudo

# Demo "sem memória vs com memória":
python -m scripts.demo_learning

# Diagnóstico de conexão (sem custo LLM):
python -m scripts.diag_truncate
python -m scripts.diag_idle_timeout

# Extrair de um único arquivo:
python -m scripts.run_extraction --source data/inputs/meeting_01_24-03.txt --type meeting
```

Cada execução produz artefatos inspecionáveis:
- `data/extractions/{source_id}_{ts}.json` — output do extrator (entidades + relações)
- `data/graph_snapshots/{ts}_{label}.{json,html}` — snapshot do grafo (HTML interativo via Pyvis)
- `data/proposals/reasoning_cycle_{ts}.json` — findings + propostas geradas
- `data/logs/calls/{date}/{time}_{stage}_{uid}.json` — log de cada chamada LLM (input, output, tokens, custo, latência)
- `data/cache/llm/{sha256}.json` — cache de respostas LLM (idempotência local)

### 3. API (FastAPI)

```bash
cd backend
.venv\Scripts\activate
python -m uvicorn hakutaku.api.main:app --host 127.0.0.1 --port 8000 --app-dir src --reload
```

Endpoints (docs interativas em http://127.0.0.1:8000/docs):
- `GET /health` · `GET /stats` — saúde e contagens
- `GET /graph` — snapshot completo (entidades + relações)
- `GET /entities[?type=Person]` · `GET /entities/{id}` — listagem e detalhe (com histórico de eventos)
- `GET /proposals[?status_filter=open]` — propostas
- `PATCH /proposals/{id}/status` — atualizar status (accepted/dismissed/resolved)
- `POST /pipeline/ingest` — ingerir documento sincronamente (~30-60s)
- `POST /pipeline/reason` — disparar 1 ciclo de raciocínio
- `POST /pipeline/cross-link` — disparar cross-linker

### 4. Frontend (Next.js)

```bash
cd frontend
npm install     # (uma vez)
npm run dev     # http://localhost:3000
```

Páginas:
- `/` — dashboard com stats, propostas em aberto, botão para disparar raciocínio
- `/graph` — visualização react-flow do grafo, com filtro por tipo
- `/proposals` — listagem com filtros por status + actions (aceitar/descartar/resolver)

## Estrutura do projeto

```
hakutaku-mvp/
├── backend/
│   ├── src/hakutaku/
│   │   ├── adapters/       # ingestão por tipo de fonte (meeting, chat)
│   │   ├── extraction/     # extractor.py — orquestra prompt → LLM → ExtractionResult
│   │   ├── graph/          # repository.py (psycopg + pgvector) + ingester.py
│   │   ├── memory/         # entity_resolver, context_retriever, cross_linker
│   │   ├── reasoning/      # 6 detectores + orquestrador de propostas
│   │   ├── proposals/      # gerador via Sonnet
│   │   ├── llm/            # client (cache + log + retry) + prompt loader
│   │   ├── schemas/        # Pydantic — fonte de verdade da ontologia
│   │   ├── api/            # FastAPI app + view schemas
│   │   └── config.py       # settings via pydantic-settings
│   └── scripts/            # run_full_pipeline, demo_learning, diag_*, run_extraction
├── frontend/               # Next.js 14 (App Router + Tailwind + reactflow)
├── prompts/                # YAML versionados (extraction, entity_resolution, answers_question, proposals)
├── data/                   # artefatos regenerados (inputs versionados, resto gitignored)
├── docs/                   # SPEC, DECISIONS, ARCHITECTURE, PROMPTING
└── supabase/migrations/    # 0001_initial_schema.sql
```

## Filosofia

- **Visibilidade primeiro** — todo passo do pipeline gera artefatos JSON/HTML inspecionáveis em `data/`. Toda chamada LLM é logada com input completo, tokens e custo.
- **Structured output > free text** — extração e raciocínio retornam Pydantic já validado via `instructor`. Zero parsing de string.
- **Prompts como código** — YAML versionado em `prompts/`, jamais hardcoded.
- **Event sourcing** — `hakutaku.events` é a fonte da verdade do grafo; `entities.current_state` é projeção materializada regenerável.
- **Cache local determinístico** — SHA256(prompt + schema) → resposta em `data/cache/llm/`. Iteração barata, demos reproducíveis.
- **Diagnóstico antes de fix** — quando algo quebra, instrumenta antes de patchear (ver D006/D009/D012-D014 + scripts `diag_*.py`).

## Documentação

- [Especificação técnica](docs/SPEC.md) — ontologia (10 tipos, 14 relações), schema, modelo temporal
- [Log de decisões](docs/DECISIONS.md) — D001-D014 com Contexto/Decisão/Trade-off/Justificativa
- [Arquitetura](docs/ARCHITECTURE.md) — diagrama de fluxos e camadas
- [Estratégia de prompting](docs/PROMPTING.md) — versionamento, instructor, 4 prompts ativos

## Estado por fase

| Fase | Status | Validação |
|---|---|---|
| 0. Setup | ✅ | Estrutura + CLAUDE.md + DECISIONS.md inicial |
| 1. Ontologia + schema | ✅ | 7 tabelas + 10 tipos Pydantic + migration aplicada |
| 2. LLM wrapper + extração | ✅ | Cache + log + retry; extração end-to-end validada |
| 3. Grafo + entity resolution | ✅ | Funil 3 estágios; recalibração D009.1 documentada |
| 4. Memória + aprendizado | ✅ | context_retriever + cross_linker; demo_learning.py existe |
| 5. Reasoning + propostas | ✅ | 6 detectores + Sonnet → 5 propostas concretas validadas |
| 6. API FastAPI | ✅ | 14 rotas; smoke testadas com curl |
| 7. Frontend Next.js | ✅ | 4 páginas; build limpo |
| 8. Documentação | ✅ | README + SPEC + DECISIONS + ARCHITECTURE + PROMPTING |
