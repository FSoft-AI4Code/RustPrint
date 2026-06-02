import logging
import os
import queue
import re
import shutil
import threading
import time
from pathlib import Path

from rustprint.cli import pipeline
from rustprint.cli.pipeline import PipelineCreds
from rustprint.cli.config_manager import ConfigManager, deep_merge_defaults, expand_path, resolve_project_dir
from rustprint.cli.git_manager import GitManager
from rustprint.cli.commands.migrate import (
    BENCHMARK_DIR,
    ROOT_DIR,
    _build_pipeline_env,
    _copy_best_solution,
    _find_final_execution_repo,
    _has_c_files,
    _has_source_tests,
    _prepare_single_repo_folder,
)

logger = logging.getLogger("rustprint")


_FEATURE_RE = re.compile(r"Feature \[(\d+)/(\d+)\]:\s*(.+?)\s*$")

_STATUS_ORDER = {"pending": 0, "active": 1, "done": 2, "skipped": 2, "error": 2}


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


class _Stopped(Exception):
    pass


class EventBus:
    def __init__(self, max_logs: int = 4000):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._logs: list[dict] = []
        self._snapshot: dict = {}
        self.max_logs = max_logs

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: dict) -> None:
        if event.get("type") == "log":
            self._logs.append(event)
            if len(self._logs) > self.max_logs:
                self._logs = self._logs[-self.max_logs:]
        elif event.get("type") == "state":
            self._snapshot = event
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(event)

    def recent_logs(self) -> list[dict]:
        return list(self._logs)

    def snapshot_event(self) -> dict:
        return dict(self._snapshot)


class _BusLogHandler(logging.Handler):
    def __init__(self, bus: EventBus):
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return
        self.bus.publish(
            {
                "type": "log",
                "level": record.levelname.lower(),
                "message": message,
                "ts": time.time(),
            }
        )


class _PhaseHandler(logging.Handler):
    def __init__(self, runner: "MigrationRunner"):
        super().__init__()
        self.runner = runner

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.runner._on_backend_log(record.getMessage())
        except Exception:
            pass


class MigrationRunner:
    def __init__(self):
        self.bus = EventBus()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._log_handler: _BusLogHandler | None = None
        self._phase_handler: _PhaseHandler | None = None
        self._force = False
        self._timers: dict[str, float] = {}
        self._current_crate: str | None = None
        self.running = False
        self.error: str | None = None
        self.final_path: str | None = None
        self.stages: list[dict] = []

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "error": self.error,
            "final_path": self.final_path,
            "stages": self.stages,
        }

    def start(self, config: dict, force: bool = False) -> tuple[bool, str]:
        with self._lock:
            if self.running:
                return False, "Migration already in progress"
            source_value = str((config.get("source") or {}).get("path") or "").strip()
            if not source_value:
                return False, "Source repository path is required"
            source_path = expand_path(source_value)
            if not source_path.is_dir():
                return False, f"Source repository does not exist: {source_path}"
            if not _has_c_files(source_path):
                return False, f"No C source or header files found in {source_path}"
            api = config.get("api") or {}
            if not str(api.get("api_key") or "").strip() or not str(api.get("base_url") or "").strip():
                return False, "API key and base URL are required"

            merged = deep_merge_defaults(config)
            merged["source"]["path"] = str(source_path)
            try:
                ConfigManager(project_path=source_path).write_project_config(merged, overwrite=True)
            except Exception as exc:
                return False, f"Failed to write config: {exc}"

            self.running = True
            self.error = None
            self.final_path = None
            self._stop = False
            self._force = bool(force or merged.get("run", {}).get("force", False))
            self.stages = self._build_stages(merged, source_path.name)
            self._emit_state()
            self._thread = threading.Thread(target=self._run, args=(merged, source_path), daemon=True)
            self._thread.start()
            return True, "Migration started"

    def stop(self) -> bool:
        if not self.running:
            return False
        self._stop = True
        self._log("Stop requested; finishing current stage then aborting.", "warning")
        return True

    def _build_stages(self, cfg: dict, project_name: str) -> list[dict]:
        requirement = cfg["requirement_refinement"]
        execution = cfg["execution_refinement"]

        cache = resolve_project_dir(expand_path(str(cfg["output"]["cache"])), project_name)
        output_base = expand_path(str(cfg["output"]["base_dir"]))
        trepos = cache / "translated_repos"
        docs_path = cache / "c_docs" / "version_0" / project_name

        stages = [
            {"id": "docs", "label": "C Documentation", "status": "pending", "detail": "", "path": str(docs_path)},
            {
                "id": "translate",
                "label": "Translation",
                "status": "pending",
                "detail": "",
                "path": str(trepos / "version_0" / project_name),
                "children": [],
            },
        ]
        if bool(requirement.get("enabled", True)):
            rounds = int(requirement.get("rounds", 5))
            stages.append(
                {
                    "id": "requirement",
                    "label": "Requirement Refinement",
                    "status": "pending",
                    "detail": "",
                    "total_rounds": rounds,
                    "path": str(trepos / "best_solution" / project_name),
                    "children": [
                        {
                            "id": f"req_round_{r}",
                            "label": f"Round {r}",
                            "status": "pending",
                            "detail": "",
                            "path": str(trepos / f"version_{r}" / project_name),
                            "children": [
                                {"id": f"req_round_{r}_docs", "label": "Rust Documentation", "status": "pending", "detail": ""},
                                {"id": f"req_round_{r}_refine", "label": "Refinement", "status": "pending", "detail": ""},
                            ],
                        }
                        for r in range(1, rounds + 1)
                    ]
                    + [
                        {
                            "id": "req_final",
                            "label": "Final Evaluation & Selection",
                            "status": "pending",
                            "detail": "",
                            "path": str(trepos / "best_solution" / project_name),
                            "children": [
                                {"id": "req_final_docs", "label": "Rust Documentation", "status": "pending", "detail": ""},
                                {"id": "req_final_eval", "label": "Final Evaluation", "status": "pending", "detail": ""},
                                {"id": "req_final_best", "label": "Best Solution Selection", "status": "pending", "detail": ""},
                            ],
                        }
                    ],
                }
            )
        if bool(execution.get("enabled", True)) and bool(execution.get("translate_tests", True)):
            rounds = int(execution.get("rounds", 5))
            exec_aware = trepos / "execution-aware"
            stages.append({"id": "test_trans", "label": "Test Translation", "status": "pending", "detail": ""})
            stages.append(
                {
                    "id": "execution",
                    "label": "Execution Refinement",
                    "status": "pending",
                    "detail": "",
                    "total_rounds": rounds,
                    "path": str(exec_aware / f"version_{rounds}" / project_name),
                    "children": [
                        {
                            "id": f"exec_round_{r}",
                            "label": f"Round {r}",
                            "status": "pending",
                            "detail": "",
                            "path": str(exec_aware / f"version_{r - 1}" / project_name),
                        }
                        for r in range(1, rounds + 1)
                    ],
                }
            )
        stages.append(
            {
                "id": "finalize",
                "label": "Finalize Output",
                "status": "pending",
                "detail": "",
                "path": str(resolve_project_dir(output_base, project_name)),
            }
        )
        return stages

    def _emit_state(self) -> None:
        self.bus.publish({"type": "state", **self.snapshot()})

    def _start_timer(self, key: str) -> None:
        if key not in self._timers:
            self._timers[key] = time.perf_counter()

    def _stop_timer(self, key: str, label: str, status: str) -> None:
        started = self._timers.pop(key, None)
        if started is None:
            return
        elapsed = time.perf_counter() - started
        verb = "completed" if status == "done" else status
        self._log(f"{label} {verb} in {_fmt_duration(elapsed)}")

    def _set_stage(self, stage_id: str, status: str, detail: str | None = None) -> None:
        for stage in self.stages:
            if stage["id"] == stage_id:
                if status == "active":
                    self._start_timer(stage_id)
                stage["status"] = status
                if detail is not None:
                    stage["detail"] = detail
                if status in ("done", "skipped", "error"):
                    self._stop_timer(stage_id, stage["label"], status)
                if status in ("done", "skipped"):
                    target = "done" if status == "done" else "skipped"
                    self._complete_subtree(stage, target)
        self._emit_state()

    def _find_node(self, node_id: str) -> dict | None:
        def walk(nodes: list[dict]) -> dict | None:
            for node in nodes:
                if node.get("id") == node_id:
                    return node
                found = walk(node.get("children", []))
                if found is not None:
                    return found
            return None

        return walk(self.stages)

    def _complete_subtree(self, node: dict, target: str) -> None:
        for child in node.get("children", []):
            if child.get("status") in ("pending", "active"):
                child["status"] = target
            self._complete_subtree(child, target)

    def _set_node(self, node_id: str, status: str, detail: str | None = None, timer_label: str | None = None) -> bool:
        node = self._find_node(node_id)
        if node is None:
            return False
        if node.get("status") == status and detail is None:
            return False
        if status == "active":
            self._start_timer(node_id)
        elif status in ("done", "skipped", "error"):
            self._stop_timer(node_id, timer_label or node.get("label", node_id), status)
        node["status"] = status
        if detail is not None:
            node["detail"] = detail
        return True

    def _begin_crate(self, name: str, idx: int) -> bool:
        self._finish_current_crate()
        translate = self._find_node("translate")
        if translate is None:
            return False
        crate_id = f"translate_crate_{idx}"
        if self._find_node(crate_id) is None:
            translate.setdefault("children", []).append(
                {
                    "id": crate_id,
                    "label": name,
                    "status": "active",
                    "detail": "",
                    "children": [
                        {"id": f"{crate_id}_planner", "label": "Planner", "status": "pending", "detail": ""},
                        {"id": f"{crate_id}_skeleton", "label": "Skeleton", "status": "pending", "detail": ""},
                    ],
                }
            )
            self._start_timer(crate_id)
        self._current_crate = crate_id
        return True

    def _set_crate_phase(self, phase: str, status: str) -> bool:
        if not self._current_crate:
            return False
        node = self._find_node(f"{self._current_crate}_{phase}")
        if node is None:
            return False
        if _STATUS_ORDER.get(status, 0) <= _STATUS_ORDER.get(node.get("status", "pending"), 0):
            return False
        crate = self._find_node(self._current_crate)
        crate_label = crate.get("label", "") if crate else ""
        return self._set_node(node["id"], status, timer_label=f"Translation · {crate_label} · {node['label']}")

    def _finish_current_crate(self) -> bool:
        if not self._current_crate:
            return False
        crate = self._find_node(self._current_crate)
        if crate is None:
            return False
        changed = False
        for child in crate.get("children", []):
            if child.get("status") in ("pending", "active"):
                changed |= self._set_node(
                    child["id"], "done", timer_label=f"Translation · {crate.get('label', '')} · {child['label']}"
                )
        if crate.get("status") != "done":
            changed |= self._set_node(self._current_crate, "done", timer_label=f"Translation · {crate.get('label', '')}")
        return changed

    def _begin_synthesis(self) -> bool:
        translate = self._find_node("translate")
        if translate is None:
            return False
        if self._find_node("translate_synthesis") is None:
            translate.setdefault("children", []).append(
                {"id": "translate_synthesis", "label": "Synthesis", "status": "active", "detail": ""}
            )
            self._start_timer("translate_synthesis")
        else:
            self._set_node("translate_synthesis", "active")
        self._current_crate = None
        return True

    def _set_child(self, stage_id: str, child_id: str, status: str, detail: str | None = None) -> bool:
        changed = False
        for stage in self.stages:
            if stage["id"] != stage_id:
                continue
            for child in stage.get("children", []):
                if child["id"] == child_id and child["status"] != status:
                    key = f"{stage_id}:{child_id}"
                    if status == "active":
                        self._start_timer(key)
                    elif status in ("done", "skipped", "error"):
                        self._stop_timer(key, f"{stage['label']} · {child['label']}", status)
                    child["status"] = status
                    if detail is not None:
                        child["detail"] = detail
                    changed = True
        return changed

    def _on_backend_log(self, message: str) -> None:
        stage = next((s for s in self.stages if s["id"] == "translate"), None)
        if not stage or stage["status"] != "active":
            return
        changed = False
        feature = _FEATURE_RE.search(message)
        if feature:
            changed |= self._begin_crate(feature.group(3), int(feature.group(1)))
        elif "[Phase 1: Planner]" in message:
            changed |= self._set_crate_phase("planner", "active")
        elif "Planner completed in" in message:
            changed |= self._set_crate_phase("planner", "done")
        elif "[Phase 2: Implementation]" in message:
            changed |= self._set_crate_phase("planner", "done")
            changed |= self._set_crate_phase("skeleton", "active")
        elif "Starting ROOT workspace synthesis" in message:
            changed |= self._finish_current_crate()
            changed |= self._begin_synthesis()
        elif "Workspace synthesis completed" in message:
            changed |= self._set_node("translate_synthesis", "done", timer_label="Translation · Synthesis")
        if changed:
            self._emit_state()

    def _log(self, message: str, level: str = "info") -> None:
        self.bus.publish({"type": "log", "level": level, "message": message, "ts": time.time()})

    def _guard(self) -> None:
        if self._stop:
            raise _Stopped()

    def _run(self, cfg: dict, source_path: Path) -> None:
        self._log_handler = _BusLogHandler(self.bus)
        self._log_handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
        logger.addHandler(self._log_handler)
        self._phase_handler = _PhaseHandler(self)
        logger.addHandler(self._phase_handler)
        previous_level = logger.level
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
        self._timers.clear()
        self._current_crate = None
        run_started = time.perf_counter()
        try:
            self._execute(cfg, source_path)
            self._log(f"Total migration time: {_fmt_duration(time.perf_counter() - run_started)}")
        except _Stopped:
            self.error = "stopped"
            self._log(f"Migration stopped by user after {_fmt_duration(time.perf_counter() - run_started)}.", "warning")
        except Exception as exc:
            logger.exception("Migration failed")
            self.error = str(exc)
            self.bus.publish({"type": "error", "message": str(exc)})
        finally:
            logger.removeHandler(self._log_handler)
            logger.removeHandler(self._phase_handler)
            logger.setLevel(previous_level)
            self.running = False
            self._emit_state()
            self.bus.publish({"type": "done", "error": self.error, "final_path": self.final_path})

    def _execute(self, cfg: dict, source_path: Path) -> None:
        project_name = source_path.name
        model = str(cfg["model"]["name"])
        api_key = str(cfg["api"].get("api_key") or "")
        base_url = str(cfg["api"].get("base_url") or "")

        cache_project_dir = resolve_project_dir(expand_path(str(cfg["output"]["cache"])), project_name)
        output_base_dir = expand_path(str(cfg["output"]["base_dir"]))
        if self._force and cache_project_dir.exists():
            self._log(f"Force enabled: clearing cache and starting from scratch ({cache_project_dir})", "warning")
            shutil.rmtree(cache_project_dir, ignore_errors=True)
        cache_project_dir.mkdir(parents=True, exist_ok=True)
        output_base_dir.mkdir(parents=True, exist_ok=True)

        self._maybe_branch(source_path, cfg.get("git", {}))

        source_repos_dir = _prepare_single_repo_folder(cache_project_dir, source_path, project_name)
        env = _build_pipeline_env(cfg, cache_project_dir, source_repos_dir)
        os.environ.update(env)

        creds = PipelineCreds(model=model, api_key=api_key, base_url=base_url)
        pipeline._common.setup_backend_logging(verbose=False)

        c_docs_dir = cache_project_dir / "c_docs" / "version_0"
        translated_v0_dir = cache_project_dir / "translated_repos" / "version_0"

        requirement = cfg["requirement_refinement"]
        execution = cfg["execution_refinement"]
        requirement_enabled = bool(requirement.get("enabled", True))
        requirement_rounds = int(requirement.get("rounds", 5))
        execution_enabled = bool(execution.get("enabled", True))
        execution_rounds = int(execution.get("rounds", 5))
        translate_tests = bool(execution.get("translate_tests", True))

        self._log(f"Project: {project_name}")
        self._log(f"Source: {source_path}")
        self._log(f"Cache: {cache_project_dir}")
        self._log(f"Final: {resolve_project_dir(output_base_dir, project_name)}")

        self._guard()
        self._set_stage("docs", "active")
        pipeline.gen_sketch.run_document(source_repos_dir, c_docs_dir, creds)
        self._set_stage("docs", "done")

        self._guard()
        self._set_stage("translate", "active")
        pipeline.gen_sketch.run_translate(source_repos_dir, c_docs_dir, translated_v0_dir, creds)
        self._set_stage("translate", "done")

        if requirement_enabled:
            self._set_stage("requirement", "active")
            for version in range(requirement_rounds):
                round_id = f"req_round_{version + 1}"
                self._guard()
                self._set_child("requirement", round_id, "active")
                self._set_node(f"{round_id}_docs", "active", timer_label=f"Round {version + 1} · Rust Documentation")
                self._set_stage("requirement", "active", f"Round {version + 1}/{requirement_rounds}: generate Rust docs")
                pipeline.gen_sketch_docs.run(version, cache_project_dir, creds)
                self._set_stage("requirement", "active", f"Round {version + 1}/{requirement_rounds}: score rubrics")
                pipeline.eval_sketch.run(version, cache_project_dir, ROOT_DIR, BENCHMARK_DIR, creds)
                self._set_node(f"{round_id}_docs", "done", timer_label=f"Round {version + 1} · Rust Documentation")
                self._set_node(f"{round_id}_refine", "active", timer_label=f"Round {version + 1} · Refinement")
                self._set_stage("requirement", "active", f"Round {version + 1}/{requirement_rounds}: refine code")
                pipeline.refine_sketch.run(version, cache_project_dir, creds)
                self._set_node(f"{round_id}_refine", "done", timer_label=f"Round {version + 1} · Refinement")
                self._set_child("requirement", round_id, "done")
                self._emit_state()
            self._guard()
            self._set_node("req_final", "active", timer_label="Final Evaluation & Selection")
            self._set_node("req_final_docs", "active", timer_label="Final · Rust Documentation")
            self._set_stage("requirement", "active", f"Final evaluation (version {requirement_rounds}): generate Rust docs")
            pipeline.gen_sketch_docs.run(requirement_rounds, cache_project_dir, creds)
            self._set_node("req_final_docs", "done", timer_label="Final · Rust Documentation")
            self._set_node("req_final_eval", "active", timer_label="Final · Final Evaluation")
            self._set_stage("requirement", "active", f"Final evaluation (version {requirement_rounds}): score rubrics")
            pipeline.eval_sketch.run(requirement_rounds, cache_project_dir, ROOT_DIR, BENCHMARK_DIR, creds)
            self._set_node("req_final_eval", "done", timer_label="Final · Final Evaluation")
            self._set_node("req_final_best", "active", timer_label="Final · Choose best repo")
            self._set_stage("requirement", "active", "Choosing best intermediate repo")
            pipeline.extract_best_solution.run(cache_project_dir)
            self._set_node("req_final_best", "done", timer_label="Final · Choose best repo")
            self._set_node("req_final", "done")
            self._set_stage("requirement", "done", "Best solution selected")
        else:
            _copy_best_solution(cache_project_dir, project_name)

        used_execution = False
        if execution_enabled and translate_tests and _has_source_tests(source_path):
            used_execution = True
            self._guard()
            self._set_stage("test_trans", "active")
            pipeline.test_trans.run(cache_project_dir, source_repos_dir, creds)
            self._set_stage("test_trans", "done")

            self._set_stage("execution", "active")
            for version in range(execution_rounds):
                self._guard()
                self._set_child("execution", f"exec_round_{version + 1}", "active")
                self._set_stage("execution", "active", f"Round {version + 1}/{execution_rounds}: run cargo nextest")
                pipeline.execute.run(version, cache_project_dir)
                self._set_stage("execution", "active", f"Round {version + 1}/{execution_rounds}: fix failing tests")
                pipeline.refine_execution.run(version, cache_project_dir, creds)
                self._set_child("execution", f"exec_round_{version + 1}", "done")
                self._emit_state()
            self._guard()
            self._set_stage("execution", "active", f"Final test run (version {execution_rounds})")
            pipeline.execute.run(execution_rounds, cache_project_dir)
            self._set_stage("execution", "done")
        elif execution_enabled and translate_tests:
            self._set_stage("test_trans", "skipped", "No source tests found")
            self._set_stage("execution", "skipped", "No source tests found")
            self._log("No source tests found; skipping execution-aware refinement.", "warning")

        self._guard()
        self._set_stage("finalize", "active")
        final_path = self._finalize(cache_project_dir, output_base_dir, project_name, execution_rounds, used_execution)
        self.final_path = str(final_path)
        self._set_stage("finalize", "done", str(final_path))
        self._log(f"Migration completed. Final Rust repo: {final_path}")

    def _maybe_branch(self, source_path: Path, git_cfg: dict) -> None:
        if not bool(git_cfg.get("branch_enabled", False)):
            return
        branch_name = str(git_cfg.get("branch_name") or "rustprint-migration")
        git_manager = GitManager(source_path)
        is_clean, status = git_manager.check_clean_working_directory()
        if not is_clean:
            raise RuntimeError(
                "Working directory has uncommitted changes. Commit or stash before branch-enabled migration.\n" + status
            )
        git_manager.create_or_checkout_branch(branch_name)
        self._log(f"Using Git branch: {branch_name}")

    def _finalize(
        self,
        cache_project_dir: Path,
        output_base_dir: Path,
        project_name: str,
        execution_rounds: int,
        used_execution: bool,
    ) -> Path:
        if used_execution:
            final_src = _find_final_execution_repo(cache_project_dir, project_name, execution_rounds)
            if final_src is None:
                final_src = cache_project_dir / "translated_repos" / "best_solution" / project_name
        else:
            final_src = cache_project_dir / "translated_repos" / "best_solution" / project_name

        if not final_src.is_dir():
            raise RuntimeError(f"Final translated repository not found: {final_src}")

        final_dst = resolve_project_dir(output_base_dir, project_name)
        if final_dst.exists():
            shutil.rmtree(final_dst)
        final_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(final_src, final_dst)
        return final_dst
