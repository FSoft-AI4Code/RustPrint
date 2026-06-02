from pydantic_ai import Agent, UsageLimits
import logging
import os
import time
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

from rustprint.src.agent_tools.deps import TestTransDeps
from rustprint.src.agent_tools.str_replace_editor import str_replace_editor_tool
from rustprint.src.agent_tools.cargo_test_no_run import (
    cargo_test_no_run_tool,
    cargo_nextest_list_tool,
    get_crate_name_tool,
    parse_rust_workspace_crates,
)
from rustprint.src.agent_tools.find_code_component import find_code_component_tool
from rustprint.src.llm_services import create_main_model
from rustprint.src.agent_retry import run_agent_with_retry
from rustprint.src.prompt_template import TEST_TRANS_PROMPT
from rustprint.src.config import Config

MAX_REQUESTS_TEST_TRANS = 500

_C_TEST_EXTENSIONS = {".c", ".h", ".cpp", ".cc", ".cxx"}
_C_TEST_DIR_NAMES = ["tests", "test", "Tests", "Test"]


def _list_c_test_entries(c_repo_root: Path) -> list:
    entries = []
    for dir_name in _C_TEST_DIR_NAMES:
        test_dir = c_repo_root / dir_name
        if test_dir.is_dir():
            for item in sorted(test_dir.iterdir()):
                if item.is_file() and item.suffix in _C_TEST_EXTENSIONS:
                    entries.append(f"{dir_name}/{item.name}")
                elif item.is_dir():
                    entries.append(f"{dir_name}/{item.name}/")
            break
    return entries


def log_info(msg: str, func_name: str = ""):
    if func_name:
        logger.info(f"[{func_name} @ test_trans_orchestrator.py] {msg}")
    else:
        logger.info(msg)


class TestTransOrchestrator:

    def __init__(self, config: Config):
        self.config = config
        self.model = create_main_model(config)

    def create_test_trans_agent(self) -> Agent:
        return Agent(
            self.model,
            name="TestTrans",
            deps_type=TestTransDeps,
            tools=[str_replace_editor_tool, cargo_test_no_run_tool, cargo_nextest_list_tool, get_crate_name_tool, find_code_component_tool],
            system_prompt=TEST_TRANS_PROMPT,
            retries=2,
            end_strategy='early',
        )

    async def translate_tests_for_repo(
        self,
        repo_name: str,
        c_repo_path: str,
        rust_repo_path: str,
    ) -> Dict[str, Any]:
        start = time.time()
        log_info(f"Test translation: {repo_name}", "translate_tests_for_repo")
        log_info(f"  C repo: {c_repo_path}", "translate_tests_for_repo")
        log_info(f"  Rust repo: {rust_repo_path}", "translate_tests_for_repo")

        workspace_info = parse_rust_workspace_crates(Path(rust_repo_path))
        crates = workspace_info["crates"]
        is_workspace = workspace_info["is_workspace"]

        if is_workspace:
            crate_lines = "\n".join(
                f"  - crate_name='{c['name']}', dir='{c['dir']}'" for c in crates
            )
            crate_section = f"""\
The Rust repository is a WORKSPACE with {len(crates)} crate(s):
{crate_lines}

For each C test file you read, inspect its #include directives and the name of the C file being tested to determine which Rust crate it targets. Then place translated tests inside THAT crate's directory:
  - Integration test → '<matching_crate_dir>/tests/<file>.rs'
  - Unit test        → #[cfg(test)] mod tests inside the relevant source file in '<matching_crate_dir>/src/...'
Never write test files at the workspace root (e.g. do NOT create 'tests/foo.rs' or 'test_foo.rs' directly at the top level of the repo)."""
        else:
            crate = crates[0] if crates else {"dir": ".", "name": repo_name}
            crate_section = f"""\
The Rust repository is a SINGLE CRATE (no workspace members):
  - crate_name='{crate['name']}', root dir='.'

Integration tests go into 'tests/<file>.rs' (at the repo root, which IS the crate root).
Unit tests go as #[cfg(test)] mod tests blocks inside the corresponding 'src/...' files."""

        c_test_entries = _list_c_test_entries(Path(c_repo_path))
        if c_test_entries:
            c_test_hint = "Known C test files/directories (relative to c_repo root):\n" + "\n".join(
                f"  {e}" for e in c_test_entries
            )
        else:
            c_test_hint = "Could not pre-scan C test files — discover them manually using working_dir='c_repo'."

        from pathlib import Path as _Path

        deps = TestTransDeps(
            absolute_c_repo_path=c_repo_path,
            absolute_rust_output_path=rust_repo_path,
            registry={},
            config=self.config,
        )

        agent = self.create_test_trans_agent()
        prompt = f"""Generate tests from the C repository to the Rust repository for '{repo_name}'. YOU MUST ENSURE there are at least 50 tests for the repo.


=== RUST CRATE STRUCTURE ===
{crate_section}

=== C TEST FILES (pre-scanned) ===
{c_test_hint}

=== WORKFLOW ===
Translate tests from the C repository to the Rust repository for '{repo_name}'.

1. Use str_replace_editor(working_dir='c_repo', command='view', path='tests') or path='test' to discover C test files. If neither exists, try path='.' and look for test-related directories. List all test files and plan to translate every one.
2. Read every C test file carefully and in full with str_replace_editor(working_dir='c_repo', command='view', path='<relative path>') to understand its test structure, assertions, inputs, and expected outputs before translating.
3. Use str_replace_editor(working_dir='rust_repo', command='view', path='.') to explore the Rust repo layout. For each C test file, inspect its #include directives and the C file under test to identify which Rust module/crate it maps to.
4. Translate the tests into the matching Rust crate/module. Do not translate too many at a single time to avoid technical debt — only 5-7 tests, verify they work, then continue.
5. When encountering an issue, avoid creating a new file (such as current_test_fix.rs); fix the issue in the current test file instead.
6. For each test in C repository, you must choose placement based on its properties:
- (1) Tests that exercise internal logic of a single module → insert directly into the corresponding source file as a #[cfg(test)] mod tests {{ #[test] fn ... }} block.
- (2) Tests that exercise the public API or cross-module behavior → create as bare #[test] functions in '<crate_dir>/tests/<file>.rs'. Do NOT add a #[cfg(test)] wrapper around integration test files. You may create multiple test files for the same crate. The name of the file should be related to functionality of the test. You can create multiple test file, should not append a lot of tests in a single file.
Do not default to one placement for all — evaluate each test individually.
- For each test, use the inputs and expected outputs from the C test as the reference. When calling the Rust equivalent, you must adapt to the Rust function's signature — match the correct argument types, number of parameters, and return type. Convert or cast them as needed to be compatible with the Rust API.
- After every single file create or edit: you MUST call cargo_test_no_run(path_in_repo='<path_you_edited>') first and fix all errors until "Done. cargo test --no-run passed." Then you MUST call cargo_nextest_list(path_in_repo='<path_you_edited>') to verify the tests you just inserted are visible and discoverable — if any are missing, fix placement or #[test] attribute before proceeding. Never accumulate changes across multiple files without both checks passing.
7. When done with all test files, call cargo_test_no_run() with no arguments. If errors, fix and call again until "Done. cargo test --no-run passed."
"""

        try:
            result = await run_agent_with_retry(
                agent,
                prompt,
                deps=deps,
                message_history=None,
                usage_limits=UsageLimits(request_limit=MAX_REQUESTS_TEST_TRANS),
            )
            elapsed = time.time() - start
            log_info(f"Completed in {elapsed:.2f}s", "translate_tests_for_repo")
            return {
                "repo_name": repo_name,
                "success": True,
                "elapsed": elapsed,
                "result": result,
            }
        except Exception as e:
            elapsed = time.time() - start
            log_info(f"Failed after {elapsed:.2f}s: {e}", "translate_tests_for_repo")
            return {
                "repo_name": repo_name,
                "success": False,
                "elapsed": elapsed,
                "error": str(e),
            }
