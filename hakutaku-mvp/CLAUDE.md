# Contexto do projeto Hakutaku MVP

Sistema de inteligência organizacional que extrai entidades tipadas de documentos não-estruturados, modela como grafo, raciocina sobre o grafo, e gera propostas de ação. O sistema demonstra aprendizado por acúmulo de contexto.

## Stack
- Python 3.11+, FastAPI, Pydantic, Instructor, Anthropic SDK, OpenAI SDK (embeddings)
- Supabase (Postgres + pgvector)
- Next.js 14 + TypeScript + Tailwind + shadcn/ui + react-flow
- Claude Sonnet 4.5 (extração), Claude Haiku 4.5 (tarefas leves), OpenAI text-embedding-3-small

## Princípios de código
- **Visibilidade primeiro**: toda etapa do pipeline salva artefatos JSON/HTML em `data/`
- **Logging obrigatório**: toda chamada de LLM é logada em `data/logs/calls/{timestamp}_{stage}.json` com input completo, output, latência, custo estimado, modelo usado
- **Prompts em YAML, nunca hardcoded**: prompts ficam em `prompts/*.yaml`, carregados em runtime
- **Pydantic em todo I/O do LLM**: nada de parsing de string. Use `instructor` com schemas tipados
- **Structured output > free text**: extração sempre retorna objetos validados
- **Falhas explícitas**: tratamento de erros em pontos de LLM e DB. Log + retry + skip.
- **Tipagem forte**: type hints em tudo no Python, TS strict no frontend

## Convenções
- Snake_case para Python, camelCase para TS
- Docstrings em funções públicas (Google style)
- Imports absolutos: `from hakutaku.extraction import ...`
- Não use comentários óbvios. Comente só o "por quê", não o "o quê"

## Estado atual do projeto
[Atualize esta seção a cada fase concluída]

- [x] Setup inicial e estrutura
- [x] Fase 1: Ontologia + schema Supabase
- [x] Fase 2: LLM wrapper + extração end-to-end
- [x] Fase 3: Grafo + entity resolution
- [ ] Fase 4: Memória e aprendizado
- [ ] Fase 5: Reasoning e propostas
- [ ] Fase 6: API FastAPI
- [ ] Fase 7: Frontend Next.js
- [ ] Fase 8: Documentação final

## Infra persistente
- Supabase project: `mhlhcdzxqqlmejoionol` (felipefk29's Project) — instância compartilhada com outro projeto.
- Schema dedicado: **`hakutaku`** — TODA query do app deve usar `hakutaku.<table>` ou `SET search_path = hakutaku, extensions, public;`.
- Tabelas: `sources`, `entities`, `events`, `relations`, `proposals`, `patterns`, `llm_calls`.
- Extensões habilitadas: `vector`, `pg_trgm`, `pgcrypto`. Índices vetoriais usam HNSW.
