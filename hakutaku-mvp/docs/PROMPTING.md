# Estratégia de prompting — Hakutaku MVP

> Toda chamada LLM do projeto passa pelo `LLMClient` que cacheia, loga,
> retenta e calcula custo. Os prompts são versionados em YAML sob `prompts/`.

## 1. Estrutura padrão de prompt

Cada arquivo `prompts/<name>.yaml` segue o mesmo formato (validado pelo
[loader](../backend/src/hakutaku/llm/prompts.py)):

```yaml
version: "1.0.0"            # SemVer; sobre quando muda lógica
description: |
  O que faz, qual modelo, qual saída
system: |                   # Persona + ontologia + heurísticas + princípios
  ...
user: |                     # Template str.format com {placeholders}
  ...
```

- Templates usam `str.format` style. Para JSON literal no system, escapar `{{` `}}`.
- Loader é `lru_cache` — recarregar exige reiniciar o processo.
- Versão é parte implícita do cache key via conteúdo do prompt.
- Quando promove para versão nova, o cache antigo é automaticamente invalidado
  (hash diferente).

## 2. Versionamento

| Prompt | Versão | Stage | Modelo |
|---|---|---|---|
| [`extraction.yaml`](../prompts/extraction.yaml) | 1.1.0 | `extraction` | Claude Sonnet 4.5 |
| [`entity_resolution.yaml`](../prompts/entity_resolution.yaml) | 1.1.0 | `entity_resolution` | Claude Haiku 4.5 |
| [`answers_question.yaml`](../prompts/answers_question.yaml) | 1.0.0 | `cross_link_answer` | Claude Haiku 4.5 |
| [`proposals.yaml`](../prompts/proposals.yaml) | 1.0.0 | `proposals_generation` | Claude Sonnet 4.5 |

Critério de bump:
- **patch** (1.0.0 → 1.0.1): typo, reforço de exemplo, sem mudança semântica.
- **minor** (1.0.0 → 1.1.0): seção nova, regra adicional, mudança comportamental
  retrocompatível. Ex.: `extraction.yaml` 1.0 → 1.1 adicionou seção sobre uso do
  context block (Fase 4); `entity_resolution.yaml` 1.0 → 1.1 reforçou heurística
  de Risk parafraseado (D009.1).
- **major** (1.0.0 → 2.0.0): rebranding total ou troca de output schema.

## 3. Structured output via instructor

Para todos os prompts de geração estruturada:

```python
content, call_meta = llm.extract_structured(
    system=system,
    user=user,
    response_model=ExtractedContent,   # Pydantic v2 com extra="forbid"
    model=settings.anthropic_model_heavy,
    stage="extraction",
    log_extras={
        "prompt_template_version": prompt.version,
        "context_block_excerpt": context_block[:500],
        "context_entities_count": ctx_meta.get("count", 0),
    },
)
```

- `instructor.from_anthropic` envolve Anthropic SDK e oferece `messages.create_with_completion`
  que devolve `(parsed_pydantic, raw_completion)`.
- `extra="forbid"` em todos os Pydantic schemas — alucinação de campos extras
  vira `ValidationError`, não passa silenciosamente.
- União discriminada `Entity` (10 tipos) usa `Field(discriminator="type")`.
  instructor + Anthropic tool-use lidam bem com oneOf+discriminator.
- Para campos UUID em outputs (ex.: `Proposal.related_entity_ids`), filtramos
  no orchestrator antes de persistir contra o que existe no grafo (proteção
  contra UUID alucinado).

## 4. Cache + log + retry (LLMClient)

### Cache (D008)
- Key: `sha256(kind || model || temperature || system || user || schema_repr)`
- Localização: `data/cache/llm/{key}.json` com `{output, meta, model, stage}`
- Aplicado em `extract_structured`, `complete` (apenas temperature=0), `embed`
- Cache hit ainda dispara log com `cache_hit=true`, `cost_usd=0`, `latency_ms=0`
- Invalidação automática via mudança de `version` do prompt YAML

### Log (data/logs/calls)
Cada chamada → `data/logs/calls/{YYYY-MM-DD}/{HH-MM-SS}_{stage}_{uid}.json`:
```json
{
  "stage": "extraction",
  "model": "claude-sonnet-4-5",
  "input": { "system": "...", "user": "..." },
  "output": { ... },
  "input_tokens": 9917,
  "output_tokens": 3094,
  "cost_usd": 0.07616,
  "latency_ms": 34844,
  "cache_hit": false,
  "timestamp": "2026-05-03T...",
  "prompt_template_version": "1.1.0",
  "context_block_excerpt": "## Contexto organizacional acumulado\n...",
  "context_entities_count": 14
}
```

### DB sink
LLMClient pode anexar callback (`attach_db_sink`) que duplica o log em
`hakutaku.llm_calls` — auditoria queryable via SQL.

### Retry (tenacity)
- 3 tentativas, `wait_exponential(min=2, max=10)`
- Retentativa em: `APIConnectionError`, `APITimeoutError`, `RateLimitError`,
  `InternalServerError` (Anthropic e OpenAI separadamente)
- Fora do retry: exceções de validação Pydantic (são bugs nossos)

### Pricing (USD por 1M tokens)
- Sonnet 4.5: $3 input / $15 output
- Haiku 4.5: $1 input / $5 output
- text-embedding-3-small: $0.02 input

## 5. Few-shot curation

Não usamos few-shot tradicional (exemplos input→output no prompt).
Substituímos por:

1. **Schema rico via Pydantic** — `Field(description=...)` em cada campo
   gera descrições visíveis ao LLM via tool-use.
2. **Heurísticas explícitas no system prompt** — ver `entity_resolution.yaml`
   §"Heurísticas obrigatórias" com 6 regras numeradas e exemplos concretos
   embutidos.
3. **`source_excerpt` obrigatório** — força o LLM a citar trecho literal,
   reduzindo alucinação.
4. **`confidence` calibrado** — escala 0-1 com critérios explícitos por faixa.

Custo: prompts ficam mais longos (1-3k tokens), mas (a) caching server-side
da Anthropic atenua, (b) cache local nosso elimina repetição.

## 6. Avaliação (informal — sem dataset rotulado no MVP)

Sinais de qualidade que monitoramos por execução:
- **Extraction**: contagem de entidades por tipo (esperamos distribuição similar
  entre rodadas do mesmo doc); `confidence` médio > 0.7 (proxy de certeza).
- **Entity resolution**: distribuição `auto_high / auto_low / llm / bypass`. Quando
  o context block está ativo, esperamos `auto_high` subir e `llm` cair (D012).
- **Cross-linker**: `verdict_yes` deve ser baixo (decisão conservadora); presença
  de pelo menos 1 link `answers` no demo confirma funcionamento.
- **Reasoning**: número de findings vs número de propostas (esperamos agrupamento
  ~1:1.5 — várias findings por proposta); cita entidades reais (não inventadas).

Calibração futura (fora do escopo MVP): dataset rotulado de match/no-match para
entity resolution, e dataset de "esta proposta é boa/ruim" para tuning de
prompts de raciocínio.

## 7. Custo + cache na demonstração

Pipeline completo em cache **frio** (rodada totalmente nova):
- 3 extrações Sonnet: ~$0.20-0.25
- ~50 embeddings: ~$0.001
- ~6 Haiku resolver gray zone: ~$0.005
- 1 Sonnet reasoning: ~$0.04
- **Total ~$0.25-0.30**

Pipeline completo em cache **quente** (rerun mesmo conteúdo):
- 3 extrações cache hit: $0
- Embeddings cache hit: $0
- Resolver Haiku cache hit: $0
- Reasoning Sonnet **cache miss** (entity IDs mudam a cada reset): ~$0.04
- **Total ~$0.04**

Ou seja, iterar sobre prompts (mudar wording sem subir versão) custa zero
para extração; iterar sobre o gerador de propostas custa $0.04/rodada.
Subir versão de qualquer prompt invalida automaticamente o cache desse stage.
