# Especificação do sistema — Hakutaku MVP

> Versão 0.1 — Fase 1 (ontologia + schema persistente).
> Toda mudança nesta spec deve ser refletida nos modelos Pydantic e na migration SQL.

---

## 1. Filosofia de modelagem

A ontologia é **operacional, não exaustiva**. O critério para um tipo entrar no sistema não é "existe no mundo organizacional" e sim "permite ação". Cada nó precisa habilitar pelo menos uma destas três coisas:

1. **Disparar uma proposta** — ex.: um `Risk` com severidade alta sem `mitigates` aciona uma proposta.
2. **Ser ponto de ancoragem para padrões longitudinais** — ex.: `Person` agrega histórico de `Commitment` ao longo de fontes para detectar "dívida crônica".
3. **Fechar uma lacuna semântica** — ex.: `OpenQuestion` torna observável o que normalmente é invisível: perguntas que ninguém respondeu.

A inspiração é Tom Gruber: *"An ontology is a specification of a conceptualization."* O sistema só precisa modelar o que o **raciocínio downstream** vai consumir. Tudo que não direciona ação é ruído — pode estar no `raw_content` da `source`, mas não vira nó.

**Consequências práticas:**
- Não modelamos "Email", "Documento", "Calendário" — fontes são tratadas como `sources`, não entidades de primeira classe no grafo.
- Não modelamos hierarquia organizacional formal (Manager-Of). Se for relevante, surge implicitamente em `decided_by` e `assigned_to`.
- Modelamos `BehavioralPattern` como entidade explícita (não só atributo de `Person`) porque padrões precisam ter histórico, confidence e justificação próprios.

---

## 2. Tipos de entidade

Convenções comuns a todos os tipos:
- `id: UUID` — identidade interna. Estável após entity resolution.
- `canonical_name: str` — nome de exibição preferencial. Evolui via merges.
- `aliases: list[str]` — variantes vistas em fontes (apelidos, abreviações, IDs externos).
- `confidence: float ∈ [0, 1]` — quão confiantes estamos de que essa entidade existe e está corretamente caracterizada.
- `source_excerpt: str` — trecho da fonte que originou ou reforçou a entidade (rastreabilidade obrigatória).
- `attributes: dict` — atributos abertos específicos do tipo, validados pelo schema do tipo.
- `current_state: dict` — projeção atual derivada da sequência de eventos.

### 2.1 `Person`

**Definição operacional:** indivíduo humano cujo nome aparece em pelo menos uma fonte e que pode possuir, decidir ou ser afetado por algo.

**Atributos:**
- Obrigatórios: `canonical_name`.
- Opcionais: `role` (texto livre, ex.: "PM", "Eng Lead"), `team`, `email`, `external_ids` (Slack, GitHub, etc.).

**Estados:** *(não aplicável)* — `Person` não tem ciclo de vida no MVP.

**Quando criar:** menção explícita por nome próprio em conteúdo da fonte. Pronomes e cargos genéricos ("o gerente") **não** geram `Person` até serem desambiguados.

---

### 2.2 `Project`

**Definição operacional:** iniciativa de trabalho com escopo nomeado, à qual outras entidades podem pertencer.

**Atributos:**
- Obrigatórios: `canonical_name`.
- Opcionais: `description`, `start_date`, `target_date`, `tags: list[str]`.

**Estados:** `proposed | active | paused | done | cancelled`.

**Quando criar:** referência explícita a uma iniciativa pelo nome ("projeto X", "iniciativa Y", "lançamento Z"). Frases como "o trabalho que estamos fazendo" só viram `Project` se houver nome consistente em pelo menos duas menções.

---

### 2.3 `Client`

**Definição operacional:** entidade externa (cliente, parceiro, fornecedor) à qual a organização entrega valor ou da qual depende.

**Atributos:**
- Obrigatórios: `canonical_name`.
- Opcionais: `client_type ∈ {customer, partner, vendor}`, `tier`, `account_owner` (referência a `Person`).

**Estados:** `prospect | active | churned`.

**Quando criar:** menção a uma organização externa. Distinguir de `Project`: se o nome se refere ao **trabalho feito para alguém**, é `Project`; se se refere a **quem é esse alguém**, é `Client`.

---

### 2.4 `Task`

**Definição operacional:** unidade de trabalho com (idealmente) owner e deadline. É a entidade mais frequente — o pulmão do sistema.

**Atributos:**
- Obrigatórios: `canonical_name` (descrição curta, imperativa: "preparar deck Q3").
- Opcionais: `description`, `deadline`, `priority ∈ {low, medium, high, critical}`, `effort_estimate`, `tags`.

**Estados:** `proposed | in_progress | blocked | done | cancelled`.

**Quando criar:** verbo de ação atribuído ou auto-atribuído ("eu vou X", "fulano vai Y", "precisamos Z"). Reconhecimento explícito de uma intenção de fazer. Reclamações genéricas sem ação ("isso está ruim") não viram `Task`.

**Estados especiais:**
- `unowned` — flag (não estado) quando `Task` existe sem `assigned_to`. Disparador de proposta.
- `overdue` — derivado: `deadline < now() AND status ∉ {done, cancelled}`.

---

### 2.5 `Decision`

**Definição operacional:** escolha tomada por alguém com poder, que altera a trajetória de um `Project` ou resolve uma `OpenQuestion`.

**Atributos:**
- Obrigatórios: `canonical_name` (resumo da decisão), `rationale` (mesmo que vazio, explícito).
- Opcionais: `decided_at: timestamp`, `reversibility ∈ {one_way, two_way}` (Bezos), `alternatives_considered: list[str]`.

**Estados:** `pending | confirmed | reversed`.

**Quando criar:** linguagem performativa de decisão ("decidimos X", "vamos com Y", "não vamos fazer Z"). Distinguir de opinião ("acho que deveríamos") — opinião só vira `Decision` quando há marca clara de fechamento.

---

### 2.6 `Risk`

**Definição operacional:** ameaça identificada com severidade e status — algo que pode dar errado e foi nomeado.

**Atributos:**
- Obrigatórios: `canonical_name`, `severity ∈ {low, medium, high, critical}`.
- Opcionais: `likelihood ∈ {low, medium, high}`, `impact_description`, `first_raised_at`.

**Estados:** `identified | mitigated | accepted | materialized`.

**Quando criar:** linguagem de hipótese negativa ("se X acontecer", "o risco é Y", "estou preocupado com Z"). Severidade default `medium` na ausência de evidência.

---

### 2.7 `OpenQuestion`

**Definição operacional:** pergunta levantada explicitamente sem resposta no momento da fonte.

**Atributos:**
- Obrigatórios: `canonical_name` (a pergunta em si).
- Opcionais: `raised_by` (referência a `Person`), `context` (texto livre).

**Estados:** `open | answered | abandoned`.

**Quando criar:** marca interrogativa explícita + ausência de resposta na mesma fonte. Esta é a entidade mais subutilizada em sistemas de notas — torná-la nó de primeira classe é diferenciador do Hakutaku.

---

### 2.8 `Dependency`

**Definição operacional:** relação reificada de bloqueio entre dois trabalhos, quando precisa ter atributos próprios (deadline interna, owner do desbloqueio).

**Atributos:**
- Obrigatórios: nenhum (atributos vêm da relação `depends_on`/`blocks`).
- Opcionais: `unblock_eta`, `responsible_for_unblock` (referência a `Person`).

**Estados:** `pending | resolved`.

**Quando criar:** **apenas** quando uma dependência precisa de tracking próprio (tem deadline, tem owner explícito, foi sinalizada como crítica). Caso contrário, modele como aresta `depends_on`/`blocks` direta entre `Task`s.

> Nota: `Dependency` como entidade reificada existe para o caso em que a aresta não comporta toda a informação. Se o MVP não precisar dessa nuance, mantenha apenas como aresta — criamos a entidade mas só populamos sob demanda.

---

### 2.9 `Commitment`

**Definição operacional:** promessa explícita de uma `Person` de fazer algo até um momento.

**Atributos:**
- Obrigatórios: `canonical_name` (a promessa), `committed_by` (referência a `Person`).
- Opcionais: `due_at`, `committed_to` (referência a `Person` ou `Client`).

**Estados:** `pending | fulfilled | broken | renegotiated`.

**Quando criar:** linguagem de primeira pessoa de promessa ("eu prometo", "te entrego", "fica comigo"). Distinguir de `Task`: toda `Commitment` tem `Task` correspondente, mas nem toda `Task` é `Commitment`. `Commitment` rastreia o **vínculo social**, `Task` rastreia o **trabalho**.

---

### 2.10 `BehavioralPattern`

**Definição operacional:** padrão longitudinal **detectado pelo sistema** (não extraído da fonte) que descreve regularidade no comportamento de uma `Person`, `Project` ou par.

**Atributos:**
- Obrigatórios: `canonical_name` (descrição do padrão), `pattern_kind` (ex.: `chronic_lateness`, `unowned_task_accumulator`, `decision_oscillation`), `subject_entity_id`.
- Opcionais: `evidence_event_ids: list[UUID]` (eventos que sustentam o padrão), `first_observed_at`.

**Estados:** `emerging | confirmed | weakening | dissolved`.

**Quando criar:** **nunca pelo extrator**. Criado pelo módulo de raciocínio quando um padrão atinge threshold de confidence (≥ 3 eventos consistentes em janela temporal). É a manifestação física do "aprendizado" do sistema — entidade que só existe porque acumulamos contexto.

---

## 3. Tipos de relação (arestas)

Toda relação tem `from_entity`, `to_entity`, `relation_type`, `attributes`, `source_id`, `confidence`. Cardinalidade descreve quantas instâncias da relação podem existir partindo do mesmo nó origem.

| Relação | Domínio (from) | Contradomínio (to) | Cardinalidade | Direcionalidade |
|---|---|---|---|---|
| `owns` | Person | Task, Project | many-to-many | dirigida |
| `assigned_to` | Task | Person | many-to-one* | dirigida |
| `decided_by` | Decision | Person | many-to-one | dirigida |
| `affects` | Risk, Decision | Project, Task | many-to-many | dirigida |
| `mitigates` | Decision, Task | Risk | many-to-many | dirigida |
| `depends_on` | Task, Project | Task, Project | many-to-many | dirigida (acíclica) |
| `blocks` | Task | Task | many-to-many | dirigida (inverso de depends_on) |
| `mentions` | Source | Entity (qualquer) | many-to-many | dirigida |
| `answers` | Decision | OpenQuestion | many-to-one | dirigida |
| `escalates_to` | Risk | Risk | one-to-one | dirigida (versão prévia → escalada) |
| `participates_in` | Person | Project | many-to-many | dirigida |
| `belongs_to` | Task | Project | many-to-one | dirigida |
| `commits_to` | Person | Commitment | one-to-many | dirigida |
| `exhibits` | Person, Project | BehavioralPattern | one-to-many | dirigida |

*`assigned_to` é tipicamente many-to-one (uma `Task` tem um responsável), mas o schema permite many-to-many para acomodar tasks compartilhadas — invariante many-to-one é validada no nível do extrator, não do banco.

**Regras de integridade:**
1. `depends_on` deve ser acíclica. Detecção é responsabilidade do reasoning (não enforced no DB pelo custo).
2. `blocks(A, B)` ⇔ `depends_on(B, A)`. Mantemos ambas para legibilidade nas queries; sincronização é responsabilidade da camada de aplicação.
3. `answers(d, q)` implica que `q.status` deve transicionar para `answered`.
4. `mentions` é registrada para **toda** entidade que aparece na fonte, mesmo que já existisse — é a base de retrieval temporal ("quando foi a última vez que X foi mencionado").

---

## 4. Modelo temporal (event sourcing)

O grafo é uma **projeção**. A fonte da verdade é a tabela `events`: sequência imutável e ordenada de mudanças. Toda escrita no grafo passa por evento.

**Por quê event sourcing:**
- **Aprendizado por acúmulo de contexto** vira observável: podemos mostrar o estado do grafo *antes* e *depois* de cada documento processado.
- **Reasoning longitudinal** é trivial: padrões detectados sobre eventos (não sobre estado) capturam dinâmica, não foto.
- **Auditoria e debug**: toda extração deixa rastro até o trecho da fonte (`source_excerpt` em cada evento).
- **Reversibilidade**: merges errôneos de entity resolution são desfazíveis ressequenciando eventos.

**Tipos de evento:**
- `entity_created` — payload: `{type, canonical_name, attributes, confidence}`.
- `attribute_changed` — payload: `{attribute, old_value, new_value, reason}`. Reason é "extracted" | "merged" | "user_edit".
- `status_changed` — payload: `{old_status, new_status, trigger}`. Cobre todas as transições de ciclo de vida.
- `relation_added` — payload: `{from_entity, to_entity, relation_type, attributes}`.
- `relation_removed` — payload: `{relation_id, reason}`.
- `entity_merged` — payload: `{merged_into: UUID, merged_from: UUID, similarity_score, decision_method}`. O `entity_id` do evento é o `merged_into`; `merged_from` é registrado como sobrescrito.

**Projeção:** `entities.current_state` e `entities.attributes` são reconstruídos via *fold* sobre `events` em ordem cronológica. No MVP, mantemos a projeção materializada (atualizada inline em cada escrita) pelo custo de não ter que folder a cada query. A projeção pode ser regenerada a qualquer momento a partir de `events` — esse é o teste de invariante.

**Ordenação:** `occurred_at` (quando aconteceu no mundo, derivado da fonte) ≠ `recorded_at` (quando o sistema soube). Reasoning usa `occurred_at`. Auditoria usa `recorded_at`.

---

## 5. Schema Supabase (SQL)

DDL completo materializa as seções 2-4. Ver [`supabase/migrations/0001_initial_schema.sql`](../supabase/migrations/0001_initial_schema.sql) para a fonte canônica. Resumo das tabelas:

| Tabela | Papel |
|---|---|
| `sources` | documentos brutos ingeridos (transcrições, threads de chat) |
| `entities` | nós do grafo (todos os tipos; discriminados por `type`) |
| `events` | log imutável de mudanças — fonte da verdade temporal |
| `relations` | arestas do grafo |
| `proposals` | output do reasoning (alertas, sugestões, ações propostas) |
| `patterns` | padrões longitudinais detectados (memória do sistema) |
| `llm_calls` | auditoria de toda chamada LLM (custo, latência, payloads) |

**Extensões habilitadas:**
- `vector` (pgvector) — embeddings densos para entity resolution e retrieval semântico.
- `pg_trgm` — similaridade de string para blocking rápido em entity resolution (etapa 1 do funil).

**Índices críticos:**
- `entities.canonical_name` com `gin_trgm_ops` — blocking por similaridade textual.
- `entities.embedding` com `ivfflat (vector_cosine_ops)` — recall@k semântico.
- `events.entity_id` e `events.occurred_at DESC` — reconstrução de timeline por entidade.
- `relations.from_entity`, `relations.to_entity`, `relations.relation_type` — travessias do grafo.

---

## 6. Estratégia de identidade e entity resolution

Funil de três estágios. Cada estágio descarta candidatos para o próximo estágio decidir com mais cara.

### Estágio 1 — Blocking (rápido, recall alto, precision baixa)
- Trigger: ao receber um candidato `(type, name, context)`.
- Query: `SELECT id FROM entities WHERE type = $1 AND similarity(canonical_name, $2) > 0.4 OR $2 = ANY(aliases)` (índice `gin_trgm_ops`).
- Output: lista de até K=20 candidatos.

### Estágio 2 — Re-ranking semântico (médio, ambos balanceados)
- Para cada candidato, calcular distância de cosseno entre `embedding` armazenado e `embedding(name + local_context)`.
- Manter top-5 com cosine similarity > 0.75.
- Se zero candidatos passam: criar nova entidade.
- Se 1 candidato com similaridade > 0.92 e mesmo type: merge automático sem LLM.

### Estágio 3 — Decisão por LLM (caro, precision alta)
- Acionado apenas para top-5 ambíguos (zona cinza 0.75-0.92).
- Prompt curto (Haiku 4.5): "São a mesma entidade? candidato_atual + alias_existente + 2 fontes de cada lado". Output structured: `{is_same: bool, confidence: float, reason: str}`.
- Decisão é registrada como evento `entity_merged` com `decision_method: "llm"` e o `confidence` do output.

**Confidence score do nó:** após resolução, `entities.confidence` é atualizado como média ponderada do score anterior e do score do match (mais menções → mais confiança).

**Histórico de merges:** todo merge gera `entity_merged` em `events` com `merged_from` preservado. Reversão = aplicar evento inverso (não implementado no MVP, mas o schema suporta).

**Quando NÃO resolver:** entidades de tipos sem identidade estável (`OpenQuestion`, `BehavioralPattern`, `Commitment` em primeira menção) bypassam o funil e sempre criam nó novo. Resolução para esses tipos é feita só por proximidade temporal estrita.

---

## 7. Memória e aprendizado

O sistema aprende ao longo do tempo via **quatro mecanismos**, todos demonstráveis no MVP processando documentos sequencialmente e mostrando o delta:

### 7.1 Entity resolution histórica
- Cada novo documento se ancora ao grafo existente — "Pedro" no doc 5 é resolvido contra o `Person` que foi criado no doc 1.
- **Demonstração:** doc 1 cria 12 entidades, doc 5 cria apenas 3 (resto é resolução). O ganho marginal de novas entidades cai com o tempo.

### 7.2 Extração contextualizada (graph-aware extraction)
- Antes de extrair de um documento novo, recuperamos do grafo as entidades mais relevantes (top-k por similaridade semântica entre embedding do doc e embeddings dos nós) e injetamos no prompt como contexto.
- **Demonstração:** uma `Task` que estava `unowned` no doc 1 e o doc 7 menciona "Maria vai pegar isso" é corretamente associada — o extrator vê a `Task` órfã no contexto e propõe `assigned_to`.

### 7.3 Padrões longitudinais
- Sobre `events`, o reasoning periódico procura regularidades: "Pessoa X tem 4 `Commitment` em estado `broken` nos últimos 30 dias", "Tipo Y de `Risk` é frequentemente seguido por `Decision` que muda escopo de `Project`".
- Cada padrão vira `BehavioralPattern` com confidence crescente a cada reforço.
- **Demonstração:** processar 5 documentos do mesmo trimestre faz emergir padrões que não existiam ao processar 1.

### 7.4 Cross-source linking
- A mesma `Decision` referida em uma transcrição de reunião e depois discutida em chat é a **mesma** entidade, com `mentions` apontando das duas fontes — habilita queries do tipo "todas as fontes que discutem decisão X".
- **Demonstração:** linha do tempo unificada por entidade, agregando todas as menções across modalidades.

**Invariante de aprendizado:** a curva (entidades novas / total entidades) por documento processado deve ser **decrescente**. Se for plana, a memória não está funcionando — virar teste de smoke da Fase 4.
