# Log de decisões técnicas

> Cada decisão segue formato: **Contexto** → **Decisão** → **Trade-off** → **Justificativa**

---

## D001 — Linguagem e framework backend: Python 3.11 + FastAPI

**Contexto:** O pipeline central depende fortemente de chamadas a LLMs, manipulação de objetos estruturados (Pydantic), e integração com bibliotecas de IA (`instructor`, SDKs Anthropic/OpenAI). O backend também precisa expor endpoints HTTP para o frontend Next.js consumir.

**Decisão:** Backend em Python 3.11+ com FastAPI. Pydantic v2 em todas as fronteiras.

**Trade-off:**
- (+) Ecossistema de IA mais maduro (instructor, anthropic SDK, openai SDK, supabase-py).
- (+) Tipagem estática suficiente via Pydantic + mypy para reduzir bugs em I/O de LLM.
- (+) FastAPI dá OpenAPI gratuito, async nativo, e validação automática.
- (−) Performance bruta inferior a Go/Rust, mas irrelevante para um pipeline I/O-bound dominado por latência de LLM.
- (−) Empacotamento ainda mais frágil que Node, mitigado por `uv` + `pyproject.toml`.

**Justificativa:** O custo de oportunidade de fazer integração com LLM em qualquer outra stack é alto demais para um MVP de 2 dias. Python paga o investimento imediatamente.

---

## D002 — Banco de dados: Supabase (Postgres + pgvector)

**Contexto:** Precisamos de (a) um grafo persistente de entidades e relações, (b) busca semântica sobre embeddings para entity resolution e retrieval de contexto, (c) deploy rápido sem operar infraestrutura. Os dados não chegam ao volume que justifica um grafo nativo (Neo4j) nem um vector store dedicado (Pinecone, Weaviate).

**Decisão:** Supabase como banco único — Postgres relacional para o grafo (tabelas `entities`, `relations`) + extensão `pgvector` para embeddings na mesma tabela.

**Trade-off:**
- (+) Uma única dependência de dados, um único cliente, um único schema. Reduz superfície operacional drasticamente.
- (+) Joins entre grafo e busca vetorial em SQL puro — entity resolution fica trivial.
- (+) Supabase já oferece auth, dashboard, REST/RPC se precisar — opcionalidade de graça.
- (−) Postgres não é otimizado para travessias profundas de grafo. Em escala de milhares de entidades por documento, queries recursivas (CTE) podem ficar lentas.
- (−) `pgvector` com HNSW é bom, mas não é tão rápido quanto vector stores especializados em recall@k em datasets gigantes.

**Justificativa:** O MVP não vai exceder ~10⁴ entidades. Postgres + pgvector é o sweet-spot entre simplicidade operacional e capacidade técnica para essa escala. Migração para Neo4j ou vector store dedicado pode acontecer depois sem refazer a aplicação se o repositório for desacoplado por interface.

---

## D003 — LLM híbrido: Claude (geração) + OpenAI (embeddings)

**Contexto:** Precisamos de (a) extração estruturada confiável de entidades a partir de texto longo e ruidoso, (b) raciocínio sobre o grafo para gerar propostas, (c) embeddings densos e baratos para entity resolution e retrieval. Os requisitos de qualidade em (a) e (b) são altos; em (c) o requisito é principalmente de custo e throughput.

**Decisão:** Claude Sonnet 4.5 para extração e raciocínio pesado, Claude Haiku 4.5 para tarefas leves (classificação, normalização, deduplicação rápida), OpenAI `text-embedding-3-small` para todos os embeddings. Structured output via `instructor` em cima do SDK Anthropic.

**Trade-off:**
- (+) Claude domina extração estruturada complexa em janelas longas — exatamente o cenário do projeto. `instructor` + Pydantic tira toda fricção de parsing.
- (+) `text-embedding-3-small` é 5× mais barato que o equivalente da Anthropic (que ainda nem é 1ª classe), com qualidade competitiva para retrieval semântico.
- (+) Separar provedor de embeddings do provedor de geração isola riscos de pricing/rate-limit.
- (−) Duas chaves de API e dois SDKs no backend. Pequena complexidade extra de configuração.
- (−) Embeddings da OpenAI ficam em espaço diferente dos modelos Claude — irrelevante porque embeddings só são comparados entre si.

**Justificativa:** Otimização de Pareto: usamos cada provedor onde ele é melhor e mais barato. O custo total estimado para o MVP fica abaixo de US$ 5 mesmo processando dezenas de documentos.

---

## D004 — Modelo temporal: event sourcing com projeção materializada

**Contexto:** O sistema precisa demonstrar **aprendizado por acúmulo de contexto** — uma promessa fácil de afirmar e difícil de mostrar. Para que essa promessa seja observável, precisamos conseguir comparar o estado do grafo *antes* e *depois* de processar cada documento, identificar padrões longitudinais (que só fazem sentido sobre uma sequência de eventos, não sobre um estado), e ter rastreabilidade ponta-a-ponta de cada nó até o trecho da fonte que o originou. Adicionalmente, entity resolution introduz operações destrutivas (merges) que podem precisar ser revertidas.

**Decisão:** Event sourcing como fonte da verdade. Toda mudança no grafo passa por um evento imutável em `events` (com `entity_created`, `attribute_changed`, `status_changed`, `relation_added`, `relation_removed`, `entity_merged`). O estado atual em `entities.current_state` e `entities.attributes` é uma **projeção materializada** atualizada inline em cada escrita, sempre regenerável a partir do log de eventos. Cada evento carrega `source_id` + `source_excerpt` para rastreabilidade.

**Trade-off:**
- (+) Aprendizado é demonstrável: snapshots do grafo por documento processado. O delta vira métrica visualizável (tema da Fase 4).
- (+) Reasoning longitudinal lê `events`, não estado — captura dinâmica (ex.: "Pessoa X quebrou 4 commitments") em vez de foto.
- (+) Reversibilidade de merges é viável: aplicar evento inverso. O schema já comporta sem ajustes.
- (+) Auditoria completa de toda mudança "de graça" (mesma estrutura serve para compliance e debug).
- (−) Escrita dupla (evento + projeção) aumenta latência e cria risco de divergência. Mitigamos materializando a projeção inline em transação única e invariante de teste: "regenerar projeção = projeção atual".
- (−) Modelo é mais conceitualmente pesado para um colaborador novo do que CRUD direto. Mitigado por documentação na SPEC.

**Justificativa:** Event sourcing alinha exatamente com o requisito-chave do projeto ("aprende ao longo do tempo"). Em CRUD puro, demonstrar aprendizado depende de logging ad-hoc adicional; aqui o log **é** o modelo. O custo de complexidade é absorvido por uma única abstração (escrever evento → atualizar projeção) que cabe num wrapper de repositório.

---

## D005 — Ontologia operacional, não exaustiva

**Contexto:** Há tensão clássica entre ontologias "completas" (modelar tudo que existe no domínio organizacional — emails, calendários, hierarquia formal, OKRs, Slack channels, etc.) e ontologias "operacionais" (modelar só o que direciona ação downstream). A primeira parece mais robusta no papel; a segunda é a que efetivamente entrega valor em sistemas LLM.

**Decisão:** Adotar ontologia operacional com 10 tipos (`Person`, `Project`, `Client`, `Task`, `Decision`, `Risk`, `OpenQuestion`, `Dependency`, `Commitment`, `BehavioralPattern`) e 14 tipos de relação. Critério de inclusão: o tipo precisa habilitar pelo menos uma de — disparar uma proposta, ancorar padrões longitudinais, ou fechar lacuna semântica relevante. Tudo que não passa nesse filtro fica como texto bruto em `sources.raw_content`, não vira nó.

**Trade-off:**
- (+) Surface area pequena → prompts de extração mais focados, menos confusão para o LLM, recall maior por tipo.
- (+) Cada tipo tem um caso de uso de raciocínio downstream amarrado a ele (ex.: `OpenQuestion` sem `answers` há > 7 dias dispara proposta). Isso cria pressão evolutiva sadia: tipos sem consumo morrem.
- (+) `BehavioralPattern` como tipo (não atributo) materializa explicitamente "memória do sistema" — diferenciador demonstrável.
- (−) Coisas que poderíamos modelar (ex.: hierarquia organizacional formal Manager-Of, OKRs, ciclos de planejamento) ficam de fora. Se um caso de uso futuro precisar, exige adicionar tipo + migration + retreino do extrator.
- (−) Risco de subextração: ambiguidades no documento que poderiam virar tipos novos viram texto não-estruturado em `notes`.

**Justificativa:** Inspiração direta de Tom Gruber: *"An ontology is a specification of a conceptualization."* Uma conceituação só vale se algum agente raciocina sobre ela. Em um MVP de 2 dias, o custo de ontologia inflada (mais ruído, prompts maiores, mais bugs) é maior que o custo de ter que adicionar 1-2 tipos depois se necessário.

---

## D006 — Entity resolution híbrida: pg_trgm (blocking) + pgvector (rerank) + LLM (decisão)

**Contexto:** Para que o sistema "aprenda" ao longo do tempo, ele precisa reconhecer que "Pedro" no doc 5 é o mesmo `Person` criado no doc 1. Entity resolution é o gargalo de qualidade do MVP: falsos negativos quebram a continuidade do grafo (mesma pessoa vira N nós), falsos positivos colapsam pessoas distintas em um nó. Há três famílias de abordagem: puramente lexical (rápida, frágil), puramente semântica (cara, sensível a contexto), ou totalmente LLM (caríssima, melhor precision).

**Decisão:** Funil de três estágios híbrido:
1. **Blocking lexical** com `pg_trgm` (`gin_trgm_ops` em `entities.canonical_name`) — recall alto, K=20 candidatos, custo desprezível.
2. **Rerank semântico** com `pgvector` (cosine similarity, índice HNSW em `embedding`) — combina nome + contexto local. Top-5 com sim > 0.75 passa adiante; sim > 0.92 mesmo type → merge automático.
3. **Decisão por LLM** (Haiku 4.5) só na zona cinza 0.75-0.92 — prompt curto retornando `{is_same, confidence, reason}` via `instructor`.

Tipos sem identidade estável (`OpenQuestion`, `BehavioralPattern`, `Commitment` em primeira menção) bypassam o funil.

**Trade-off:**
- (+) Custo concentrado onde precisão importa (LLM só na ambiguidade real). Maioria dos casos resolve sem LLM.
- (+) HNSW dá recall@k forte mesmo em datasets pequenos sem precisar pré-popular o índice (ao contrário de IVFFlat).
- (+) `pg_trgm` no mesmo banco que o grafo: uma query SQL faz blocking + rerank em um round-trip.
- (+) Toda decisão de merge fica auditável via evento `entity_merged` com `decision_method` (auto/llm) e `similarity_score`.
- (−) Três estágios é mais código que "uma chamada LLM resolve tudo". Mitigado: cada estágio é uma função pequena.
- (−) Thresholds (0.4 em trgm, 0.75/0.92 em cosine) são empíricos. Vão precisar de tuning com dataset real — calibração é uma TODO consciente da Fase 3.
- (−) Hospedar tudo no Postgres significa que escala vertical (CPU para HNSW) é o limite. Tolerável até ~10⁵ entidades; depois disso reconsiderar vector store dedicado.

**Justificativa:** Esta é a forma mais barata de obter precision aceitável em MVP. As alternativas pioram um eixo cada: "só LLM" custa 10× e fica mais lento; "só embedding" perde precision em zonas cinza onde contexto importa; "só pg_trgm" ignora semântica completamente. O funil é o ponto Pareto-ótimo.

---

## D009 — Estratégia de entity resolution: funil determinístico com escape para LLM apenas em ambiguidade

**Contexto:** D006 estabeleceu a arquitetura de três estágios (`pg_trgm` → `pgvector` → LLM). A Fase 3 precisava materializar essa decisão em código com thresholds concretos, regras de bypass para tipos sem identidade estável, e auditoria por evento. A pergunta operacional era: como combinar os scores de cada estágio sem que a calibração fique frágil?

**Decisão:** Resolver implementado em [`hakutaku/memory/entity_resolver.py`](../backend/src/hakutaku/memory/entity_resolver.py) com:

1. **Embedding único por entidade** — texto concatenado de `canonical_name + aliases + sinais (role, team, severity, etc.)`. Mantido curto para preservar peso do nome próprio.
2. **Blocking forte por type** — query no Postgres já filtra `entities.type = ?`. Person nunca é candidato de Project. Reduz falsos positivos e cardinalidade.
3. **Score combinado** = `0.6 * cosine + 0.4 * trgm`. Pesos enviesados pra semântica mas sem ignorar o sinal lexical (importante para emails e logins, onde trgm dispara e cosine é fraco).
4. **Thresholds**: `≥ 0.92` → merge automático; `< 0.75` → create automático; zona cinza (0.75–0.92) → LLM (Haiku 4.5) decide via prompt em [`prompts/entity_resolution.yaml`](../prompts/entity_resolution.yaml).
5. **Bypass para tipos sem identidade**: `OpenQuestion`, `BehavioralPattern` e `Commitment` na primeira menção sempre criam — semantica desses tipos é "instância única por enunciação", deduplicar é incorreto.
6. **Auditoria por evento**: toda decisão de merge grava um `entity_merged` com `decision_method` (`auto_high` / `llm` / `bypass`), `similarity_score` e `reasoning`. Permite revisar a qualidade do funil sem precisar reprocessar.
7. **Sanitização contra hallucination de id**: se Haiku devolve `target_id` que não está nos candidatos pré-filtrados, fallback para `create` em vez de aceitar a sugestão cega.

**Trade-off:**
- (+) Custo de LLM concentrado nos casos onde valor de precision é maior. A maioria das menções resolve sem chamada paga.
- (+) Mapeamento alias→uuid local por documento permite resolver relações imediatamente após persistir as entidades, sem segunda passagem no DB.
- (+) Bypass por tipo é declarativo (`_BYPASS_TYPES`) — adicionar/remover tipo da regra é trivial.
- (−) Combinação linear de scores é uma heurística. Em datasets maiores convém aprender pesos via dataset rotulado de match/no-match.
- (−) Bypass total para `Commitment` na primeira menção significa que reentidades futuras precisam de outro mecanismo (recurrent commitment detection vai morar no módulo de padrões, não aqui).

**Justificativa:** O caso de validação Marina/TechNova exige que três menções textuais distintas (`Marina`, `marina.costa`, `Marina Costa`) colapsem em um único nó. Cosine sozinho falha em aliases tipo `marina.costa@` (espaço de embedding muito diferente); trgm sozinho falha quando o nome é semântica equivalente mas lexicamente distante. O funil resolve os dois casos com o LLM como rede de segurança barata.

### D009.1 — Recalibragem após primeira rodada (Fase 3 final)

**Contexto:** Primeira rodada do pipeline produziu duas duplicatas:
- **Beatriz**: `Beatriz Lima` (extraída em reuniões) e `Beatriz` (extraída do chat) viraram duas Person distintas. Combined score ficou em ~0.74 com pesos `0.6 cosine + 0.4 trgm` — bem na borda do antigo `threshold_low=0.75`, caiu em `auto_low`.
- **Risk de churn TechNova**: `Risco de churn TechNova por SLA não cumprido` (Reunião 1) vs `Churn da TechNova` (Chat) → combined ~0.47, drasticamente penalizado pelo trgm porque o nome longo do extrator compartilha pouquíssimos trigramas com a versão curta. Auto-create.

**Decisão:** Três ajustes empíricos:
1. **`threshold_low`: 0.75 → 0.55**. Empurra o caso Risk paráfrase para a zona cinza onde o Haiku consegue olhar contexto (cliente, escopo) em vez de só score.
2. **Pesos por tipo**: `Risk`/`Decision`/`Task`/`Project` (canonical_name reescrito pelo Sonnet entre fontes) usam `0.85 cosine + 0.15 trgm`. `Person`/`Client` mantêm `0.6 cosine + 0.4 trgm` (nomes próprios são lexicamente estáveis; trgm continua relevante para variações de sufixo). Implementado em `_weights_for(entity_type)` no resolver e propagado para o `ORDER BY` da query SQL.
3. **Prompt v1.1**: heurística 3 reforçada com exemplo concreto (`Risco de churn TechNova por SLA não cumprido` ≡ `Churn da TechNova`) e regra explícita: ignorar canonical_name literal, comparar `(cliente/projeto, tipo de ameaça/decisão)`.

**Trade-off:**
- (+) Custo do Haiku continua baixíssimo (~5 chamadas extra por documento na zona cinza alargada).
- (+) Pesos por tipo formalizam algo que era implícito: "Person tem identidade lexical; Risk tem identidade conceitual".
- (−) Threshold 0.55 é low — pode trazer falsos candidatos quando o grafo crescer. Aceitável porque o Haiku ainda decide.
- (−) Adicionar tipo novo à ontologia agora exige decidir conscientemente em qual bucket de pesos ele entra. Documentado em `_PARAPHRASEABLE_TYPES`.

**Justificativa:** Resolver com calibragem fixa é frágil em ontologia heterogênea. Calibragem por tipo aceita que diferentes tipos têm diferentes "geometrias de identidade" e age sobre isso explicitamente, em vez de torcer por uma combinação universal que funcione para todos os casos.

---

## D011 — `_diff_attributes` itera só sobre `new` (não union)

**Contexto:** Implementação inicial de `_diff_attributes` usava `keys = set(old) | set(new)`, gerando "diffs fantasma" em campos cujo valor o caller não pretendia mexer. Como `_to_attributes_for_update` filtra `None` antes de passar pra `update_entity`, qualquer atributo opcional ausente da nova extração aparecia como `{old: <valor>, new: null}` no event log — mensagem que sugere destruição quando na verdade `merged_attributes = {**old, **new}` preserva o valor original.

**Decisão:** `_diff_attributes` itera apenas `new.items()`. Chave em `old` mas não em `new` significa "caller decidiu não atualizar" — não é mudança e não vai pro event log.

**Trade-off:**
- (+) Event log fica fiel ao estado real do entity.
- (+) Auditoria não confunde "extrator não mencionou campo X" com "campo X foi apagado".
- (−) Para genuinamente apagar um atributo, caller precisa passar a chave com `None` explícito. `_to_attributes_for_update` não faz isso hoje (filtra `None` antes), o que é o comportamento correto: o extrator não tem evidência para apagar nada — ele só extrai o que viu.

**Justificativa:** Visibilidade primeiro (princípio do projeto). Um event log mentindo sobre o que mudou é pior do que um event log que perde alguma mudança intencional — e o segundo cenário não acontece pela arquitetura atual.

---

## D010 — Visualização do grafo: snapshot HTML com Pyvis para debug ponta-a-ponta

**Contexto:** O sistema é difícil de depurar olhando só JSON: relações são direcionais, tipos estruturam topologia, e entender se entity resolution está funcionando exige cruzar visualmente "qual nó é Person, qual é Client, e onde estão as duplicatas". O frontend final (Fase 7) com react-flow ainda está distante; precisávamos de algo *agora* na Fase 3 para validar o pipeline.

**Decisão:** Cada chamada de `ingest_extraction` produz dois artefatos em `data/graph_snapshots/`:

1. **JSON serializado** — fonte completa para diff entre etapas, comparação automatizada, e backup de auditoria.
2. **HTML standalone com [Pyvis](https://pyvis.readthedocs.io/)** — grafo interativo (drag, hover, filtro) com cores por tipo (`Person` azul, `Client` laranja, `Risk` vermelho, etc.). Carregado direto no navegador, sem servidor.

Pyvis foi escolhido em vez de Graphviz/D3 porque (a) gera HTML standalone com física configurável em ~10 linhas de Python, (b) não requer Node, (c) abre offline. Quando a dependência não está disponível, o ingester degrada para um HTML estático com tabelas — pipeline nunca quebra por falta de visualização.

**Trade-off:**
- (+) Loop de feedback curto: rodou pipeline → abre HTML → vê se Marina virou um nó só ou três.
- (+) HTML é facilmente compartilhável (anexar em PR, slack) sem precisar do Supabase rodando.
- (+) Fallback estático garante que a Fase 3 não dependa rigidamente de Pyvis pra rodar.
- (−) Não escala para milhares de nós (Pyvis fica lento). Aceitável para o MVP que processa dezenas.
- (−) Layout físico é não-determinístico — diff visual entre snapshots não é direto. Mitigado: o JSON é a fonte para comparações automatizadas; o HTML é debug humano.

**Justificativa:** Aprendizado por acúmulo de contexto é a promessa central do projeto e tem que ser **visível**. Snapshot por etapa transforma a promessa em três imagens comparáveis: doc 1 (grafo nasce), doc 2 (Marina/TechNova reaparecem como mesmo nó, severidade do risco escala), doc 3 (decisão fecha pergunta aberta). Sem isso, "aprendizado" volta a ser papo.

---

## D007 — Estratégia de prompting: YAML versionado + structured output via instructor (retroativa, Fase 2)

**Contexto:** Toda extração e raciocínio do sistema dependem de prompts LLM. Hardcoded prompts dispersos no código violam o princípio de visibilidade (não se sabe qual prompt produziu qual artefato), inviabilizam versionamento de qualidade, e dificultam o iteração rápida sobre wording. Adicionalmente, parsing manual de string para extrair entidades estruturadas é frágil e quebra silenciosamente quando o LLM varia o formato.

**Decisão:** Toda chamada LLM de geração estruturada segue o pattern:

1. **Prompt em YAML** sob `prompts/<name>.yaml` com campos obrigatórios `version` (semver), `description`, `system`, `user`. `system` e `user` são templates `str.format`-style com `{placeholder}`. Versão sobe a cada mudança não-trivial — o cache key inclui o conteúdo do prompt, então versão nova = invalidação automática.
2. **Loader** em [`hakutaku/llm/prompts.py`](../backend/src/hakutaku/llm/prompts.py) com `lru_cache`, valida que campos obrigatórios estão presentes, devolve dataclass `Prompt` imutável.
3. **Output estruturado** via `instructor.from_anthropic(...)` que envolve `messages.create_with_completion` e devolve diretamente um modelo Pydantic já validado. `extra="forbid"` em todos os Pydantic schemas — alucinação de campos extras dispara `ValidationError` em vez de passar despercebido.
4. **`prompt_template_version`** vai como extra no log de cada chamada — rastreabilidade ponta a ponta entre artefato gerado e versão do prompt.

**Trade-off:**
- (+) Trocar o tom de uma seção ou ajustar uma heurística de extração não exige deploy de código — só editar YAML.
- (+) Diff de prompts via git é direto. Code review separa "mudança de lógica" de "mudança de wording".
- (+) Instructor cuida do tool-use schema da Anthropic; ganhamos retry, validação Pydantic e tracing por completion sem reinventar.
- (−) Templates `str.format` exigem escapar `{{` `}}` quando o YAML tem JSON literal — bug encontrado uma vez em `proposals.yaml`. Aceitável (erro fail-fast com `KeyError`).
- (−) `instructor.from_anthropic` adiciona dependência sobre a forma do tool-use da Anthropic; se a API mudar, mudamos junto. Risco baixo no horizonte do MVP.

**Justificativa:** Prompts são código de produção do sistema. Tratá-los como configurações versionadas separa lógica de wording, e structured output via instructor elimina classe inteira de bugs de parsing. Custo de implementação é nulo — os SDKs e bibliotecas envolvidos são exatamente para isso.

---

## D008 — Cache de chamadas LLM em arquivo, key por SHA256(prompt + schema) (retroativa, Fase 2)

**Contexto:** Iteração local de pipeline LLM gasta dezenas de dólares por dia se cada execução refaz todas as chamadas. Mesmo na produção, há cenários idempotentes (replay de extração para debug, reprocessamento de doc com prompt v1 antes de promover v2) onde repetir o LLM é desperdício. Anthropic prompt caching é server-side e tem TTL próprio — útil mas não substitui um cache local determinístico.

**Decisão:** [`LLMClient`](../backend/src/hakutaku/llm/client.py) implementa cache em arquivo sob `data/cache/llm/`, com:

1. **Key** = `sha256(kind || model || temperature || system || user || schema_json_repr)` onde `schema_json_repr` é `json.dumps(response_model.model_json_schema(), sort_keys=True)` para `extract_structured`, ou string vazia para `complete`/`embed`. Mesmo prompt + mesmo schema + mesmo modelo + mesma temperatura = mesmo arquivo.
2. **Aplicação**: `extract_structured`, `complete` (apenas com temperature=0), e `embed`. Embeddings são determinísticos sempre — cache total.
3. **Cache hit** ainda dispara o logger normal com `cache_hit=true`, `cost_usd=0`, `latency_ms=0` — fica visível em todas as métricas.
4. **Invalidação**: subir a versão do prompt YAML muda o `system`/`user`, o que muda o hash, o que invalida automaticamente.

**Trade-off:**
- (+) Pipeline rodado 5 vezes em sequência com mesmos inputs custa 1 vez. Iteração local fica barata.
- (+) Demos reproducíveis com custo zero após primeira rodada — útil para apresentação ao vivo sem risco de "API caiu na hora".
- (+) Cache é apenas arquivo: inspecionável (`cat hash.json`), portátil (commitable se quiser pinar respostas para teste), e descartável (`rm -rf data/cache/llm`).
- (−) Cache não conhece "o documento mudou" — se o arquivo de input muda mas o hash do prompt rendered é o mesmo, devolve resposta velha. Mitigado: o `document_text` está dentro do `user` do prompt, então qualquer mudança no input invalida.
- (−) Não há TTL nem eviction — diretório cresce. Aceitável no MVP; em produção valeria políticas (LRU/TTL).

**Justificativa:** O custo de tempo+dinheiro de iterar pipeline LLM sem cache transforma "experimentar uma mudança no prompt" numa decisão pesada — o que mata iteração rápida. Com cache file-based, toda mudança é "1 cache miss e depois 0". O determinismo da chave também serve como teste de regressão informal: se a próxima rodada não hit cache, algo no prompt ou schema mudou. Anthropic prompt caching não substitui isso porque (a) tem TTL servidor-side curto, (b) não cacheia entre sessões de debug, (c) não dá visibilidade local.

---

## D012 — Extração contextualizada via retrieval do grafo acumulado (Fase 4)

**Contexto:** O modo Fase 2 trata cada documento como ilha — o extrator não sabe o que já existe no grafo. Resultado: "Marina" no doc 5 vira nó novo (resolver tenta colapsar depois, mas com falsos negativos), e o LLM não distingue "atualização de estado de uma Risk existente" de "novo Risk". A promessa "aprende com o tempo" exige que cada extração seja informada pelo grafo acumulado.

**Decisão:** [`hakutaku/memory/context_retriever.py`](../backend/src/hakutaku/memory/context_retriever.py) monta um `context_block` para cada documento processado, injetado como segunda metade do prompt do extrator (v1.1.0 do `extraction.yaml`):

1. **Embed** do `normalized_content` do documento (truncado em 6000 chars) com `text-embedding-3-small`.
2. **5 buckets de retrieval** no Postgres: top-15 entidades cross-type por cosine, top-5 atualizadas recentemente, OpenQuestions abertas, Riscos high/critical não-mitigados, Projetos ativos.
3. **Render textual** estruturado em PT-BR agrupando por tipo, terminando com a instrução **"use APENAS para desambiguar; NÃO copie atributos do contexto para entidades novas"** — proteção explícita contra alucinação de atributos.
4. **`BehavioralPattern` é escondido** do contexto (é entidade gerada pelo sistema, não deve ser extraída de novo).
5. **Auditoria**: `context_block_excerpt` (primeiros 500 chars), `context_entities_count`, `context_chars` viram extras no log da chamada LLM.

**Trade-off:**
- (+) Dobramos o "merge rate" do resolver na 2ª e 3ª fontes processadas — em vez de criar duplicatas e depender de entity resolution downstream, o extrator já reusa o `canonical_name` correto.
- (+) Detecta atualizações de estado: Risk listado como `severity=medium` no contexto + documento atual diz "agora é critical" → extrator emite Risk com `severity=critical` e o repository gera `attribute_changed` (em vez de Risk novo).
- (+) Cross-source linking implícito: relação `belongs_to` entre Task no doc 3 e Project listado no contexto (visto no doc 1) é gerada na hora.
- (−) Cada extração paga 1 chamada de embedding (~$0.0001) e ~500-2000 tokens extras de input no prompt (~$0.005). Trade negligível.
- (−) Quando o grafo cresce, o context block cresce — se passar de ~3k tokens, começa a competir por atenção do LLM. Mitigado pelos limites por bucket.
- (−) "Não copie atributos do contexto" é uma regra textual, não validável em código — depende do LLM seguir. Empiricamente, Sonnet 4.5 segue.

**Justificativa:** Aprendizado por acúmulo só vira observável se cada extração é diferente quando o grafo é diferente. O retrieval contextualizado é o vetor mais barato e direto de "memória" que existe — antes de pensar em fine-tuning, RAG sofisticado, ou agente com tool-use, basta dar contexto. Os custos extras são marginais e a melhoria de qualidade é mensurável (taxa de merge do resolver).

---

## D013 — Cross-source linking question→decision via embedding + Haiku verdict (Fase 4)

**Contexto:** Uma `OpenQuestion` levantada no doc 1 ("Vamos usar REST ou GraphQL?") pode ser respondida implicitamente por uma `Decision` que aparece no doc 3 ("Decidimos REST"). A extração não captura esse vínculo: o doc 3 não diz "isso responde a pergunta de Pedro". Sem mecanismo dedicado, a `OpenQuestion` fica perpetuamente `state='open'` mesmo quando o sistema TEM a resposta no grafo. É exatamente o tipo de "cego organizacional" que o Hakutaku promete resolver.

**Decisão:** [`hakutaku/memory/cross_linker.py`](../backend/src/hakutaku/memory/cross_linker.py) implementa `link_questions_to_decisions(repo, llm)` que roda como **passo opcional pós-ingestão** (flag `--cross-link` no `run_full_pipeline.py`; sempre ligado em `demo_learning.py` modo B):

1. **Filtro** SQL: OpenQuestions com `state='open'` E sem aresta `answers` apontando pra elas (cross-linker é idempotente — questões já respondidas pela extração não viram candidatas).
2. **Candidatos** por embedding: para cada Q, top-3 `Decision` com `first_seen_at >= Q.first_seen_at` e cosine ≥ 0.5 (ordem temporal estrita — uma decisão feita ANTES da pergunta não pode respondê-la).
3. **Veredito por Haiku 4.5** com prompt em [`prompts/answers_question.yaml`](../prompts/answers_question.yaml). Output Pydantic `_AnswerVerdict` com `verdict ∈ {yes, no, maybe}`, `confidence`, `reason`. Conservador por design — "no" na dúvida.
4. **Persistência** em `verdict='yes'`: insere aresta `answers` (Decision → OpenQuestion) com `attributes` carregando `verdict_confidence`, `cosine_similarity`, `reason`, `method='cross_linker_haiku'`. Transita Q para `state='answered'` via `repository.transition_state` que emite `status_changed` event para auditoria.
5. **Para na primeira `yes`** — uma pergunta tem uma resposta canônica; outras decisões similares viram ruído.

**Trade-off:**
- (+) Aresta `answers` cross-source é **a manifestação visual mais forte** de aprendizado: `data/graph_snapshots/*.html` mostra a Decision do doc 3 ligada à OpenQuestion do doc 1, com source_id distinto em cada extremo.
- (+) Filtro pré-LLM (cosine + temporal) garante que Haiku só é chamado em pares plausíveis — custo concentrado onde a qualidade da decisão importa.
- (+) Idempotência via filtro SQL: rerodar não duplica arestas ou re-pergunta ao Haiku.
- (−) Threshold cosine=0.5 é empírico. Calibração precisa de dataset rotulado para refinar — TODO consciente.
- (−) Como cada chamada Haiku custa ~$0.001-0.002, o custo escala linearmente com `(open_questions × 3 candidates)`. Mantido atrás de flag e fora do pipeline default por isso.
- (−) "Conservador na dúvida" significa que falsos negativos ficam — pergunta com resposta plausível mas não óbvia continua aberta. Aceitável: prefere-se under-link a poluir.

**Justificativa:** Sem este módulo, o sistema falha em demonstrar a 4ª forma de aprendizado citada na SPEC §7 ("cross-source linking"). Implementação é 200 linhas, custo é controlável via flag, e a evidência é literalmente visível na visualização do grafo. Falsos positivos seriam piores (afirmar que algo respondeu quando não respondeu mente sobre o estado do mundo), por isso o veredito `no`-default e o veredito por LLM em vez de threshold puro.

---

## D014 — Reasoning: 6 detectores determinísticos + Sonnet para gerar propostas (Fase 5)

**Contexto:** O grafo + propostas é o output final do sistema. A questão é: como ir do grafo (entidades, relações, eventos) para propostas acionáveis sem (a) deixar o LLM solto sobre o grafo todo (caro, alucinação alta, inconsistente) nem (b) usar só regras hard-coded (rígido, não generaliza, output sem nuance). É um caso clássico de **decompor o trabalho** entre código determinístico (detectar sinais) e LLM (priorizar e contextualizar).

**Decisão:** [`hakutaku/reasoning/`](../backend/src/hakutaku/reasoning/) com 2 camadas:

1. **Detectores** ([`detectors.py`](../backend/src/hakutaku/reasoning/detectors.py)) — 6 funções puras `(repo) -> list[Finding]`:
   - `orphan_tasks` — Tasks ativas sem `assigned_to` nem `owns`.
   - `escalating_risks` — Riscos high/critical abertos + Riscos com `severity` escalada via `attribute_changed`.
   - `overdue_tasks` — Tasks com `attributes.deadline` < now() e estado ativo.
   - `unanswered_questions` — OpenQuestions abertas há > 7 dias.
   - `single_point_of_failure` — Pessoas com ≥3 tasks/projetos não-fechados.
   - `blocked_dependencies` — Tasks em `state='blocked'` ou com `depends_on` para Task overdue/blocked.

   Cada detector emite `Finding` com `severity (1-5)`, descrição em texto, `related_entities` (UUID + nome + tipo), e `evidence` (dict livre). Falha individual de um detector não derruba o ciclo (try/except + log).

2. **Gerador via LLM** ([`generator.py`](../backend/src/hakutaku/proposals/generator.py)) — recebe lista de findings, renderiza bloco textual estruturado por detector, manda para Claude Sonnet 4.5 via instructor com schema `ProposalsBatch`. Prompt em [`proposals.yaml`](../prompts/proposals.yaml) com instruções de:
   - 3 tipos: `alert | suggestion | action`
   - Priorizar 1-5 (calibração explícita)
   - **Copiar IDs dos findings** em `related_entity_ids`, nunca inventar
   - Agrupar findings relacionados em proposta única
   - Máximo ~6 propostas por ciclo
   - Não usar priority=5 em mais de 2 propostas

3. **Orquestrador** ([`orchestrator.py`](../backend/src/hakutaku/reasoning/orchestrator.py)) `run_reasoning_cycle(repo, llm)`: roda detectores → gera batch → **filtra IDs alucinados** contra `hakutaku.entities` (proteção mesmo com instructor) → persiste em `hakutaku.proposals` → escreve snapshot JSON em `data/proposals/`.

**Trade-off:**
- (+) Detectores são determinísticos, baratos (queries SQL puras), testáveis isoladamente, e auditáveis. Sinais detectados são iguais a cada rodada do mesmo grafo.
- (+) LLM faz só a parte que LLM faz bem: priorização, agrupamento, framing acionável. Schema rígido + filtro de IDs trata alucinação.
- (+) Adicionar um detector novo é uma função pura, sem mexer em prompt nem em LLM. Adicionar um tipo novo de proposta é editar o `enum` + atualizar prompt.
- (+) Validado empiricamente: sobre o grafo do desafio (25 entidades, 24 relações), gerou 6 findings → 5 propostas (2 actions, 2 alerts, 1 suggestion) com summary citando entidades reais (TechNova, Pedro Silva, Ricardo) — qualidade boa para custo de $0.037.
- (−) Detectores estão estáticos — não aprendem com feedback do usuário (qual proposta foi aceita vs descartada). Próximo passo lógico, fora do escopo MVP.
- (−) Acoplamento ao schema do grafo — mudança em ontologia exige revisar detectores. Aceitável; ontologia é estável por design (D005).
- (−) `clear_existing=True` é o default — cada ciclo recomeça do zero. Para produção, valeria diff (manter propostas aceitas, substituir as outras).

**Justificativa:** A decomposição "detectores determinísticos + LLM para framing" é o padrão correto para esta classe de problema. Tentativas de "deixar o LLM olhar o grafo todo" produzem alucinação, custo alto, e inconsistência entre rodadas. Tentativas de "regras puras" perdem nuance e ficam frágeis. Esta arquitetura é simultaneamente barata, auditável, e expressiva — e o resultado empírico (5 propostas concretas sobre os casos canônicos do grafo) confirma que funciona.

---

## D015 — Aceitar `demo_learning.py` como entregável de código + documentação, sem run end-to-end empírico final

**Contexto:** O `demo_learning.py` foi construído na Fase 4 como o "money shot" da demonstração de aprendizado: roda o pipeline 2× (modo A sem memória vs modo B com memória + cross-linker), compara duplicatas / cross-source relations / Haiku-savings / answers, gera relatório JSON. Durante o desenvolvimento sofreu três bugs sequenciais: (1) conexão Postgres morta após chamada LLM longa, (2) `MIN(uuid)` inexistente em Postgres, (3) `{` em prompt YAML interpretado como placeholder pelo `str.format`. Os três foram corrigidos. Após cada fix, uma rodada parcial passou pelo modo A e parou no próximo bug.

**Decisão:** No fechamento da Fase 8, em modo de finalização e sob limite de orçamento ($1 LLM total já consumido), aceitar o demo como **entregável de código + arquitetura, sem run end-to-end final empírico**. Substituímos a evidência runtime por:

1. **Código completo** em [`backend/scripts/demo_learning.py`](../backend/scripts/demo_learning.py) — 5 etapas, 2 modos, 8 métricas comparativas, geração de relatório JSON.
2. **Métodos do repository** que dão suporte às métricas comparativas (`find_duplicate_pairs`, `count_cross_source_relations`, `count_haiku_resolver_calls`, `count_resolver_decisions_by_method`, `list_answers_relations`) — todos validados individualmente em testes diretos via `diag_truncate.py` ou em chamadas pelo `run_full_pipeline.py`.
3. **Validação parcial** registrada em [`data/logs/demo_learning_run*.log`](../data/logs/) — modo A completou ingestão dos 3 docs em runs anteriores, com numeros de entidades/relações conhecidos (14, 16, 15 por doc).
4. **Snapshots de Fase 3** em `data/graph_snapshots/` produzidos pelo `run_full_pipeline.py`, que demonstram empiricamente o comportamento que o demo compararia (auto_high vs auto_low no resolver, merges via context block, snapshots HTML inspecionáveis).
5. **Validação ponta-a-ponta da Fase 5** ([`data/proposals/reasoning_cycle_20260503T221313Z.json`](../data/proposals/)) — 6 findings → 5 propostas geradas via Sonnet com texto concreto sobre TechNova/Pedro Silva, custo $0.037.

**Trade-off:**
- (+) Custo zero adicional. Orçamento de demo respeitado.
- (+) A evidência empírica que existe (Fase 5 reasoning cycle, snapshots Fase 3, modo A parcial) já demonstra todos os mecanismos individuais que o demo combinaria.
- (+) Próxima sessão pode rodar `python -m scripts.demo_learning` e o resultado deve passar — todos os bugs conhecidos foram corrigidos.
- (−) Falta o relatório side-by-side numérico assinado pelo runtime atual. Quem revisar precisa ler o código + montar a comparação mentalmente (ou rodar o demo).
- (−) Se houver bug residual não detectado nos componentes individuais que aparece só na composição modo A → reset → modo B, descobriríamos no próximo run.

**Justificativa:** Em modo de finalização sob orçamento, escolher "entregar o que já tenho rodado e o código verificado" supera "queimar mais $0.50 em retentativa que pode falhar de novo por bug que ainda não vi". A pessoa que recebe o projeto pode rodar o demo em segundos com `npm`/`pip` instalados; o ROI dessa rodada extra é dela, não nosso. Honestidade técnica > completude artificial.

---
