import datetime
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

from rustprint.src.translation.test.test_trans_orchestrator import TestTransOrchestrator
from rustprint.src.translation.test.preprocess import preprocess_repo
from rustprint.src.config import Config


def _find_test_folder(c_repo_path: str) -> Optional[str]:
    for name in ("tests", "test"):
        path = os.path.join(c_repo_path, name)
        if os.path.isdir(path):
            return name
    return None


def _normalize_version_name(version_filter: Optional[str]) -> str:
    if not version_filter:
        return "version_0"
    s = version_filter.strip()
    if s.startswith("version_"):
        return s
    return f"version_{s}"


class TestTransGenerator:

    def __init__(self, config: Config):
        self.config = config
        self.orchestrator = TestTransOrchestrator(config)

    def _load_succeeded(self, log_path: Path) -> set:
        done = set()
        if not log_path.is_file():
            return done
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    for repo_name, status in entry.items():
                        if status == "success":
                            done.add(repo_name)
                except json.JSONDecodeError:
                    continue
        return done

    def _append_log(self, log_path: Path, repo_name: str, status: str) -> None:
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps({repo_name: status}) + "\n")
        except OSError as e:
            logger.warning("Could not append to %s: %s", log_path, e)

    def _write_cost_json(self, repo_dir: Path, repo_name: str, version: str) -> None:
        cost_jsonl = repo_dir / "cost.jsonl"
        cost_json = repo_dir / "cost.json"
        if not cost_jsonl.is_file():
            return
        total_cost = 0.0
        total_tokens = 0
        prompt_tokens = 0
        completion_tokens = 0
        requests_count = 0
        with open(cost_jsonl) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total_cost += float(entry.get("cost_usd", 0) or 0)
                total_tokens += int(entry.get("total_tokens", 0) or 0)
                prompt_tokens += int(entry.get("prompt_tokens", 0) or 0)
                completion_tokens += int(entry.get("completion_tokens", 0) or 0)
                requests_count += 1
        cost_data = {
            "repo": repo_name,
            "version": version,
            "model": self.config.main_model,
            "phase": "test_trans",
            "total_cost_usd": round(total_cost, 8),
            "total_tokens": total_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "api_requests": requests_count,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        try:
            with open(cost_json, "w") as f:
                json.dump(cost_data, f, indent=2)
        except OSError as e:
            logger.warning("Could not write %s: %s", cost_json, e)

    def _discover_repos(
        self,
        repo_data_dir: str,
        source_dir: Path,
    ) -> List[Tuple[str, str, str]]:
        out = []
        if not os.path.isdir(repo_data_dir):
            logger.warning(f"Repo data dir not found: {repo_data_dir}")
            return out
        if not source_dir.is_dir():
            logger.warning(f"Source repos dir not found: {source_dir}")
            return out
        for repo_name in sorted(os.listdir(repo_data_dir)):
            c_repo_path = os.path.join(repo_data_dir, repo_name)
            if not os.path.isdir(c_repo_path):
                continue
            if _find_test_folder(c_repo_path) is None:
                logger.debug(f"Skip {repo_name}: no test/tests folder in C repo")
                continue
            src_repo_path = source_dir / repo_name
            if not src_repo_path.is_dir():
                logger.debug(f"Skip {repo_name}: not found in source repos dir")
                continue
            out.append((repo_name, c_repo_path, str(src_repo_path)))
        return out

    async def run(
        self,
        repo_data_dir: str,
        translated_repos_dir: str,
        version_filter: Optional[str] = None,
        source_repos_dir: Optional[str] = None,
    ) -> None:
        overall_start = time.time()
        logger.info("=" * 80)
        logger.info("[run @ test_trans_generator.py] TEST TRANSLATION WORKFLOW STARTED")
        logger.info("=" * 80)
        logger.info(f"  C repos base: {repo_data_dir}")
        logger.info(f"  Translated repos base: {translated_repos_dir}")
        if source_repos_dir:
            logger.info(f"  Source repos dir: {source_repos_dir}")
        if version_filter:
            logger.info(f"  Version filter: {version_filter}")

        translated_repos_dir = Path(translated_repos_dir).resolve()
        if source_repos_dir is not None:
            effective_source_dir = Path(source_repos_dir).resolve()
        else:
            effective_source_dir = translated_repos_dir.parent / "best_solution"
        version = _normalize_version_name(version_filter)
        version_dir = translated_repos_dir / version
        version_dir.mkdir(parents=True, exist_ok=True)
        log_path = version_dir / "log.jsonl"

        repos = self._discover_repos(repo_data_dir, effective_source_dir)
        if not repos:
            logger.warning("No repos found in source dir with C test folder.")
            return

        succeeded = self._load_succeeded(log_path)
        if succeeded:
            logger.info(f"Resuming: {len(succeeded)} repo(s) already succeeded, skipping them.")

        logger.info(f"Discovered {len(repos)} repo(s) to process")
        results = []
        for repo_name, c_repo_path, src_repo_path in repos:
            if repo_name in succeeded:
                logger.info(f"Skip {repo_name}: already succeeded.")
                continue

            dst_repo_path = version_dir / repo_name
            logger.info(f"\n--- {version} / {repo_name} ---")
            logger.info(f"Copying {repo_name} from {src_repo_path} -> {dst_repo_path}")
            if dst_repo_path.exists():
                shutil.rmtree(dst_repo_path)
            shutil.copytree(src_repo_path, dst_repo_path)

            logger.info("Preprocessing Rust repo...")
            preprocess_repo(dst_repo_path)

            result = await self.orchestrator.translate_tests_for_repo(
                repo_name=repo_name,
                c_repo_path=c_repo_path,
                rust_repo_path=str(dst_repo_path),
            )
            results.append(result)
            status = "success" if result.get("success") else "failed"

            if status == "success":
                succeeded.add(repo_name)
                self._write_cost_json(dst_repo_path, repo_name, version)
                self._append_log(log_path, repo_name, status)
            else:
                logger.warning(f"{repo_name} failed — deleting from {dst_repo_path}")
                shutil.rmtree(dst_repo_path, ignore_errors=True)

        overall_time = time.time() - overall_start
        success_count = sum(1 for r in results if r.get("success"))
        logger.info("\n" + "=" * 80)
        logger.info("[run @ test_trans_generator.py] TEST TRANSLATION COMPLETED")
        logger.info("=" * 80)
        logger.info(f"  Processed: {len(results)}, Success: {success_count}, Time: {overall_time:.2f}s")
        logger.info("=" * 80 + "\n")
