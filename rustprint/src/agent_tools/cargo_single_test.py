"""
Run a single test via cargo nextest for Execution Refinement Agent.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Union

from pydantic_ai import RunContext, Tool

from .deps import ExecutionRefinementDeps

logger = logging.getLogger(__name__)


async def cargo_single_test(ctx: RunContext[ExecutionRefinementDeps]) -> str:
    """
    Run the current test (name comes from context). No arguments.
    Runs: RUSTFLAGS="-Awarnings" RUST_BACKTRACE=full cargo nextest run <current_test_name>.
    """
    deps = ctx.deps
    if not isinstance(deps, ExecutionRefinementDeps):
        return "Error: cargo_single_test is only available in Execution Refinement context."
    rust_path = (
        getattr(deps, "rust_workspace_path", None)
        or getattr(deps, "absolute_rust_repo_path", None)
    )
    if not rust_path:
        return "Error: No Rust workspace path in deps."
    test_name = getattr(deps, "current_test_name", None) or ""
    if not test_name:
        return "Error: No current_test_name in deps."
    workspace_root = Path(rust_path).resolve()
    if not workspace_root.is_dir():
        return f"Error: Rust repo path is not a directory: {workspace_root}"
    env = dict(os.environ)
    env["RUSTFLAGS"] = "-Awarnings"
    env["RUST_BACKTRACE"] = "full"
    cmd = ["cargo", "nextest", "run", test_name]
    logger.info("=" * 80)
    logger.info("TOOL CALL: cargo_single_test")
    logger.info("  Test name : %s", test_name)
    logger.info("  Workspace : %s", workspace_root)
    logger.info("  Command   : RUSTFLAGS=-Awarnings cargo nextest run %s", test_name)
    logger.info("=" * 80)
    try:
        result = subprocess.run(
            cmd,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[cargo_single_test] timeout (120s)")
        return "Error: cargo nextest run timed out after 120 seconds."
    except FileNotFoundError:
        return "Error: 'cargo' or 'nextest' not found. Install cargo-nextest."
    except Exception as e:
        logger.exception("[cargo_single_test] %s", e)
        return f"Error: {e}"
    if result.returncode != 0:
        out = (result.stderr or result.stdout or "").strip()
        logger.info("[cargo_single_test] test failed.\n%s", out)
        return f"Test failed.\n<STDOUT>\n{out}\n</STDOUT>" if out else "Test failed."
    logger.info("[cargo_single_test] test passed.")
    return "Test passed."


cargo_single_test_tool = Tool(
    function=cargo_single_test,
    name="cargo_single_test",
    description=(
        "Run the current failing test (no arguments). Uses the test name from execution context. "
        "Runs RUSTFLAGS=\"-Awarnings\" RUST_BACKTRACE=full cargo nextest run <current_test>. "
        "Call after editing code to verify the test passes or to get updated stdout on failure."
    ),
    takes_ctx=True,
)
