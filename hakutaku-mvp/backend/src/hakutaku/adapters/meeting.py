"""Adapter para transcrições de reunião.

Heurísticas leves (regex) para extrair metadados sem LLM. A normalização é
tolerante: se nenhum padrão bater, devolvemos o texto original como
`normalized_content` — o LLM lida.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from hakutaku.adapters.base import NormalizedDocument, SourceAdapter


_DATE_LABEL_RE = re.compile(
    r"^\s*(?:date|data)\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?\b")
_BR_DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")

_PARTICIPANTS_LABEL_RE = re.compile(
    r"^\s*(?:participants?|participantes?|attendees?|presentes?)\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Linhas tipo "[10:30] Pedro:" / "Pedro:" / "Pedro (10:30):"
_SPEAKER_LINE_RE = re.compile(
    r"^\s*(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?"
    r"(?P<name>[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç'.\- ]{0,40})"
    r"(?:\s*\(\d{1,2}:\d{2}(?::\d{2})?\))?\s*:\s",
    re.MULTILINE,
)

_TITLE_LABEL_RE = re.compile(
    r"^\s*(?:meeting|reunião|título|title|subject)\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _try_parse_date(value: str) -> datetime | None:
    iso = _ISO_DATE_RE.search(value)
    if iso:
        date_part, time_part = iso.group(1), iso.group(2)
        try:
            if time_part:
                return datetime.fromisoformat(f"{date_part}T{time_part}")
            return datetime.fromisoformat(date_part)
        except ValueError:
            pass
    br = _BR_DATE_RE.search(value)
    if br:
        d, m, y = br.group(1), br.group(2), br.group(3)
        try:
            return datetime.fromisoformat(f"{y}-{m}-{d}")
        except ValueError:
            return None
    return None


class MeetingAdapter(SourceAdapter):
    """Adapter para transcrições de reunião em texto livre.

    Tenta extrair: data, participantes, título. Se falhar, devolve o texto bruto
    com `metadata` mínima — não é função do adapter inferir o que falta.
    """

    source_type = "meeting"

    def parse(self, raw_text: str, *, hints: dict[str, Any] | None = None) -> NormalizedDocument:
        hints = hints or {}
        normalized = self._normalize_whitespace(raw_text)

        title = self._extract_title(raw_text, fallback=hints.get("title", "Untitled meeting"))
        occurred_at = self._extract_occurred_at(raw_text)
        participants = self._extract_participants(raw_text)

        metadata: dict[str, Any] = {
            "participants": participants,
            "speaker_turns": len(_SPEAKER_LINE_RE.findall(raw_text)),
            "char_count": len(raw_text),
        }

        return NormalizedDocument(
            source_type="meeting",
            title=title,
            raw_content=raw_text,
            normalized_content=normalized,
            metadata=metadata,
            occurred_at=occurred_at,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        # Colapsa runs de linhas em branco para no máximo uma; preserva quebras únicas.
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _extract_title(text: str, *, fallback: str) -> str:
        m = _TITLE_LABEL_RE.search(text)
        if m:
            return m.group(1).strip()
        first_non_empty = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if 0 < len(first_non_empty) < 120 and ":" not in first_non_empty:
            return first_non_empty
        return fallback

    @staticmethod
    def _extract_occurred_at(text: str) -> datetime | None:
        labeled = _DATE_LABEL_RE.search(text)
        if labeled:
            parsed = _try_parse_date(labeled.group(1))
            if parsed:
                return parsed
        # Fallback: qualquer data ISO/BR nas primeiras 30 linhas (header).
        head = "\n".join(text.splitlines()[:30])
        return _try_parse_date(head)

    @staticmethod
    def _extract_participants(text: str) -> list[str]:
        m = _PARTICIPANTS_LABEL_RE.search(text)
        if m:
            return [p.strip() for p in re.split(r"[,;]| e ", m.group(1)) if p.strip()]
        # Fallback: nomes únicos detectados como speakers.
        seen: list[str] = []
        for match in _SPEAKER_LINE_RE.finditer(text):
            name = match.group("name").strip()
            if name and name not in seen and len(name.split()) <= 3:
                seen.append(name)
        return seen
