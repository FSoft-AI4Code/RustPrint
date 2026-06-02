import asyncio
import logging
from pathlib import Path

from rustprint.cli.adapters.doc_generator import CLIDocumentationGenerator
from rustprint.cli.pipeline._common import (
    PipelineCreds,
    build_config,
    discover_repos,
    docs_dir_for_repo,
)
from rustprint.src.translation.code.c2rust_generator import C2RustGenerator

logger = logging.getLogger(__name__)


def run_document(repos_folder, docs_output_dir, creds: PipelineCreds, verbose: bool = False) -> None:
    repos_folder = Path(repos_folder)
    docs_output_dir = Path(docs_output_dir)
    docs_output_dir.mkdir(parents=True, exist_ok=True)

    repos = discover_repos(repos_folder)
    if not repos:
        raise RuntimeError(f"No repositories found in {repos_folder}")

    for repo_path in repos:
        repo_name = repo_path.name
        logger.info("Generating documentation for %s", repo_name)
        generator = CLIDocumentationGenerator(
            repo_path=repo_path,
            output_dir=docs_output_dir,
            config={
                "main_model": creds.model,
                "cluster_model": creds.model,
                "base_url": creds.base_url,
                "api_key": creds.api_key,
            },
            verbose=verbose,
        )
        generator.generate()


def run_translate(repos_folder, docs_output_dir, translated_output_dir, creds: PipelineCreds) -> None:
    repos_folder = Path(repos_folder)
    docs_output_dir = Path(docs_output_dir)
    translated_output_dir = Path(translated_output_dir)
    translated_output_dir.mkdir(parents=True, exist_ok=True)

    repos = discover_repos(repos_folder)
    if not repos:
        raise RuntimeError(f"No repositories found in {repos_folder}")

    for repo_path in repos:
        repo_name = repo_path.name
        translated_repo_path = translated_output_dir / repo_name
        if translated_repo_path.is_dir():
            logger.info("Skipping %s (translation already exists)", repo_name)
            continue

        docs_repo_path = docs_dir_for_repo(docs_output_dir, repo_name)
        if not docs_repo_path.is_dir():
            raise RuntimeError(
                f"C docs path does not exist for {repo_name}: {docs_repo_path}. "
                "Run the documentation stage first."
            )

        logger.info("Translating %s to Rust skeleton", repo_name)
        config = build_config(repo_path, translated_output_dir, creds, docs_dir=docs_repo_path)
        generator = C2RustGenerator(config)
        asyncio.run(generator.run())
