"""Camada de memória: entity resolution + retrieval contextual + cross-source linking."""

from hakutaku.memory.context_retriever import build_context_block
from hakutaku.memory.cross_linker import (
    CrossLinkerStats,
    LinkedAnswer,
    link_questions_to_decisions,
)
from hakutaku.memory.entity_resolver import (
    DEFAULT_THRESHOLD_HIGH,
    DEFAULT_THRESHOLD_LOW,
    ResolutionDecision,
    ResolverStats,
    resolve_entity,
)

__all__ = [
    "DEFAULT_THRESHOLD_HIGH",
    "DEFAULT_THRESHOLD_LOW",
    "CrossLinkerStats",
    "LinkedAnswer",
    "ResolutionDecision",
    "ResolverStats",
    "build_context_block",
    "link_questions_to_decisions",
    "resolve_entity",
]
