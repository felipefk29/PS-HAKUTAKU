"""Adapters de ingestão: convertem fontes brutas em `NormalizedDocument`."""

from hakutaku.adapters.base import NormalizedDocument, SourceAdapter
from hakutaku.adapters.chat import ChatAdapter, ChatMessage
from hakutaku.adapters.meeting import MeetingAdapter

__all__ = [
    "ChatAdapter",
    "ChatMessage",
    "MeetingAdapter",
    "NormalizedDocument",
    "SourceAdapter",
]
