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
