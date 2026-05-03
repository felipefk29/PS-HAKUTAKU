"""Modelos Pydantic dos tipos de entidade da ontologia.

Cada classe corresponde a um tipo descrito em docs/SPEC.md §2. O campo
`type` é Literal — serve como discriminador na união `Entity` e como
validação contra typos. Atributos seguem a SPEC: obrigatórios sem default,
opcionais com `None` ou lista vazia.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# =====================================================================
# Enums de estado e severidade
# =====================================================================
class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ProjectState(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    CANCELLED = "cancelled"


class ClientType(str, Enum):
    CUSTOMER = "customer"
    PARTNER = "partner"
    VENDOR = "vendor"


class ClientState(str, Enum):
    PROSPECT = "prospect"
    ACTIVE = "active"
    CHURNED = "churned"


class TaskState(str, Enum):
    PROPOSED = "proposed"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class DecisionState(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REVERSED = "reversed"


class DecisionReversibility(str, Enum):
    ONE_WAY = "one_way"
    TWO_WAY = "two_way"


class RiskSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskLikelihood(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskState(str, Enum):
    IDENTIFIED = "identified"
    MITIGATED = "mitigated"
    ACCEPTED = "accepted"
    MATERIALIZED = "materialized"


class OpenQuestionState(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    ABANDONED = "abandoned"


class DependencyState(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"


class CommitmentState(str, Enum):
    PENDING = "pending"
    FULFILLED = "fulfilled"
    BROKEN = "broken"
    RENEGOTIATED = "renegotiated"


class BehavioralPatternStatus(str, Enum):
    EMERGING = "emerging"
    CONFIRMED = "confirmed"
    WEAKENING = "weakening"
    DISSOLVED = "dissolved"


# =====================================================================
# Base
# =====================================================================
class _EntityBase(BaseModel):
    """Campos comuns a toda entidade extraída.

    `id` só é preenchido após persistência; durante extração é None.
    `confidence` reflete certeza do extrator de que a entidade existe
    e está corretamente caracterizada.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: UUID | None = None
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list)
    source_excerpt: str = Field(
        min_length=1,
        description="Trecho da fonte que originou ou reforçou a entidade. Obrigatório.",
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# =====================================================================
# Tipos de entidade
# =====================================================================
class Person(_EntityBase):
    type: Literal["Person"] = "Person"
    role: str | None = None
    team: str | None = None
    email: str | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)


class Project(_EntityBase):
    type: Literal["Project"] = "Project"
    description: str | None = None
    start_date: date | None = None
    target_date: date | None = None
    tags: list[str] = Field(default_factory=list)
    state: ProjectState = ProjectState.ACTIVE


class Client(_EntityBase):
    type: Literal["Client"] = "Client"
    client_type: ClientType | None = None
    tier: str | None = None
    account_owner_alias: str | None = Field(
        default=None,
        description="Alias de uma Person que é o owner da conta. Resolvido em fase posterior.",
    )
    state: ClientState = ClientState.ACTIVE


class Task(_EntityBase):
    type: Literal["Task"] = "Task"
    description: str | None = None
    deadline: datetime | None = None
    priority: Priority = Priority.MEDIUM
    effort_estimate: str | None = None
    tags: list[str] = Field(default_factory=list)
    state: TaskState = TaskState.PROPOSED
    unowned: bool = Field(
        default=False,
        description="Flag derivada: True quando nenhuma Person tem assigned_to.",
    )


class Decision(_EntityBase):
    type: Literal["Decision"] = "Decision"
    rationale: str = Field(
        default="",
        description="Justificativa explicitada na fonte. Vazio significa: não havia rationale na fonte.",
    )
    decided_at: datetime | None = None
    reversibility: DecisionReversibility | None = None
    alternatives_considered: list[str] = Field(default_factory=list)
    state: DecisionState = DecisionState.CONFIRMED


class Risk(_EntityBase):
    type: Literal["Risk"] = "Risk"
    severity: RiskSeverity = RiskSeverity.MEDIUM
    likelihood: RiskLikelihood | None = None
    impact_description: str | None = None
    first_raised_at: datetime | None = None
    state: RiskState = RiskState.IDENTIFIED


class OpenQuestion(_EntityBase):
    type: Literal["OpenQuestion"] = "OpenQuestion"
    raised_by_alias: str | None = Field(
        default=None,
        description="Alias da Person que levantou. Resolvido em fase posterior.",
    )
    context: str | None = None
    state: OpenQuestionState = OpenQuestionState.OPEN


class Dependency(_EntityBase):
    """Dependência reificada — usar apenas quando precisa de tracking próprio.

    Para dependências simples, prefira aresta `depends_on`/`blocks` direta
    entre Tasks sem criar este nó.
    """

    type: Literal["Dependency"] = "Dependency"
    unblock_eta: datetime | None = None
    responsible_alias: str | None = None
    state: DependencyState = DependencyState.PENDING


class Commitment(_EntityBase):
    type: Literal["Commitment"] = "Commitment"
    committed_by_alias: str = Field(
        description="Alias da Person que fez a promessa. Obrigatório (sem committer não há commitment).",
    )
    committed_to_alias: str | None = Field(
        default=None,
        description="Alias da Person ou Client a quem foi prometido. Pode estar implícito.",
    )
    due_at: datetime | None = None
    state: CommitmentState = CommitmentState.PENDING


class BehavioralPattern(_EntityBase):
    """Padrão longitudinal detectado pelo sistema (NUNCA pelo extrator).

    Criado pelo módulo de raciocínio quando evidências cruzam threshold de
    confidence. É a manifestação física do "aprendizado" — entidade que só
    existe porque acumulamos contexto.
    """

    type: Literal["BehavioralPattern"] = "BehavioralPattern"
    pattern_kind: str = Field(
        description="Ex.: chronic_lateness, unowned_task_accumulator, decision_oscillation.",
    )
    subject_entity_id: UUID = Field(
        description="Entidade sobre a qual o padrão fala (geralmente Person ou Project).",
    )
    evidence_event_ids: list[UUID] = Field(default_factory=list)
    first_observed_at: datetime | None = None
    state: BehavioralPatternStatus = BehavioralPatternStatus.EMERGING


# =====================================================================
# União discriminada — usada por instructor para extração de tipo aberto
# =====================================================================
Entity = Annotated[
    Person
    | Project
    | Client
    | Task
    | Decision
    | Risk
    | OpenQuestion
    | Dependency
    | Commitment
    | BehavioralPattern,
    Field(discriminator="type"),
]
