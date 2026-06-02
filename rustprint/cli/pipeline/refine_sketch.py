import asyncio
import logging
from pathlib import Path

from rustprint.cli.pipeline._common import PipelineCreds
from rustprint.src.config import Config

logger = logging.getLogger(__name__)


def run(version, output_root, creds: PipelineCreds, requirement_refine_iterations: int = 5, repo: str = "") -> None:
    output_root = Path(output_root)
    source_repo_dir = output_root / "translated_repos" / f"version_{version}"
    if not source_repo_dir.is_dir():
        raise RuntimeError(f"Source repository directory not found: {source_repo_dir}")

    config = Config.for_llm_only(
        llm_base_url=creds.base_url,
        llm_api_key=creds.api_key,
        model=creds.model,
        requirement_refine_iterations=requirement_refine_iterations,
    )

    from rustprint.src.documentation.rust.refinement import SketchRefinementOrchestrator

    agent = SketchRefinementOrchestrator(config=config, output_base_dir=str(output_root), version=version)
    if repo:
        asyncio.run(agent.refine_repository(repo))
    else:
        asyncio.run(agent.refine_all_repositories())
