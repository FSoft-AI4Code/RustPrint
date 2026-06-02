import asyncio
import logging
import shutil
from pathlib import Path

from rustprint.cli.pipeline._common import (
    PipelineCreds,
    build_config,
    c_docs_repo_dir,
    discover_repos,
)
from rustprint.src.documentation.rust.sketch_doc_generator import SketchDocGenerator

logger = logging.getLogger(__name__)

_MODULE_TREE_FILES = ("module_tree.json", "first_module_tree.json")


def run(version, output_root, creds: PipelineCreds) -> None:
    output_root = Path(output_root)
    c_docs_version_dir = output_root / "c_docs" / "version_0"
    sketch_docs_version_dir = output_root / "sketch_docs" / f"version_{version}"
    translated_repos_version_dir = output_root / "translated_repos" / f"version_{version}"

    sketch_docs_version_dir.mkdir(parents=True, exist_ok=True)

    if not translated_repos_version_dir.is_dir():
        raise RuntimeError(
            f"Translated repos directory not found: {translated_repos_version_dir}"
        )

    repos = discover_repos(
        translated_repos_version_dir, exclude={"temp", "translated_repos"}
    )
    if not repos:
        raise RuntimeError(
            f"No translated repositories found in {translated_repos_version_dir}"
        )

    for repo_path in repos:
        repo_name = repo_path.name
        c_docs_path = c_docs_repo_dir(c_docs_version_dir, repo_name) / "docs"
        sketch_docs_path = sketch_docs_version_dir / repo_name / "docs"

        if sketch_docs_path.is_dir() and any(sketch_docs_path.glob("*.md")):
            logger.info("Skipping %s (sketch docs already exist)", repo_name)
            continue

        if not c_docs_path.is_dir():
            logger.info("Skipping %s (C documentation not found at %s)", repo_name, c_docs_path)
            continue

        logger.info("Generating sketch documentation for %s", repo_name)
        config = build_config(repo_path, sketch_docs_path, creds)
        generator = SketchDocGenerator(config)
        asyncio.run(
            generator.generate_documentation(
                repo_name=repo_name,
                c_docs_path=str(c_docs_path),
                rust_translated_path=str(repo_path),
                output_sketch_docs_path=str(sketch_docs_path),
            )
        )

        if not any(sketch_docs_path.glob("*.md")):
            raise RuntimeError(
                f"Rust documentation generation produced no markdown files for {repo_name} at {sketch_docs_path}"
            )

        for filename in _MODULE_TREE_FILES:
            source = c_docs_path / filename
            if source.is_file():
                sketch_docs_path.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, sketch_docs_path / filename)
