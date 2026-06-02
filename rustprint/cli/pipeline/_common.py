import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from rustprint.src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class PipelineCreds:
    model: str
    api_key: str
    base_url: str


_logging_configured = False


def setup_backend_logging(verbose: bool = False) -> None:
    global _logging_configured
    if _logging_configured:
        return

    backend_logger = logging.getLogger("rustprint.src")

    if os.environ.get("RUSTPRINT_LIVE_LOG") == "1":
        backend_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        backend_logger.propagate = True
        logging.getLogger("httpx").setLevel(logging.WARNING)
        _logging_configured = True
        return

    from rustprint.src.dependency_analyzer.utils.logging_config import ColoredFormatter

    backend_logger.handlers = []
    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter())
    backend_logger.addHandler(handler)
    backend_logger.propagate = False
    level = logging.DEBUG if verbose else logging.INFO
    backend_logger.setLevel(level)
    handler.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _logging_configured = True


def discover_repos(folder, exclude: Optional[set] = None) -> List[Path]:
    folder = Path(folder)
    if not folder.is_dir():
        return []
    exclude = exclude or set()
    repos = [
        p
        for p in folder.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in exclude
    ]
    return sorted(repos, key=lambda p: p.name)


def _name_variants(repo_name: str) -> List[str]:
    return [repo_name, repo_name.replace("-", "_"), repo_name.replace("_", "-")]


def docs_dir_for_repo(base_dir, repo_name: str) -> Path:
    base_dir = Path(base_dir)
    for name in _name_variants(repo_name):
        candidate = base_dir / name / "docs"
        if candidate.is_dir():
            return candidate
    return base_dir / repo_name / "docs"


def c_docs_repo_dir(base_dir, repo_name: str) -> Path:
    base_dir = Path(base_dir)
    for name in _name_variants(repo_name):
        candidate = base_dir / name
        if candidate.is_dir():
            return candidate
    return base_dir / repo_name


def build_config(repo_path, output_dir, creds: PipelineCreds, docs_dir=None) -> Config:
    return Config.from_cli(
        repo_path=str(repo_path),
        output_dir=str(output_dir),
        llm_base_url=creds.base_url,
        llm_api_key=creds.api_key,
        main_model=creds.model,
        cluster_model=creds.model,
        docs_dir=str(docs_dir) if docs_dir is not None else None,
    )
