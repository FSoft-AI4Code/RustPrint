from dataclasses import dataclass, asdict
from typing import Optional
from pathlib import Path

from rustprint.cli.utils.validation import (
    validate_url,
    validate_api_key,
    validate_model_name,
)


@dataclass
class Configuration:
    base_url: str
    main_model: str
    cluster_model: str
    default_output: str = "docs"
    requirement_refine_iterations: int = 5
    max_tool_calls_per_test: int = 50
    translate_only: bool = False

    def validate(self):
        validate_url(self.base_url)
        validate_model_name(self.main_model)
        validate_model_name(self.cluster_model)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Configuration':
        return cls(
            base_url=data.get('base_url', ''),
            main_model=data.get('main_model', ''),
            cluster_model=data.get('cluster_model', ''),
            default_output=data.get('default_output', 'docs'),
            requirement_refine_iterations=data.get('requirement_refine_iterations', 5),
            max_tool_calls_per_test=data.get('max_tool_calls_per_test', 50),
            translate_only=data.get('translate_only', False),
        )

    def is_complete(self) -> bool:
        return bool(self.base_url)

    def to_backend_config(self, repo_path: str, output_dir: str, api_key: str):
        from rustprint.src.config import Config

        return Config.from_cli(
            repo_path=repo_path,
            output_dir=output_dir,
            llm_base_url=self.base_url,
            llm_api_key=api_key,
            main_model=self.main_model,
            cluster_model=self.cluster_model,
            requirement_refine_iterations=self.requirement_refine_iterations,
            max_tool_calls_per_test=self.max_tool_calls_per_test,
            translate_only=self.translate_only,
        )
