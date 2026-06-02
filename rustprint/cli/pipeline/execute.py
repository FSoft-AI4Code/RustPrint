import logging
import os
import subprocess
from pathlib import Path

from rustprint.src.translation.execution_revisor.temp_to_execution import convert_temp_to_execution_and_result

logger = logging.getLogger(__name__)


def _run_nextest(repo_path: Path) -> None:
    env = os.environ.copy()
    env["RUST_BACKTRACE"] = "full"
    env["NEXTEST_EXPERIMENTAL_LIBTEST_JSON"] = "1"
    temp_path = repo_path / "temp.jsonl"
    with open(temp_path, "w") as handle:
        subprocess.run(
            [
                "cargo",
                "nextest",
                "run",
                "--no-fail-fast",
                "--message-format",
                "libtest-json-plus",
                "--jobs",
                "2",
            ],
            cwd=str(repo_path),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )


def _process_repo(repo_path: Path) -> None:
    name = repo_path.name
    if not (repo_path / "Cargo.toml").is_file():
        logger.info("Skip %s (no Cargo.toml)", name)
        return
    if (repo_path / "result.json").is_file():
        logger.info("Skip %s (result.json exists)", name)
        return

    logger.info("--- %s ---", name)
    if not (repo_path / "temp.jsonl").is_file():
        _run_nextest(repo_path)
    try:
        convert_temp_to_execution_and_result(repo_path / "temp.jsonl")
    except Exception as exc:
        logger.warning("Conversion failed for %s: %s", name, exc)


def run(version, output_root) -> None:
    output_root = Path(output_root)
    version_dir = output_root / "translated_repos" / "execution-aware" / f"version_{version}"
    if not version_dir.is_dir():
        raise RuntimeError(f"Not a directory: {version_dir}")

    for repo_path in sorted(version_dir.iterdir()):
        if repo_path.is_dir():
            _process_repo(repo_path)


def run_single(repo_path) -> None:
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise RuntimeError(f"--single_repo path is not a directory: {repo_path}")
    _process_repo(repo_path)
