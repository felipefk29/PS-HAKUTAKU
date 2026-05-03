"""Configurações globais carregadas de `.env` e variáveis de ambiente.

Caminhos de dados são resolvidos relativamente à raiz do monorepo
(`hakutaku-mvp/`), não ao CWD — assim o app funciona indistintamente
rodado de `backend/` ou da raiz.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py → hakutaku → src → backend → hakutaku-mvp
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"


class Settings(BaseSettings):
    """Settings tipados. Validação acontece em `get_settings()`, não no import."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM providers ------------------------------------------------
    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")

    anthropic_model_heavy: str = Field(default="claude-sonnet-4-5")
    anthropic_model_light: str = Field(default="claude-haiku-4-5")
    openai_embedding_model: str = Field(default="text-embedding-3-small")

    # --- Supabase -----------------------------------------------------
    supabase_url: str = Field(default="", validation_alias="SUPABASE_URL")
    supabase_key: str = Field(default="", validation_alias="SUPABASE_KEY")
    supabase_db_url: str = Field(default="", validation_alias="SUPABASE_DB_URL")

    # --- App ----------------------------------------------------------
    log_level: str = Field(default="INFO")

    # --- Caminhos derivados (não vêm do .env) -------------------------
    project_root: Path = Field(default=PROJECT_ROOT, exclude=True)

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def prompts_dir(self) -> Path:
        return self.project_root / "prompts"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache" / "llm"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs" / "calls"

    @property
    def extractions_dir(self) -> Path:
        return self.data_dir / "extractions"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retorna instância singleton de Settings. Lazy para não falhar no import."""
    return Settings()
