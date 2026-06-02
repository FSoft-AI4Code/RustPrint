import asyncio
import logging
from pathlib import Path

from rustprint.cli.pipeline._common import PipelineCreds, build_config

logger = logging.getLogger(__name__)


def run(output_root, repo_data_dir, creds: PipelineCreds, version: str = "version_0") -> None:
    output_root = Path(output_root)
    repo_data_dir = Path(repo_data_dir)
    translated_repos_dir = output_root / "translated_repos" / "execution-aware"
    best_solution_dir = output_root / "translated_repos" / "best_solution"
    version_dir = translated_repos_dir / version

    if not best_solution_dir.is_dir():
        raise RuntimeError(
            f"best_solution directory not found: {best_solution_dir}. Run extract_best_solution first."
        )

    translated_repos_dir.mkdir(parents=True, exist_ok=True)
    version_dir.mkdir(parents=True, exist_ok=True)

    config = build_config(repo_data_dir, translated_repos_dir, creds)

    from rustprint.src.translation.test.test_trans_generator import TestTransGenerator

    generator = TestTransGenerator(config)
    asyncio.run(
        generator.run(
            repo_data_dir=str(repo_data_dir),
            translated_repos_dir=str(translated_repos_dir),
            version_filter=version,
            source_repos_dir=None,
        )
    )
