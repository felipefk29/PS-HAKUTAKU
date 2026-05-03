"""Camada de grafo: repositório Supabase + ingester orquestrador."""

from hakutaku.graph.ingester import IngestStats, ingest_extraction
from hakutaku.graph.repository import (
    EntityCandidate,
    EntityRecord,
    GraphRepository,
    get_repository,
)

__all__ = [
    "EntityCandidate",
    "EntityRecord",
    "GraphRepository",
    "IngestStats",
    "get_repository",
    "ingest_extraction",
]
