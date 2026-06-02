import logging
import os
import subprocess
import sys
from pathlib import Path

from rustprint.cli.pipeline._common import PipelineCreds, c_docs_repo_dir, discover_repos

logger = logging.getLogger(__name__)

_BATCH_SIZE = 8
_RUBRICS_MAX_RETRIES = 3
_RUBRICS_TEMPERATURE = 0.1
_EVAL_MAX_RETRIES = 2
_COMBINATION_METHOD = "average"


def _eval_src_dir(benchmark_dir) -> Path:
    return Path(benchmark_dir) / "eval_sketch" / "src"


def _build_env(root_dir, eval_src_dir, creds: PipelineCreds) -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    parts = [str(root_dir), str(eval_src_dir)]
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["MODEL"] = creds.model
    return env


def _run(cmd, cwd, env) -> None:
    logger.info("$ %s", " ".join(str(c) for c in cmd))
    process = subprocess.Popen(
        [str(c) for c in cmd],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in process.stdout:
        line = line.rstrip()
        if line:
            logger.info(line)
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)


def _generate_rubrics(eval_src_dir, env, repo_name, c_docs_repo_dir_path, model) -> None:
    models = [model, model, model]
    for current in models:
        _run(
            [
                sys.executable,
                "rubrics_generator/generate_rubrics.py",
                "--repo-name",
                repo_name,
                "--model",
                current,
                "--data-dir",
                c_docs_repo_dir_path,
            ],
            cwd=eval_src_dir,
            env=env,
        )
    _run(
        [
            sys.executable,
            "rubrics_generator/combine_rubrics.py",
            "--repo-name",
            repo_name,
            "--model",
            models[0],
            "--data-dir",
            c_docs_repo_dir_path,
            "--temperature",
            _RUBRICS_TEMPERATURE,
            "--max-retries",
            _RUBRICS_MAX_RETRIES,
        ],
        cwd=eval_src_dir,
        env=env,
    )


def _run_evaluation(eval_src_dir, env, repo_name, c_docs_repo_dir_path, sketch_docs_repo_dir, model) -> None:
    models = [model, model, model]
    for current in models:
        _run(
            [
                sys.executable,
                "judge/judge.py",
                "--repo-name",
                repo_name,
                "--model",
                current,
                "--batch-size",
                _BATCH_SIZE,
                "--max-retries",
                _EVAL_MAX_RETRIES,
                "--original-dir",
                c_docs_repo_dir_path,
                "--generated-dir",
                sketch_docs_repo_dir,
                "--use-tools",
            ],
            cwd=eval_src_dir,
            env=env,
        )
    if len(models) > 1:
        _run(
            [
                sys.executable,
                "judge/combine_evaluations.py",
                "--repo-name",
                repo_name,
                "--method",
                _COMBINATION_METHOD,
                "--reference",
                "",
                "--generated-dir",
                sketch_docs_repo_dir,
            ],
            cwd=eval_src_dir,
            env=env,
        )


def run(version, output_root, root_dir, benchmark_dir, creds: PipelineCreds) -> None:
    output_root = Path(output_root)
    c_docs_dir = output_root / "c_docs" / "version_0"
    sketch_docs_dir = output_root / "sketch_docs" / f"version_{version}"
    eval_src_dir = _eval_src_dir(benchmark_dir)

    if not c_docs_dir.is_dir():
        raise RuntimeError(f"C docs directory does not exist: {c_docs_dir}")
    if not sketch_docs_dir.is_dir():
        raise RuntimeError(f"Sketch docs directory does not exist: {sketch_docs_dir}")
    if not eval_src_dir.is_dir():
        raise RuntimeError(f"Benchmark eval source not found: {eval_src_dir}")

    env = _build_env(root_dir, eval_src_dir, creds)
    repos = discover_repos(sketch_docs_dir)
    if not repos:
        raise RuntimeError(f"No repositories found in {sketch_docs_dir}")

    for sketch_docs_repo_dir in repos:
        repo_name = sketch_docs_repo_dir.name
        c_docs_repo_path = c_docs_repo_dir(c_docs_dir, repo_name)

        if not (sketch_docs_repo_dir / "docs").is_dir():
            logger.info("Skipping %s (sketch docs not found)", repo_name)
            continue

        logger.info("Evaluating FCV for %s", repo_name)

        docs_tree = c_docs_repo_path / "docs_tree.json"
        structured = c_docs_repo_path / "structured_docs.json"
        if not (docs_tree.is_file() and structured.is_file()):
            if not (c_docs_repo_path / "docs").is_dir():
                logger.info("Skipping %s (C docs not found at %s)", repo_name, c_docs_repo_path / "docs")
                continue
            _run(
                [
                    sys.executable,
                    "docs_parser/parse_official_docs.py",
                    "--repo_name",
                    repo_name,
                    "--docs-path",
                    c_docs_repo_path / "docs",
                    "--output-dir",
                    c_docs_repo_path,
                ],
                cwd=eval_src_dir,
                env=env,
            )

        combined_rubrics = c_docs_repo_path / "rubrics" / "combined_rubrics.json"
        if not combined_rubrics.is_file():
            (c_docs_repo_path / "rubrics").mkdir(parents=True, exist_ok=True)
            _generate_rubrics(eval_src_dir, env, repo_name, c_docs_repo_path, creds.model)

        _run(
            [
                sys.executable,
                "docs_parser/parse_generated_docs.py",
                "--input-dir",
                sketch_docs_repo_dir / "docs",
                "--output-dir",
                sketch_docs_repo_dir,
            ],
            cwd=eval_src_dir,
            env=env,
        )

        (sketch_docs_repo_dir / "evaluation_results").mkdir(parents=True, exist_ok=True)
        _run_evaluation(
            eval_src_dir,
            env,
            repo_name,
            c_docs_repo_path,
            sketch_docs_repo_dir,
            creds.model,
        )
