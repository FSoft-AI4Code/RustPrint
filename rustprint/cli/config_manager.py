"""
Project-scoped TOML configuration for RustPrint.

The public CLI stores one config file per migrated C project at:

    ~/.config/rustprint/<project_name>

The file is TOML even though it intentionally has no suffix. Internal stage
commands still use ConfigManager.get_config()/get_api_key(), so this module keeps
that compatibility surface while changing the storage model.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any, Optional

from rustprint.cli.models.config import Configuration
from rustprint.cli.utils.errors import ConfigurationError, FileSystemError
from rustprint.cli.utils.fs import ensure_directory, safe_read


CONFIG_DIR = Path.home() / ".config" / "rustprint"


DEFAULT_PROJECT_CONFIG: dict[str, dict[str, Any]] = {
    "model": {
        "name": "gpt-5.4",
        "provider": "openai",
    },
    "source": {
        "path": "",
    },
    "api": {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
    },
    "output": {
        "base_dir": "~/rustprint-output",
        "cache": "~/.cache/rustprint",
    },
    "git": {
        "branch_enabled": False,
        "branch_name": "rustprint-migration",
        "commit": False,
    },
    "requirement_refinement": {
        "enabled": True,
        "rounds": 5,
    },
    "execution_refinement": {
        "enabled": True,
        "rounds": 5,
        "translate_tests": True,
    },
    "run": {
        "force": False,
    },
}


def project_name_from_path(path: Path) -> str:
    resolved = Path(path).expanduser().resolve()
    if not resolved.name:
        raise ConfigurationError(f"Cannot determine project name from path: {resolved}")
    return resolved.name


def config_path_for_project(project_name: str) -> Path:
    if not project_name or "/" in project_name:
        raise ConfigurationError(f"Invalid project config name: {project_name}")
    return CONFIG_DIR / project_name


def expand_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def resolve_project_dir(base: Path, project_name: str) -> Path:
    base = Path(base)
    if base.name == project_name:
        return base
    return base / project_name


def _escape_toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return f'"{_escape_toml_string(str(value))}"'


def dumps_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        if lines:
            lines.append("")
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_format_toml_value(value)}")
    return "\n".join(lines) + "\n"


def parse_toml_scalar(raw_value: str) -> Any:
    raw_value = raw_value.strip()
    try:
        return tomllib.loads(f"value = {raw_value}")["value"]
    except tomllib.TOMLDecodeError:
        return raw_value


def deep_merge_defaults(data: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_PROJECT_CONFIG)
    for section, values in data.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


class ConfigManager:
    """Manage the current project's RustPrint config file."""

    def __init__(self, project_path: Optional[Path | str] = None):
        self.project_path = Path(project_path or Path.cwd()).expanduser().resolve()
        self.project_name = project_name_from_path(self.project_path)
        self._api_key: Optional[str] = None
        self._config: Optional[Configuration] = None
        self._project_config: Optional[dict[str, Any]] = None

    @property
    def config_file_path(self) -> Path:
        return config_path_for_project(self.project_name)

    @property
    def keyring_available(self) -> bool:
        return False

    def config_exists(self) -> bool:
        return self.config_file_path.exists()

    def load(self) -> bool:
        """
        Load config for internal command compatibility.

        Explicit environment variables take precedence so subprocess-based
        pipeline stages can receive credentials from `rustprint migrate`.
        """
        env_api_key = os.environ.get("LLM_API_KEY") or os.environ.get("API_KEY")
        env_base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("BASE_URL")
        env_model = os.environ.get("MAIN_MODEL") or os.environ.get("MODEL") or ""

        if env_api_key and env_base_url:
            self._api_key = env_api_key
            self._config = Configuration(
                base_url=env_base_url,
                main_model=env_model,
                cluster_model=os.environ.get("CLUSTER_MODEL") or env_model,
                default_output="docs",
            )
            return True

        if not self.config_file_path.exists():
            return False

        data = self.load_project_config()
        self._load_from_project_config(data)
        return True

    def load_project_config(self) -> dict[str, Any]:
        if not self.config_file_path.exists():
            raise ConfigurationError(
                f"No RustPrint config found for project '{self.project_name}'.\n"
                "Run: rustprint init"
            )
        try:
            content = safe_read(self.config_file_path)
            parsed = tomllib.loads(content)
        except tomllib.TOMLDecodeError as e:
            raise ConfigurationError(f"Invalid TOML in {self.config_file_path}: {e}")
        except FileSystemError as e:
            raise ConfigurationError(str(e))

        self._project_config = deep_merge_defaults(parsed)
        return self._project_config

    def _load_from_project_config(self, data: dict[str, Any]) -> None:
        model = data["model"]
        api = data["api"]
        requirement = data["requirement_refinement"]
        execution = data["execution_refinement"]
        output = data["output"]

        self._api_key = os.environ.get("LLM_API_KEY") or api.get("api_key") or None
        model_name = model.get("name", "")
        self._config = Configuration(
            base_url=os.environ.get("LLM_BASE_URL") or api.get("base_url", ""),
            main_model=model_name,
            cluster_model=model_name,
            default_output=output.get("base_dir", "~/rustprint-output"),
            requirement_refine_iterations=int(requirement.get("rounds", 5)),
            max_tool_calls_per_test=50,
            translate_only=not bool(execution.get("enabled", True)),
        )

    def get_project_config(self) -> dict[str, Any]:
        if self._project_config is None:
            return self.load_project_config()
        return self._project_config

    def raw_toml(self) -> str:
        if not self.config_file_path.exists():
            raise ConfigurationError(
                f"No RustPrint config found for project '{self.project_name}'.\n"
                "Run: rustprint init"
            )
        return safe_read(self.config_file_path)

    def create_default_project_config(self, source_path: Path | str) -> dict[str, Any]:
        data = copy.deepcopy(DEFAULT_PROJECT_CONFIG)
        resolved = Path(source_path).expanduser().resolve()
        data["source"]["path"] = str(resolved)
        data["output"]["base_dir"] = f"~/rustprint-output/{resolved.name}"
        data["output"]["cache"] = f"~/.cache/rustprint/{resolved.name}"
        return data

    def write_project_config(self, data: dict[str, Any], overwrite: bool = False) -> Path:
        path = self.config_file_path
        if path.exists() and not overwrite:
            raise ConfigurationError(f"Config already exists: {path}")
        try:
            ensure_directory(CONFIG_DIR)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(dumps_toml(data), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
            os.chmod(path, 0o600)
        except OSError as e:
            raise ConfigurationError(f"Failed to write config {path}: {e}")
        self._project_config = deep_merge_defaults(data)
        self._load_from_project_config(self._project_config)
        return path

    def set_value(self, dotted_key: str, value: Any) -> Path:
        parts = dotted_key.split(".")
        if len(parts) < 2:
            raise ConfigurationError("Config key must be dotted, for example: model.name")

        data = self.load_project_config()
        cursor: dict[str, Any] = data
        for part in parts[:-1]:
            existing = cursor.get(part)
            if existing is None:
                existing = {}
                cursor[part] = existing
            if not isinstance(existing, dict):
                raise ConfigurationError(f"Cannot set nested key under non-table value: {part}")
            cursor = existing
        cursor[parts[-1]] = value
        return self.write_project_config(data, overwrite=True)

    def get_api_key(self) -> Optional[str]:
        if self._api_key is None:
            self.load()
        return self._api_key

    def get_config(self) -> Optional[Configuration]:
        if self._config is None:
            self.load()
        return self._config

    def is_configured(self) -> bool:
        config = self.get_config()
        return bool(config and config.base_url and self.get_api_key())
