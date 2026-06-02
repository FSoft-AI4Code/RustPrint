import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, TextPart

from rustprint.src.dependency_analyzer.utils.logging_config import setup_logging
setup_logging(level=logging.INFO)

from rustprint.src.agent_tools.deps import ExecutionRefinementDeps
from rustprint.src.agent_tools.str_replace_editor import str_replace_editor_tool
from rustprint.src.agent_tools.cargo_single_test import cargo_single_test_tool
from rustprint.src.agent_tools.cargo_test_no_run import cargo_test_no_run_tool
from rustprint.src.agent_tools.find_code_component import find_code_component_tool
from rustprint.src.llm_services import create_main_model
from rustprint.src.agent_retry import run_agent_with_retry
from rustprint.src.prompt_template import EXECUTION_REFINEMENT_SYSTEM_PROMPT
from rustprint.src.config import Config

logger = logging.getLogger(__name__)


def _load_completed(completed_path: Path) -> set:
    done = set()
    if not completed_path.is_file():
        return done
    with open(completed_path) as f:
        for line in f:
            line = line.strip().strip("{}")
            if not line:
                continue
            try:
                done.add(int(line))
            except ValueError:
                continue
    return done


def _append_completed(completed_path: Path, index: int) -> None:
    with open(completed_path, "a") as f:
        f.write(f"{index}\n")


def load_all_tests(execution_path: Path) -> List[Dict[str, Any]]:
    out = []
    with open(execution_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("name"):
                out.append(rec)
    return out


def format_execution_user_prompt(test_name: str, path: str = "") -> str:
    prompt = "=" * 80 + "\n"
    prompt += "FAILING TEST\n"
    prompt += "=" * 80 + "\n"
    prompt += f"Test name: {test_name}\n"
    if path:
        prompt += f"Path: {path}\n"
    prompt += "\n"
    prompt += f"Your task is locate ONLY {test_name} (avoiding reading all the test in the files) and after that tracing the production code to fix the problem."
    prompt += "\n"
    prompt += "Should call cargo_test_no_run() and cargo_single_test() at the start of the workflow to check if we need to apply any edit."
    prompt += "\n"
    prompt += " Avoid creating new document file .md, you need to localize the code that used in the test to fix the problem."
    prompt += "\n"
    prompt += "If after too many attempts, the test still fails, you should give up and to proceed to the next test."
    prompt += "\n"
    prompt += "If the test cargo_single_test() reports the test passed, you should stop immediately and to proceed to the next test."
    prompt += "\n"
    prompt += "If you want to modify something, avoid create scripts or executable files, you must use str_replace_editor with command 'str_replace' or 'insert' to modify the existing code."
    return prompt


def _log_agent_messages(result: Any) -> None:
    try:
        messages = result.all_messages()
    except Exception:
        return
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    args_preview = ""
                    try:
                        if isinstance(part.args, str):
                            args_preview = part.args[:200]
                        else:
                            args_preview = json.dumps(part.args)[:200]
                    except Exception:
                        pass
                    logger.info("  [agent→tool] %s(%s)", part.tool_name, args_preview)
                elif isinstance(part, TextPart) and part.content.strip():
                    preview = part.content.strip()[:300].replace("\n", " ")
                    logger.info("  [agent text] %s", preview)
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    preview = str(part.content)[:200].replace("\n", " ")
                    logger.info("  [tool→agent] %s: %s", part.tool_name, preview)


class ExecutionRefinementOrchestrator:
    def __init__(self, config: Config, rust_workspace_path: str, repo_name: str, max_tool_calls: int = 50):
        self.config = config
        self.rust_workspace_path = rust_workspace_path
        self.repo_name = repo_name
        self.max_tool_calls = max_tool_calls
        self.model = create_main_model(config)

    def create_agent(self) -> Agent:
        return Agent(
            self.model,
            name="execution_refinement_agent",
            deps_type=ExecutionRefinementDeps,
            tools=[str_replace_editor_tool, cargo_single_test_tool, cargo_test_no_run_tool, find_code_component_tool],
            system_prompt=EXECUTION_REFINEMENT_SYSTEM_PROMPT,
            retries=2,
            end_strategy="early",
        )

    async def refine_for_test(
        self,
        test_name: str,
        stdout: str,
        path: str = "",
        agent: Agent = None,
    ) -> bool:
        deps = ExecutionRefinementDeps(
            rust_workspace_path=self.rust_workspace_path,
            current_module_name=self.repo_name,
            current_test_name=test_name,
            current_stdout=stdout or "",
            config=self.config,
        )
        user_prompt = format_execution_user_prompt(test_name, path)
        if agent is None:
            agent = self.create_agent()
        try:
            logger.info("USER PROMPT:\n%s", user_prompt)
            logger.info("Starting agent for test: %s", test_name)
            result = await run_agent_with_retry(agent, user_prompt, deps=deps, usage_limits=UsageLimits(request_limit=self.max_tool_calls))
            _log_agent_messages(result)
            logger.info("=" * 80)
            logger.info("✓ Agent completed for: %s", test_name)
            logger.info("=" * 80)
            return True
        except UsageLimitExceeded as e:
            logger.warning("⚠ [%s] request limit (30) exceeded: %s — marking completed and moving on", test_name, e.message)
            return True
        except Exception as e:
            logger.exception("=" * 80)
            logger.error("✗ Refinement failed for %s: %s", test_name, e)
            logger.error("=" * 80)
            return False


async def run_execution_refinement(
    execution_jsonl_path: Path,
    rust_workspace_path: str,
    repo_name: str,
    config: Config,
    max_tool_calls: int = 50,
) -> Dict[str, Any]:
    overall_start = time.time()
    execution_jsonl_path = Path(execution_jsonl_path).resolve()
    rust_workspace_path = str(Path(rust_workspace_path).resolve())

    logger.info("")
    logger.info("=" * 80)
    logger.info("EXECUTION REFINEMENT: %s", repo_name)
    logger.info("=" * 80)
    logger.info("Workspace : %s", rust_workspace_path)
    logger.info("Execution : %s", execution_jsonl_path)
    logger.info("=" * 80)


    completed_path = Path(rust_workspace_path) / "completed.jsonl"
    completed = _load_completed(completed_path)
    if completed:
        logger.info("Resuming: %d test(s) already completed", len(completed))

    all_tests = load_all_tests(execution_jsonl_path)
    if not all_tests:
        logger.info("No tests found in %s", execution_jsonl_path)
        return {"total_failures": 0, "refined_count": 0, "failed_count": 0}

    total = len(all_tests)
    failures = [r for r in all_tests if r.get("status") == "fail"]
    passes = total - len(failures)

    logger.info("Total tests : %d  |  Pass: %d  |  Fail: %d", total, passes, len(failures))
    logger.info("=" * 80)

    refiner = ExecutionRefinementOrchestrator(config, rust_workspace_path, repo_name, max_tool_calls=max_tool_calls)
    agent = refiner.create_agent()
    refined = 0
    agent_failed = 0

    for i, rec in enumerate(all_tests):
        test_name = rec.get("name", "")
        status = rec.get("status", "")
        stdout = rec.get("stdout", "")
        path = rec.get("path", "")

        logger.info("")
        logger.info("─" * 80)

        if status != "fail":
            logger.info("[%d/%d] %s  →  pass  →  skip", i + 1, total, test_name)
            logger.info("─" * 80)
            continue

        if (i + 1) in completed:
            logger.info("[%d/%d] %s  →  fail  →  already completed  →  skip", i + 1, total, test_name)
            logger.info("─" * 80)
            refined += 1
            continue

        logger.info("[%d/%d] %s  →  fail  →  refining", i + 1, total, test_name)
        if path:
            logger.info("  Path  : %s", path)
        if stdout:
            preview = stdout.strip()[:400].replace("\n", " ↵ ")
            logger.info("  Stdout: %s", preview)
        logger.info("─" * 80)

        success = await refiner.refine_for_test(test_name, stdout, path, agent=agent)
        if success:
            refined += 1
            _append_completed(completed_path, i + 1)
            logger.info("✓ [%d/%d] %s  →  done", i + 1, total, test_name)
        else:
            agent_failed += 1
            logger.warning("✗ [%d/%d] %s  →  agent failed", i + 1, total, test_name)

    overall_time = time.time() - overall_start

    logger.info("")
    logger.info("=" * 80)
    logger.info("EXECUTION REFINEMENT COMPLETED: %s", repo_name)
    logger.info("=" * 80)
    logger.info("Total tests         : %d", total)
    logger.info("Passed (skipped)    : %d", passes)
    logger.info("Failed              : %d", len(failures))
    logger.info("Refined successfully: %d", refined)
    logger.info("Agent failed        : %d", agent_failed)
    logger.info("Total time          : %.2fs", overall_time)
    logger.info("=" * 80)

    return {"total_failures": len(failures), "refined_count": refined, "failed_count": agent_failed}
