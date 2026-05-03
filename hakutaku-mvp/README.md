# Hakutaku — Organizational Intelligence Layer

> Transforma documentos organizacionais não-estruturados em um grafo ontológico que raciocina sobre si mesmo e propõe ações.

## Visão geral

Hakutaku ingere transcrições de reunião e threads de chat, extrai entidades tipadas (pessoas, tasks, decisões, riscos, projetos) usando LLMs com structured output, modela tudo como um grafo no Supabase com pgvector, e raciocina sobre o grafo para detectar padrões — gargalos, decisões esquecidas, dependências implícitas — gerando propostas acionáveis. A cada novo documento processado, o contexto acumulado melhora a qualidade da extração e do raciocínio: o sistema aprende.

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.11+, FastAPI, Pydantic v2 |
| LLM | Claude Sonnet 4.5 (extração), Claude Haiku 4.5 (tarefas leves) |
| Structured output | `instructor` |
| Embeddings | OpenAI `text-embedding-3-small` |
| Banco | Supabase (Postgres + pgvector) |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind, shadcn/ui, react-flow |

## Como rodar

> ⚠️ Em desenvolvimento — instruções completas serão preenchidas nas fases finais.

```bash
# Backend (placeholder)
cd backend
uv sync
uvicorn hakutaku.api.main:app --reload

# Frontend (placeholder)
cd frontend
pnpm install
pnpm dev
```

## Estrutura do projeto

```
hakutaku-mvp/
├── backend/         # FastAPI + pipeline de extração/raciocínio
├── frontend/        # Next.js com visualização do grafo
├── prompts/         # Prompts versionados em YAML
├── data/            # Artefatos inspecionáveis (inputs, outputs, logs)
├── docs/            # Especificação, decisões, arquitetura
└── supabase/        # Migrations do schema Postgres
```

## Documentação

- [Especificação técnica](docs/SPEC.md) — ontologia, schema do grafo, contratos
- [Log de decisões](docs/DECISIONS.md) — trade-offs e justificativas técnicas
- [Arquitetura](docs/ARCHITECTURE.md) — diagrama de fluxos e componentes
- [Estratégia de prompting](docs/PROMPTING.md) — como prompts são estruturados, versionados e avaliados
