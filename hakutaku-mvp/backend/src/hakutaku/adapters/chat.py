"""Adapter para threads de chat.

Estratégia: detectar mensagens com regex tolerante a múltiplos formatos
("[10:30] Pedro: ...", "Pedro [10:30]: ...", "Pedro (10:30 AM): ..."), produzir
lista estruturada em `metadata.messages`, e devolver o texto normalizado em
forma canônica `[HH:MM] Author: text` para o LLM.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from hakutaku.adapters.base import NormalizedDocument, SourceAdapter

# Regex unificado: tenta capturar autor + horário em qualquer ordem comum.
_MESSAGE_RE = re.compile(
    r"""
    ^\s*
    (?:
        \[(?P<ts1>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)\]\s*    # [10:30]
        (?P<auth1>[^:\n]+?)\s*:                                       # Author:
      |
        (?P<auth2>[^[\n:]+?)\s*\[(?P<ts2>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)\]\s*:  # Author [10:30]:
      |
        (?P<auth3>[^(\n:]+?)\s*\((?P<ts3>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)\)\s*:  # Author (10:30):
    )
    \s*(?P<body>.+?)
    (?=\n\s*(?:\[\d{1,2}:\d{2}|[^\n:]{1,40}?\s*[\[\(]?\d{1,2}:\d{2}|\Z))
    """,
    re.MULTILINE | re.DOTALL | re.VERBOSE,
)

_DATE_LABEL_RE = re.compile(
    r"^\s*(?:date|data)\s*[:\-]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_CHANNEL_RE = re.compile(
    r"^\s*(?:channel|canal|thread|sala)\s*[:\-]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


class ChatMessage(BaseModel):
    timestamp: str
    author: str
    text: str


class ChatAdapter(SourceAdapter):
    """Adapter para chats. Tolerante: se não casar nada, devolve raw como
    `normalized_content` e `metadata.messages = []`."""

    source_type = "chat"

    def parse(self, raw_text: str, *, hints: dict[str, Any] | None = None) -> NormalizedDocument:
        hints = hints or {}
        messages = self._extract_messages(raw_text)
        normalized = self._render_normalized(messages, fallback=raw_text)
        title = self._extract_title(raw_text, fallback=hints.get("title", "Untitled chat"))
        occurred_at = self._extract_occurred_at(raw_text)

        metadata: dict[str, Any] = {
            "channel": self._extract_channel(raw_text),
            "participants": sorted({m.author for m in messages}),
            "message_count": len(messages),
            "messages": [m.model_dump() for m in messages],
            "char_count": len(raw_text),
        }

        return NormalizedDocument(
            source_type="chat",
            title=title,
            raw_content=raw_text,
            normalized_content=normalized,
            metadata=metadata,
            occurred_at=occurred_at,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_messages(text: str) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for m in _MESSAGE_RE.finditer(text):
            author = m.group("auth1") or m.group("auth2") or m.group("auth3") or ""
            ts = m.group("ts1") or m.group("ts2") or m.group("ts3") or ""
            body = m.group("body") or ""
            author = author.strip()
            ts = ts.strip()
            body = re.sub(r"\s+", " ", body).strip()
            if author and body:
                messages.append(ChatMessage(timestamp=ts, author=author, text=body))
        return messages

    @staticmethod
    def _render_normalized(messages: list[ChatMessage], *, fallback: str) -> str:
        if not messages:
            # Sem parsing: passa o texto cru, normalizando whitespace.
            return re.sub(r"\n{3,}", "\n\n", fallback.strip())
        return "\n".join(f"[{m.timestamp}] {m.author}: {m.text}" for m in messages)

    @staticmethod
    def _extract_title(text: str, *, fallback: str) -> str:
        ch = _CHANNEL_RE.search(text)
        if ch:
            return f"Chat — {ch.group(1).strip()}"
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if 0 < len(first) < 120 and ":" not in first:
            return first
        return fallback

    @staticmethod
    def _extract_channel(text: str) -> str | None:
        ch = _CHANNEL_RE.search(text)
        return ch.group(1).strip() if ch else None

    @staticmethod
    def _extract_occurred_at(text: str) -> datetime | None:
        labeled = _DATE_LABEL_RE.search(text)
        candidate = labeled.group(1) if labeled else "\n".join(text.splitlines()[:20])
        m = _ISO_DATE_RE.search(candidate)
        if not m:
            return None
        try:
            return datetime.fromisoformat(m.group(1))
        except ValueError:
            return None
