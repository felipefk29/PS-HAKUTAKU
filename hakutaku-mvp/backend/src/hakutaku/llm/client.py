"""Wrapper unificado para Anthropic (geração) + OpenAI (embeddings).

Toda chamada LLM do projeto passa por aqui. Garante:

- **Cache em arquivo** sob `data/cache/llm/{sha256}.json` para idempotência
  e replay barato durante desenvolvimento.
- **Logging completo** em `data/logs/calls/{YYYY-MM-DD}/{HH-MM-SS}_{stage}_{uid}.json`
  com input, output, tokens, latência, custo e flag de cache_hit.
- **Retry exponencial** (3 tentativas) em erros de rede / rate limit / 5xx.
- **Cálculo de custo** com tabela de preços hardcoded em USD.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from collections.abc import Callable
from typing import Any, TypeVar

import anthropic
import instructor
import openai
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hakutaku.config import get_settings


# =====================================================================
# Pricing — USD por 1M de tokens. Atualizar quando preços mudarem.
# =====================================================================
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5":     {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5":      {"input": 1.00, "output":  5.00},
    "text-embedding-3-small": {"input": 0.02, "output":  0.00},
}


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Custo em USD a partir da tabela de preços. Retorna 0.0 se modelo desconhecido."""
    p = _PRICING.get(model)
    if p is None:
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


# =====================================================================
# Retry policy
# =====================================================================
_RETRYABLE_ANTHROPIC = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)
_RETRYABLE_OPENAI = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)
_RETRY_KWARGS = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)


T = TypeVar("T", bound=BaseModel)


# =====================================================================
# LLMClient
# =====================================================================
class LLMClient:
    """Cliente único para todas as operações de LLM do projeto."""

    def __init__(
        self,
        *,
        anthropic_api_key: str,
        openai_api_key: str,
        cache_dir: Path,
        logs_dir: Path,
    ) -> None:
        if not anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY ausente — preencha .env.")
        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY ausente — preencha .env.")

        self._anthropic = anthropic.Anthropic(api_key=anthropic_api_key)
        self._instructor = instructor.from_anthropic(self._anthropic)
        self._openai = openai.OpenAI(api_key=openai_api_key)

        self._cache_dir = cache_dir
        self._logs_dir = logs_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        # Sink opcional para persistir cada chamada também em hakutaku.llm_calls.
        # Wire-up via attach_db_sink(); fica None no LLMClient puro (uso em testes).
        self._db_sink: Callable[[dict[str, Any]], None] | None = None
        # source_id atual — setado pelo orquestrador antes de cada documento.
        # Permite que o sink saiba a qual fonte cada chamada pertence sem poluir
        # a API pública dos métodos extract/complete/embed.
        self._current_source_id: Any = None

    # ------------------------------------------------------------------
    # Hooks de persistência
    # ------------------------------------------------------------------
    def attach_db_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Registra callback que recebe o record completo de cada chamada.

        Usado pelo `run_full_pipeline.py` para gravar em `hakutaku.llm_calls`.
        Idempotente: chamar duas vezes substitui o sink anterior.
        """
        self._db_sink = sink

    def set_source_context(self, source_id: Any) -> None:
        """Define o `source_id` que será anexado aos próximos records.

        Pass `None` ao final do processamento de um documento para evitar
        que chamadas avulsas (ex.: testes) carreguem source_id errado.
        """
        self._current_source_id = source_id

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def extract_structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
        model: str,
        stage: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> tuple[T, dict[str, Any]]:
        """Chama Claude com `instructor` e devolve o Pydantic já validado.

        Returns:
            Tupla `(parsed_model, call_metadata)`.
        """
        schema_repr = json.dumps(
            response_model.model_json_schema(), sort_keys=True, ensure_ascii=False
        )
        cache_key = self._cache_key(
            kind="structured",
            model=model,
            system=system,
            user=user,
            extra=schema_repr,
            temperature=temperature,
        )

        cached = self._cache_get(cache_key)
        if cached is not None:
            parsed = response_model.model_validate(cached["output"])
            meta = self._meta_from_cache(cached, stage=stage, model=model)
            self._log_call(
                stage=stage,
                model=model,
                input_payload={"system": system, "user": user},
                output_payload=cached["output"],
                meta=meta,
            )
            return parsed, meta

        t0 = time.monotonic()
        parsed, raw = _retry_call(
            self._instructor.messages.create_with_completion,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
            response_model=response_model,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        in_tok = raw.usage.input_tokens
        out_tok = raw.usage.output_tokens
        meta: dict[str, Any] = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_usd": _compute_cost(model, in_tok, out_tok),
            "latency_ms": latency_ms,
            "cache_hit": False,
        }

        output_dict = parsed.model_dump(mode="json")
        self._cache_put(
            cache_key,
            {
                "output": output_dict,
                "meta": meta,
                "model": model,
                "stage": stage,
            },
        )
        self._log_call(
            stage=stage,
            model=model,
            input_payload={"system": system, "user": user},
            output_payload=output_dict,
            meta=meta,
        )
        return parsed, meta

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        stage: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> tuple[str, dict[str, Any]]:
        """Geração de texto livre. Cache só é seguro com temperature=0."""
        cache_key = self._cache_key(
            kind="text",
            model=model,
            system=system,
            user=user,
            extra="",
            temperature=temperature,
        )

        if temperature == 0.0:
            cached = self._cache_get(cache_key)
            if cached is not None:
                meta = self._meta_from_cache(cached, stage=stage, model=model)
                self._log_call(
                    stage=stage,
                    model=model,
                    input_payload={"system": system, "user": user},
                    output_payload={"text": cached["output"]},
                    meta=meta,
                )
                return cached["output"], meta

        t0 = time.monotonic()
        response = _retry_call(
            self._anthropic.messages.create,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        text = "".join(block.text for block in response.content if block.type == "text")
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        meta = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_usd": _compute_cost(model, in_tok, out_tok),
            "latency_ms": latency_ms,
            "cache_hit": False,
        }

        if temperature == 0.0:
            self._cache_put(cache_key, {"output": text, "meta": meta, "model": model, "stage": stage})

        self._log_call(
            stage=stage,
            model=model,
            input_payload={"system": system, "user": user},
            output_payload={"text": text},
            meta=meta,
        )
        return text, meta

    def embed(
        self,
        text: str,
        *,
        model: str = "text-embedding-3-small",
        stage: str = "embedding",
    ) -> tuple[list[float], dict[str, Any]]:
        """Embedding densamente cacheado — embeddings são determinísticos."""
        cache_key = self._cache_key(
            kind="embedding",
            model=model,
            system="",
            user=text,
            extra="",
            temperature=0.0,
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            meta = self._meta_from_cache(cached, stage=stage, model=model)
            # Não logamos embeddings cache hit por padrão (ruído alto, sem valor).
            return cached["output"], meta

        t0 = time.monotonic()
        response = _retry_call(
            self._openai.embeddings.create,
            model=model,
            input=text,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        vec = response.data[0].embedding
        in_tok = response.usage.total_tokens
        meta = {
            "input_tokens": in_tok,
            "output_tokens": 0,
            "cost_usd": _compute_cost(model, in_tok, 0),
            "latency_ms": latency_ms,
            "cache_hit": False,
        }

        self._cache_put(cache_key, {"output": vec, "meta": meta, "model": model, "stage": stage})
        self._log_call(
            stage=stage,
            model=model,
            input_payload={"text_preview": text[:200]},
            output_payload={"dim": len(vec)},
            meta=meta,
        )
        return vec, meta

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _cache_key(
        *,
        kind: str,
        model: str,
        system: str,
        user: str,
        extra: str,
        temperature: float,
    ) -> str:
        h = hashlib.sha256()
        for piece in (kind, model, str(temperature), system, user, extra):
            h.update(piece.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        path = self._cache_dir / f"{key}.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        path = self._cache_dir / f"{key}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _meta_from_cache(cached: dict[str, Any], *, stage: str, model: str) -> dict[str, Any]:
        prev = cached.get("meta", {})
        return {
            "input_tokens": prev.get("input_tokens", 0),
            "output_tokens": prev.get("output_tokens", 0),
            "cost_usd": 0.0,  # cache hit não custa
            "latency_ms": 0,
            "cache_hit": True,
        }

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log_call(
        self,
        *,
        stage: str,
        model: str,
        input_payload: dict[str, Any],
        output_payload: Any,
        meta: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc)
        day_dir = self._logs_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{now.strftime('%H-%M-%S')}_{stage}_{uuid.uuid4().hex[:8]}.json"
        record = {
            "stage": stage,
            "model": model,
            "input": input_payload,
            "output": output_payload,
            "input_tokens": meta["input_tokens"],
            "output_tokens": meta["output_tokens"],
            "cost_usd": meta["cost_usd"],
            "latency_ms": meta["latency_ms"],
            "cache_hit": meta["cache_hit"],
            "timestamp": now.isoformat(),
        }
        with (day_dir / filename).open("w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2, default=str)

        # Fan-out opcional para hakutaku.llm_calls. Falha do sink não pode
        # derrubar o pipeline — log em stderr e segue.
        if self._db_sink is not None:
            db_record = {
                **record,
                "source_id": self._current_source_id,
            }
            try:
                self._db_sink(db_record)
            except Exception as exc:  # pragma: no cover — defensivo
                import sys

                print(
                    f"[LLMClient] db_sink falhou ({type(exc).__name__}: {exc}); "
                    "log de arquivo preservado.",
                    file=sys.stderr,
                )


# =====================================================================
# Retry wrapper — aplicável a calls Anthropic e OpenAI uniformemente
# =====================================================================
@retry(
    retry=retry_if_exception_type(_RETRYABLE_ANTHROPIC + _RETRYABLE_OPENAI),
    **_RETRY_KWARGS,
)
def _retry_call(fn: Callable[..., Any], **kwargs: Any) -> Any:
    return fn(**kwargs)


# =====================================================================
# Singleton lazy
# =====================================================================
@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    s = get_settings()
    return LLMClient(
        anthropic_api_key=s.anthropic_api_key,
        openai_api_key=s.openai_api_key,
        cache_dir=s.cache_dir,
        logs_dir=s.logs_dir,
    )
