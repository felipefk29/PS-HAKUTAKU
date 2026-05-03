"""Pydantic schemas — fonte de verdade da ontologia em runtime.

Espelha o que está descrito em docs/SPEC.md e materializado em
supabase/migrations/0001_initial_schema.sql. Mudanças aqui devem ser
acompanhadas de migration e atualização do SPEC.
"""

from hakutaku.schemas.entities import (
    BehavioralPattern,
    BehavioralPatternStatus,
    Client,
    ClientState,
    ClientType,
    Commitment,
    CommitmentState,
    Decision,
    DecisionState,
    Dependency,
    DependencyState,
    Entity,
    OpenQuestion,
    OpenQuestionState,
    Person,
    Priority,
    Project,
    ProjectState,
    Risk,
    RiskSeverity,
    RiskState,
    Task,
    TaskState,
)
from hakutaku.schemas.events import Event, EventType
from hakutaku.schemas.extraction import ExtractedContent, ExtractionResult
from hakutaku.schemas.relations import ProposedRelation, Relation, RelationType

__all__ = [
    "BehavioralPattern",
    "BehavioralPatternStatus",
    "Client",
    "ClientState",
    "ClientType",
    "Commitment",
    "CommitmentState",
    "Decision",
    "DecisionState",
    "Dependency",
    "DependencyState",
    "Entity",
    "Event",
    "EventType",
    "ExtractedContent",
    "ExtractionResult",
    "OpenQuestion",
    "OpenQuestionState",
    "Person",
    "Priority",
    "Project",
    "ProjectState",
    "ProposedRelation",
    "Relation",
    "RelationType",
    "Risk",
    "RiskSeverity",
    "RiskState",
    "Task",
    "TaskState",
]
