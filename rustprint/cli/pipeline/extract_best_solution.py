import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^version_(\d+)$")


def _overall_score_from_rubrics(rubrics) -> Optional[float]:
    if not isinstance(rubrics, list):
        return None
    total_score = 0.0
    total_weight = 0.0
    for item in rubrics:
        weight = item.get("weight", 1)
        score = item.get("score", 0)
        total_score += score * weight
        total_weight += weight
    return total_score / total_weight if total_weight > 0 else 0.0


def _get_overall_score(json_path: Path) -> Optional[float]:
    if not json_path.is_file():
        return None
    try:
        with open(json_path) as handle:
            data = json.load(handle)
    except Exception:
        return None
    meta = data.get("metadata") or data.get("combination_metadata") or {}
    if "overall_score" in meta:
        try:
            return float(meta["overall_score"])
        except (TypeError, ValueError):
            return None
    rubrics = data.get("rubrics", data if isinstance(data, list) else None)
    return _overall_score_from_rubrics(rubrics)


def _sorted_versions(translated_repos_dir: Path):
    versions = []
    for path in translated_repos_dir.glob("version_*"):
        if not path.is_dir():
            continue
        match = _VERSION_RE.match(path.name)
        if match:
            versions.append((int(match.group(1)), path.name))
    return [name for _, name in sorted(versions)]


def run(output_root) -> Path:
    output_root = Path(output_root)
    translated_repos_dir = output_root / "translated_repos"
    sketch_docs_base = output_root / "sketch_docs"
    best_solution_dir = translated_repos_dir / "best_solution"

    if not translated_repos_dir.is_dir():
        raise RuntimeError(f"Translated repos directory not found: {translated_repos_dir}")

    versions = _sorted_versions(translated_repos_dir)

    repo_names = set()
    for version in versions:
        version_dir = translated_repos_dir / version
        for repo in version_dir.iterdir():
            if repo.is_dir():
                repo_names.add(repo.name)

    if not repo_names:
        raise RuntimeError(f"No repos found under {translated_repos_dir}/version_*/")

    best_solution_dir.mkdir(parents=True, exist_ok=True)

    for repo in sorted(repo_names):
        best_version = None
        best_score = -1.0

        for version in versions:
            trans_path = translated_repos_dir / version / repo
            eval_path = (
                sketch_docs_base
                / version
                / repo
                / "evaluation_results"
                / "combined_evaluation_results.json"
            )
            if not trans_path.is_dir() or not eval_path.is_file():
                continue
            score = _get_overall_score(eval_path)
            if score is None:
                continue
            if best_version is None or score > best_score:
                best_score = score
                best_version = version

        if best_version is None:
            logger.info("%s - skip (no evaluated translated version)", repo)
            continue

        source = translated_repos_dir / best_version / repo
        destination = best_solution_dir / repo
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        logger.info("%s - %s (score %.4f) -> %s", repo, best_version, best_score, destination)

    return best_solution_dir
