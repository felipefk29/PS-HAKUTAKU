"""Loader de prompts versionados em YAML.

Convenção: cada arquivo `prompts/<name>.yaml` tem campos `version`, `description`,
`system`, `user` (templates Python f-string-like — usam `str.format`).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from hakutaku.config import get_settings


@dataclass(frozen=True)
class Prompt:
    """Prompt carregado de YAML. `system` e `user` são templates `str.format`."""

    name: str
    version: str
    description: str
    system: str
    user: str

    def format(self, **kwargs: object) -> tuple[str, str]:
        """Aplica `str.format` em system e user com os mesmos kwargs.

        Args:
            **kwargs: variáveis para interpolar no template.

        Returns:
            Tupla `(system, user)` com placeholders substituídos.

        Raises:
            KeyError: se algum placeholder não for fornecido em kwargs.
        """
        return self.system.format(**kwargs), self.user.format(**kwargs)


@lru_cache(maxsize=32)
def load_prompt(name: str, prompts_dir: Path | None = None) -> Prompt:
    """Carrega `prompts/{name}.yaml` e devolve um `Prompt` imutável.

    Args:
        name: nome do prompt sem a extensão (ex.: "extraction").
        prompts_dir: diretório alternativo (uso em testes).

    Returns:
        Instância de `Prompt`.

    Raises:
        FileNotFoundError: se o YAML não existir.
        ValueError: se o YAML não tiver os campos obrigatórios.
    """
    base = prompts_dir or get_settings().prompts_dir
    path = base / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Prompt não encontrado: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(f"YAML inválido em {path}: esperado mapping no topo.")

    required = {"version", "system", "user"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"Prompt {path} está sem campos obrigatórios: {sorted(missing)}")

    return Prompt(
        name=name,
        version=str(raw["version"]),
        description=str(raw.get("description", "")),
        system=str(raw["system"]),
        user=str(raw["user"]),
    )
