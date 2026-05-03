# Estratégia de prompting — Hakutaku MVP

> Este documento será preenchido na **Fase 2** (LLM wrapper + extração end-to-end).

## Conteúdo previsto

1. **Estrutura padrão de prompt** — system + role + few-shot + schema + input. Versionado em YAML.
2. **Versionamento** — convenção de naming (`extraction.v1.yaml`) e como rastrear qual versão produziu qual artefato.
3. **Few-shot curation** — como escolhemos exemplos representativos sem leakage do dataset de teste.
4. **Avaliação** — como mediamos qualidade de extração (precision/recall por tipo de entidade) e quando consideramos uma versão "promovida".
5. **Custo e cache** — uso de prompt caching da Anthropic, estratégia de chunking para documentos longos.
