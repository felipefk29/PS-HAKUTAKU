"""Camada de memória: entity resolution + (futuro) padrões longitudinais."""

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
    "ResolutionDecision",
    "ResolverStats",
    "resolve_entity",
]
