import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from rustprint.cli.pipeline._common import PipelineCreds, build_config

logger = logging.getLogger(__name__)

_DELETE_NAMES = (
    "cost.json",
    "cost.jsonl",
    "execution.jsonl",
    "result.json",
    "temp.jsonl",
    "completed.jsonl",
)
_README_NAMES = {"README.md", "readme.md", "Readme.md"}
_DIGIT_RE = re.compile(r"[0-9]")


def _pass_rate(result_path: Path) -> float:
    try:
        with open(result_path) as handle:
            return float(json.load(handle).get("pass_rate", 0))
    except Exception:
        return 0.0


def _fail_count(execution_jsonl: Path) -> int:
    count = 0
    try:
        with open(execution_jsonl) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("status") == "fail":
                        count += 1
                except Exception:
                    pass
    except Exception:
        return 0
    return count


def _completed_count(completed_jsonl: Path) -> int:
    if not completed_jsonl.is_file():
        return 0
    count = 0
    try:
        with open(completed_jsonl) as handle:
            for line in handle:
                if _DIGIT_RE.search(line):
                    count += 1
    except Exception:
        return 0
    return count


def _prepare_dst_repo(src_repo: Path, dst_repo: Path) -> None:
    shutil.copytree(src_repo, dst_repo)
    for path in dst_repo.rglob("*"):
        if path.is_file() and path.name in _DELETE_NAMES:
            path.unlink()
    for path in dst_repo.glob("*.rs"):
        if path.is_file():
            path.unlink()
    for path in dst_repo.glob("*.md"):
        if path.is_file() and path.name not in _README_NAMES:
            path.unlink()


def run(version, output_root, creds: PipelineCreds, max_tool_calls: int = 50) -> None:
    output_root = Path(output_root)
    translated_repos_dir = output_root / "translated_repos"
    next_version = int(version) + 1
    src_version_dir = translated_repos_dir / "execution-aware" / f"version_{version}"
    dst_version_dir = translated_repos_dir / "execution-aware" / f"version_{next_version}"

    if not src_version_dir.is_dir():
        raise RuntimeError(f"Source version directory not found: {src_version_dir}")

    dst_version_dir.mkdir(parents=True, exist_ok=True)

    from rustprint.src.translation.execution_revisor.execution_refine import run_execution_refinement

    processed = 0
    skipped = 0

    for src_repo in sorted(src_version_dir.iterdir()):
        if not src_repo.is_dir():
            continue
        repo_name = src_repo.name
        dst_repo = dst_version_dir / repo_name

        result_path = src_repo / "result.json"
        if result_path.is_file() and _pass_rate(result_path) == 1.0:
            logger.info("Skip %s (pass_rate=1.0) - copying result.json only", repo_name)
            dst_repo.mkdir(parents=True, exist_ok=True)
            shutil.copy(result_path, dst_repo / "result.json")
            skipped += 1
            continue

        if not (src_repo / "Cargo.toml").is_file():
            logger.info("Skip %s (no Cargo.toml)", repo_name)
            skipped += 1
            continue

        src_execution_jsonl = src_repo / "execution.jsonl"
        if not src_execution_jsonl.is_file():
            logger.info("Skip %s (no execution.jsonl in source)", repo_name)
            skipped += 1
            continue

        fail_count = _fail_count(src_execution_jsonl)

        if dst_repo.is_dir():
            completed_count = _completed_count(dst_repo / "completed.jsonl")
            if fail_count > 0 and completed_count >= fail_count:
                logger.info(
                    "Skip %s (all %d failing test(s) already completed)", repo_name, fail_count
                )
                skipped += 1
                continue
            logger.info(
                "--- resuming %s (%d/%d test(s) completed) ---",
                repo_name,
                completed_count,
                fail_count,
            )
        else:
            logger.info("--- copying %s ---", repo_name)
            _prepare_dst_repo(src_repo, dst_repo)

        logger.info("--- refining %s ---", repo_name)
        config = build_config(dst_repo, dst_repo.parent, creds)
        result = asyncio.run(
            run_execution_refinement(
                execution_jsonl_path=src_execution_jsonl,
                rust_workspace_path=str(dst_repo.resolve()),
                repo_name=repo_name,
                config=config,
                max_tool_calls=max_tool_calls,
            )
        )
        logger.info(
            "%s - total_failures=%d refined=%d failed=%d",
            repo_name,
            result["total_failures"],
            result["refined_count"],
            result["failed_count"],
        )
        processed += 1

    logger.info(
        "Execution refinement completed. Processed: %d, Skipped: %d, Output: %s",
        processed,
        skipped,
        dst_version_dir,
    )
