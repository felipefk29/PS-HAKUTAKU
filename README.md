# Hakutaku — Organizational Intelligence Layer

> Sistema que transforma conhecimento organizacional não-estruturado em um grafo ontológico que raciocina sobre si mesmo e propõe ações.
>
> Entrega para o desafio técnico da **Hakutaku AI**.

---

## A tese

Empresas geram conhecimento operacional o tempo todo — em reuniões, em chats, em documentos. Quase nada disso vira ação estruturada. Decisões se perdem. Riscos passam despercebidos. Tarefas ficam sem dono.

Existe um caminho lógico que liga **conhecimento bruto** (texto solto) a **ação operacional** (uma tarefa atribuída, um alerta sobre risco escalando), e esse caminho passa por modelagem ontológica + IA.

Este projeto é uma implementação ponta-a-ponta desse caminho.

**Demonstração canônica:** três documentos do desafio (reunião de kickoff TechNova → thread de chat → reunião de status) → grafo com **25 entidades, 24 relações, 79 eventos** → **6 findings detectados** → **5 propostas priorizadas em PT-BR** citando entidades e contexto reais.

---

## Como o sistema funciona em 30 segundos

```
Documentos brutos              Grafo ontológico             Propostas acionáveis
─────────────────              ─────────────────             ────────────────────

reunião de               →     Pessoas, Tarefas,      →     "Risco TechNova
kickoff (24/03)                Riscos, Decisões,             escalou para crítico
                               OpenQuestions,                e não tem mitigação
chat técnico                   Compromissos,                  ativa. Designar owner
(25–29/03)               →     Dependências.           →     para a task de service
                                                              credit antes da call
reunião de                     Conectados por                de 03/04."
status (28/03)           →     relações tipadas        →
                               (owns, mitigates,
                               affects, answers, …)
```

A cada novo documento, o sistema **acumula contexto**: entidades já conhecidas são reusadas em vez de duplicadas, decisões respondem perguntas levantadas em documentos anteriores, e o grafo fica mais denso e mais útil.

---

## Stack técnica

| Camada | Tecnologia | Justificativa |
|---|---|---|
| Backend | Python 3.11+, FastAPI, Pydantic v2 | Tipos fortes, ecossistema NLP, documentação automática |
| Extração + Reasoning | Claude Sonnet 4.5 | Tarefas críticas que exigem nuance |
| Entity Resolution + Cross-linking | Claude Haiku 4.5 | Decisões binárias repetitivas, modelo pequeno resolve |
| Embeddings | OpenAI `text-embedding-3-small` | Custo desprezível, qualidade suficiente |
| Structured output | `instructor` over Anthropic SDK | Elimina parsing de string, valida Pydantic em runtime |
| Banco | Supabase (Postgres 17 + pgvector + pg_trgm) | Postgres battle-tested, vector search nativo, painel de inspeção |
| Frontend | Next.js 14, TypeScript, Tailwind, shadcn/ui, react-flow | Stack moderna, react-flow é a melhor lib de grafos no navegador |

**Custo total da execução de validação:** $0.98 USD para processar 3 documentos completos com aprendizado, cross-linking e geração de 5 propostas.

---

## Arquitetura

```
┌──────────────────────────────────────────────────────────────────┐
│                      Frontend (Next.js 14)                       │
│   /  (dashboard)   ·   /graph  (react-flow)   ·   /proposals     │
└─────────────┬──────────────────────────────┬─────────────────────┘
              │ HTTP                          │
┌─────────────▼──────────────────────────────▼─────────────────────┐
│                    Backend API (FastAPI)                         │
│  /health · /stats · /graph · /entities · /proposals              │
│  POST /pipeline/{ingest, reason, cross-link}                     │
└─────────────┬──────────────────────────────┬─────────────────────┘
              │                              │
┌─────────────▼─────────────┐  ┌─────────────▼─────────────┐
│   Extraction pipeline     │  │   Reasoning + proposals   │
│ adapter → context block   │  │  6 detectors → Sonnet     │
│ → extractor → ingester    │  │  → ProposalsBatch         │
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

Diagrama detalhado e fluxos completos em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Os 4 pilares técnicos

### 1. Ontologia operacional, não exaustiva

Em vez de modelar tudo o que existe, modela apenas o que **direciona ação**. 10 tipos de entidade:

`Person` · `Project` · `Client` · `Task` · `Decision` · `Risk` · `OpenQuestion` · `Dependency` · `Commitment` · `BehavioralPattern`

Cada tipo tem estado, atributos calibrados e regras de quando criar. 14 relações tipadas conectam os tipos com domínio/contradomínio explícitos.

Ontologia completa em [`docs/SPEC.md`](docs/SPEC.md).

### 2. Modelo temporal por event sourcing

A tabela `events` é a fonte da verdade. Toda mudança vira um evento imutável:

```
Risk #0f7b098d (churn TechNova):
  24/03  entity_created      severity=high
  24/03  relation_added      affects → TechNova
  25/03  attribute_changed   severity: high → critical
  25/03  entity_merged       (LLM reconheceu duplicata do chat)
  28/03  entity_merged       (LLM reconheceu duplicata da reunião 2)
```

Isso permite responder perguntas como **"como esse risco evoluiu ao longo do tempo?"** ou **"quem decidiu o quê e em que ordem?"** — não só o estado atual.

### 3. Entity resolution em funil de 3 estágios

Quando uma entidade extraída precisa ser resolvida contra o grafo existente:

```
Score combinado (0.6 cosine pgvector + 0.4 trgm pg_trgm):
  > 0.92  →  auto-merge      (sem LLM)
  < 0.75  →  auto-create     (sem LLM)
  zona cinza (0.75–0.92)  →  Claude Haiku decide
```

**Resultado mensurável:** Marina aparece em 3 documentos como "Marina", "Marina Costa", "marina.costa" — vira **uma única entidade** com aliases consolidados. TechNova aparece em todos os documentos — vira **um único Client**.

A justificativa de cada merge é gravada no payload do evento `entity_merged`, com `decision_method`, `similarity_score` e `reasoning` do LLM. **Cada decisão é auditável.**

### 4. Aprendizado por retrieval (não por fine-tuning)

O sistema fica mais inteligente a cada documento processado, sem nenhum re-treinamento.

**Mecanismo:** antes de processar um documento novo, o sistema busca no grafo as entidades semanticamente relevantes (top-K via embedding similarity) + entidades modificadas recentemente + perguntas abertas + riscos críticos. Esses elementos viram um bloco de contexto injetado no prompt do extrator:

> *"Pessoas conhecidas: Marina Costa (engenheira backend, especialista Salesforce). Riscos abertos: churn TechNova severidade crítica desde 25/03. Perguntas sem resposta: REST vs GraphQL para nova API…"*

**Evidência empírica:** na reunião 1 (sistema "frio", grafo vazio), 0 entidades reusadas. Na reunião 2, com o grafo já populado, **9 de 15 entidades extraídas foram identificadas como já conhecidas (60%)**. O cross-linker conectou a OpenQuestion *"REST vs GraphQL?"* à Decision *"Usar REST"* tomada em outro documento.

---

## Demo visual

### Dashboard de propostas

![Dashboard de propostas](docs/evidence/proposals.png)

Cada proposta cita entidades reais do grafo, tem prioridade visível, e abre dialog com a justificativa rastreando exatamente para os nós e eventos que a originaram. **Não é IA mágica, é IA fundamentada.**

### Grafo organizacional

![Grafo](docs/evidence/graph.png)

Visualização interativa via react-flow. Nós coloridos por tipo. **As arestas verdes animadas representam relações `answers`** — descobertas pelo cross-linker conectando perguntas a decisões em documentos diferentes. É a evidência visual de que o sistema **raciocina cruzando fontes**.

---

## Quando o sistema chama LLM

Cinco momentos, com modelos calibrados por custo-benefício:

| Stage | Modelo | Quando | Frequência típica | Custo |
|---|---|---|---|---|
| **Extração** | Sonnet 4.5 | Por documento ingerido | 1 por doc | ~$0.07 |
| **Entity Resolution (zona cinza)** | Haiku 4.5 | Quando funil cai entre 0.75 e 0.92 | 0–10 por doc | ~$0.005 cada |
| **Cross-link verdict** | Haiku 4.5 | No fim de cada ciclo | ~3–10 por execução | ~$0.005 cada |
| **Proposals** | Sonnet 4.5 | Ao rodar reasoning cycle | 1 por execução | ~$0.04 |
| **Embeddings** | OpenAI text-embedding-3-small | Antes de extração e resolução | dezenas por pipeline | <$0.001 total |

**Total da execução de validação (3 docs + reasoning + cross-linking):** 203 chamadas, **$0.98 USD**.

Toda chamada passa por cache local SHA256 antes de bater na API. Cada chamada é logada com tokens, custo e latência em `data/logs/calls/`. Auditoria queryable também em `hakutaku.llm_calls` (Supabase).

---

## Como rodar

### Pré-requisitos

- Python 3.11+ (testado em 3.11.9)
- Node 18+ (testado em 22.20.0)
- Conta Supabase com schema `hakutaku` aplicado (migration em [`supabase/migrations/0001_initial_schema.sql`](supabase/migrations/0001_initial_schema.sql))
- Chaves de API: Anthropic + OpenAI

### Setup do backend

```bash
cd backend

# Cria venv (uma vez)
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell
# ou: source .venv/bin/activate  # Unix
pip install -e .

# Configurar credenciais
cp .env.example .env
# Editar .env com:
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...
#   SUPABASE_URL=https://<ref>.supabase.co
#   SUPABASE_KEY=<service_role>
#   SUPABASE_DB_URL=postgresql://postgres:<senha>@db.<ref>.supabase.co:5432/postgres
```

### Pipeline standalone (CLI)

Coloque os 3 documentos do desafio em `data/inputs/`:

```bash
# Pipeline completo: extração + entity resolution + cross-linking + reasoning
python -m scripts.run_full_pipeline --reset --cross-link --reason

# Variantes:
python -m scripts.run_full_pipeline --reset                # só extração + ingest
python -m scripts.run_full_pipeline --reset --reason       # + propostas
python -m scripts.run_full_pipeline --reset --cross-link   # + cross-linker

# Demo "sem memória vs com memória":
python -m scripts.demo_learning

# Diagnóstico de conexão (sem custo LLM):
python -m scripts.diag_truncate
python -m scripts.diag_idle_timeout
```

### API + Frontend (terminais separados)

```bash
# Terminal 1 — Backend API
cd backend
.venv\Scripts\activate
python -m uvicorn hakutaku.api.main:app --host 127.0.0.1 --port 8000 --app-dir src --reload
# Docs interativas: http://127.0.0.1:8000/docs

# Terminal 2 — Frontend
cd frontend
npm install
npm run dev
# UI: http://localhost:3000
```

### Artefatos gerados a cada execução

Toda etapa do pipeline produz arquivos inspecionáveis:

```
data/
├── extractions/{source_id}_{ts}.json      ← entidades + relações extraídas
├── graph_snapshots/{ts}_{label}.json      ← estado do grafo após cada doc
├── graph_snapshots/{ts}_{label}.html      ← visualização interativa Pyvis
├── proposals/reasoning_cycle_{ts}.json    ← findings + propostas geradas
├── logs/calls/{date}/{time}_{stage}.json  ← log por chamada LLM
└── cache/llm/{sha256}.json                ← cache local (idempotência)
```

**Princípio:** *visibilidade primeiro*. Toda decisão do sistema deixa rastro em arquivo, não só no banco.

---

## Estrutura do projeto

```
hakutaku-mvp/
├── backend/
│   ├── src/hakutaku/
│   │   ├── adapters/           # ingestão por tipo de fonte (meeting, chat)
│   │   ├── extraction/         # extractor.py — orquestra prompt → LLM → ExtractionResult
│   │   ├── graph/              # repository.py (psycopg + pgvector) + ingester.py
│   │   ├── memory/             # entity_resolver, context_retriever, cross_linker
│   │   ├── reasoning/          # 6 detectores SQL + orquestrador
│   │   ├── proposals/          # gerador via Sonnet
│   │   ├── llm/                # client (cache + log + retry) + prompt loader
│   │   ├── schemas/            # Pydantic — fonte de verdade da ontologia
│   │   ├── api/                # FastAPI app + view schemas
│   │   └── config.py           # settings via pydantic-settings
│   └── scripts/                # CLIs: run_full_pipeline, demo_learning, diag_*
├── frontend/                   # Next.js 14 (App Router + Tailwind + react-flow)
├── prompts/                    # YAML versionados (4 prompts ativos)
├── data/                       # artefatos regenerados (inputs versionados, resto gitignored)
├── docs/                       # SPEC, DECISIONS, ARCHITECTURE, PROMPTING, FINAL_REPORT
└── supabase/migrations/        # 0001_initial_schema.sql
```

---

## Documentação técnica

Cinco documentos densos, escritos durante o desenvolvimento (não retroativamente):

| Documento | Conteúdo |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | Ontologia formal: 10 tipos de entidade, 14 relações, modelo temporal, schema SQL |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | **D001–D015** — cada decisão técnica com Contexto / Decisão / Trade-off / Justificativa |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Diagramas de camadas, fluxos de ingestão e reasoning, limites |
| [`docs/PROMPTING.md`](docs/PROMPTING.md) | Versionamento YAML, instructor, cache, custo cache frio vs quente |
| [`docs/FINAL_REPORT.md`](docs/FINAL_REPORT.md) | Estado real por fase, custo agregado, limitações conhecidas, roadmap |

**O documento de decisões é central.** A Hakutaku enfatizou no enunciado que ele é tão importante quanto o código. Cada uma das 15 decisões traz o que foi escolhido, o que foi rejeitado, e por quê.

---

## Princípios de engenharia adotados

- **Visibilidade primeiro.** Todo passo do pipeline gera artefatos JSON/HTML inspecionáveis em `data/`. Toda chamada LLM é logada com input completo, tokens e custo. Nada é caixa-preta.
- **Structured output > free text.** Extração e raciocínio retornam Pydantic já validado via `instructor`. Zero parsing de string. `extra="forbid"` em todos os schemas — alucinação de campos vira `ValidationError`.
- **Prompts como código.** YAML versionado em `prompts/`, jamais hardcoded. Mudança de versão invalida cache automaticamente.
- **Event sourcing.** `hakutaku.events` é fonte da verdade do grafo; `entities.current_state` é projeção materializada regenerável a partir dos eventos.
- **Cache local determinístico.** SHA256(prompt + schema + modelo) → resposta em `data/cache/llm/`. Iteração barata, demos reprodutíveis.
- **Diagnóstico antes de fix.** Quando algo quebra, instrumenta antes de patchear. Scripts `diag_truncate.py` e `diag_idle_timeout.py` foram escritos pra isolar bugs antes de "consertar". Padrão documentado em `feedback_diagnose_before_fix.md` na memória do projeto.
- **Modelo certo para a tarefa certa.** Sonnet 4.5 para extração e raciocínio; Haiku 4.5 para decisões binárias repetitivas; OpenAI embeddings para vector search. *Model cascading* economizou ~60% do custo vs uso indiscriminado de Sonnet.

---


## Limitações conhecidas

Documentadas honestamente em [`docs/FINAL_REPORT.md`](docs/FINAL_REPORT.md). Resumo:

- **`demo_learning.py` end-to-end ficou parcial.** O script existe e foi instrumentado contra todos os bugs encontrados, mas a última tentativa de execução foi interrompida para preservar orçamento. A evidência do aprendizado está demonstrada qualitativamente nos snapshots da Fase 3 (60% das entidades da reunião 2 foram identificadas como já existentes) e nas arestas `answers` do cross-linker visíveis no grafo.
- **Detector de `BehavioralPattern` não implementado.** Schema da tabela `patterns` existe, tipo na ontologia também — o detector longitudinal (ex: "Pedro tem 3 commitments quebrados em 30 dias") fica como evolução futura.
- **Aprendizado de feedback não retroalimenta.** Painel `/proposals` permite aceitar/descartar/resolver, mas essas decisões só atualizam status. Não há ciclo onde "propostas descartadas" ajustam thresholds dos detectores.
- **Sem testes pytest, sem auth, sem rate limiting.** Validação foi via runs end-to-end + scripts diagnósticos. Pra produção, primeiros itens do roadmap.

---

## Roadmap (o que viria depois)

### Curto prazo (1–2 semanas)
- Suíte pytest cobrindo schemas, detectores, entity_resolver em zona cinza
- Auth via header `X-API-Key` no FastAPI
- Rate limiting com `slowapi`
- CI no GitHub Actions: lint (ruff) + type check (mypy) + tests
- Logging estruturado JSON

### Médio prazo (1–2 meses)
- Detector de `BehavioralPattern` (consultas longitudinais sobre `events`)
- Pipeline assíncrono: `POST /pipeline/ingest` retorna 202 + job_id; processa em worker
- Diff-based proposal updates (preservar aceitas, recalcular o resto)
- Dataset rotulado para calibração de entity_resolver
- Multi-tenant via `tenant_id` + RLS no Postgres
- Frontend: paginação, busca, drill-down em entidades, replay de events na timeline

### Longo prazo
- Fine-tuning do extrator em dataset interno (após acumular >100 docs)
- Migração para vector store dedicado (Pinecone/Weaviate) ao passar de ~10⁵ entidades
- Realtime updates via Supabase realtime — frontend escuta mudanças no grafo

---

## Caminhos de produto

Três trajetórias viáveis pra esta arquitetura virar produto:

**1. SaaS B2B para times de projeto** — plugins Slack/Meet/Notion, processa em background, painel mostra o estado. Modelo: $30–100/seat/mês.

**2. Camada de inteligência interna corporativa** — instalado on-prem ou cloud privada, conectado a Confluence/Jira/Slack/email, age como "second brain organizacional". Modelo: licenciamento $50k–500k/ano.

**3. Plataforma para auditoria e compliance** — especializada em detectar decisões com implicação regulatória, riscos não escalados, compromissos legais não cumpridos. Modelo: B2B vertical, ticket alto.

---

## Reconhecimentos

Desafio técnico proposto pela [**Hakutaku AI**](https://hakutaku.ai/). O enunciado pediu que candidatos "pensassem profundamente sobre o problema, tomassem decisões de modelagem fundamentadas, e construíssem algo que funcionasse end-to-end mesmo que com escopo reduzido". Esta entrega é minha tentativa honesta de fazer isso.

---

**Construído em ~48 horas por Felipe FK ([@felipefk29](https://github.com/felipefk29))**
**Repositório:** https://github.com/felipefk29/PS-HAKUTAKU