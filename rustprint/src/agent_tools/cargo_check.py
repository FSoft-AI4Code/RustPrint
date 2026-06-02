"""Tool to run `cargo check` on the translated Rust repo and return success or compiler output for the agent to fix."""

import logging
import subprocess
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic_ai import RunContext, Tool

from .deps import C2RustDeps, RefinementDeps, TestTransDeps

logger = logging.getLogger(__name__)

DepsWithRustPath = Union[C2RustDeps, TestTransDeps, RefinementDeps]


def _get_rust_repo_path(deps: DepsWithRustPath) -> str:
    return getattr(deps, "absolute_rust_output_path", None) or getattr(
        deps, "rust_workspace_path", None
    )


def _find_crate_root(start: Path) -> Optional[Path]:
    """Walk up from start until we find a directory containing Cargo.toml (crate root)."""
    current = start.resolve()
    for _ in range(20):
        if (current / "Cargo.toml").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


async def cargo_check(
    ctx: RunContext[DepsWithRustPath],
    scope: Literal["crate", "workspace"] = "crate",
) -> str:
    """
    Run `cargo check` in the translated Rust repository.
    - scope="crate": Change directory into the current crate (current_crate_root) and run `cargo check` there. Use during module translation.
    - scope="workspace": Change directory into the workspace root and run `cargo check` for the full repo. Use only after synthesis or when refining a single repo.
    To check a specific crate, the tool uses the crate directory as cwd (same as: cd <crate_folder> && cargo check). If there are errors, fix the code and call again until it passes.
    """
    deps = ctx.deps

    if not isinstance(deps, (C2RustDeps, TestTransDeps, RefinementDeps)):
        return "Error: cargo_check is only available in C2Rust, TestTrans, or Refinement context."

    rust_path = _get_rust_repo_path(deps)
    if not rust_path:
        return "Error: No Rust repository path in deps."

    path = Path(rust_path).resolve()
    if not path.is_dir():
        return f"Error: Rust repo path is not a directory: {path}"

    is_refinement = isinstance(deps, RefinementDeps)
    single_crate_at_root = (isinstance(deps, C2RustDeps) and getattr(deps, "single_crate_at_root", False)) or is_refinement
    if scope == "crate" and single_crate_at_root:
        cwd = path
        cmd = ["cargo", "check"]
    elif scope == "crate":
        if isinstance(deps, C2RustDeps) and getattr(deps, "current_crate_root", None):
            crate_root = Path(deps.current_crate_root).resolve()
            if not crate_root.is_dir():
                return f"Error: current_crate_root is not a directory: {crate_root}"
        else:
            crate_root = _find_crate_root(path)
            if crate_root is None:
                return "Error: No Cargo.toml found above current path. Ensure you are inside a crate directory."
        cwd = crate_root
        cmd = ["cargo", "check"]
    else:
        cwd = path
        cmd = ["cargo", "check"]

    logger.info("=" * 80)
    logger.info("TOOL CALL: cargo_check")
    logger.info("  Scope   : %s", scope)
    logger.info("  CWD     : %s", cwd)
    logger.info("  Command : cd %s && cargo check", cwd)
    logger.info("=" * 80)

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[cargo_check] cargo check timeout (300s)")
        return "Error: cargo check timed out after 300 seconds. Try simplifying the code or check for infinite compilation."
    except FileNotFoundError:
        logger.warning("[cargo_check] cargo not found")
        return "Error: 'cargo' command not found. Ensure Rust toolchain is installed and on PATH."
    except Exception as e:
        logger.exception("[cargo_check] subprocess error: %s", e)
        return f"Error running cargo check: {e}"

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    attempts = getattr(deps, "cargo_check_attempts", 0)

    # Log command and terminal output once (for inspection in log file)
    logger.info("[cargo_check] exit_code=%s stdout:\n%s\nstderr:\n%s", result.returncode, stdout, stderr)

    if result.returncode != 0:
        attempts += 1
        setattr(deps, "cargo_check_attempts", attempts)
        out = stderr.strip() or stdout.strip() or "(no output)"
        return (
            f"Still has errors. Iteration {attempts}.\n\n"
            "Fix the code according to the errors below and call cargo_check again.\n\n"
            "<CARGO_CHECK_OUTPUT>\n"
            f"{out}\n"
            "</CARGO_CHECK_OUTPUT>"
        )

    # Success: reset counter. When exit 0, cargo may still print warnings to stderr.
    setattr(deps, "cargo_check_attempts", 0)
    logger.info("[cargo_check] Done. cargo check passed (exit 0).")

    msg = "Done. cargo check passed. Proceed to the next step."
    if stderr.strip():
        msg += "\n\n<CARGO_CHECK_WARNINGS>\n" + stderr.strip() + "\n</CARGO_CHECK_WARNINGS>"
    if stdout.strip() and not stderr.strip():
        msg += "\n\n<CARGO_CHECK_OUTPUT>\n" + stdout.strip() + "\n</CARGO_CHECK_OUTPUT>"
    return msg


async def cargo_fix(
    ctx: RunContext[DepsWithRustPath],
    crate_name: str,
) -> str:
    """
    Run `cargo fix --lib -p <crate_name>` in the Rust repository (workspace root).
    Use when cargo check stderr suggests it (e.g. 'run `cargo fix --lib -p crateX` to apply 1 suggestion')
    or when warnings show 'help: first cast to a pointer `as *const ()`' — these fixes are safe to apply.
    Then run cargo_check again to confirm.
    """
    deps = ctx.deps

    if not isinstance(deps, (C2RustDeps, TestTransDeps, RefinementDeps)):
        return "Error: cargo_fix is only available in C2Rust, TestTrans, or Refinement context."

    rust_path = _get_rust_repo_path(deps)
    if not rust_path:
        return "Error: No Rust repository path in deps."

    path = Path(rust_path).resolve()
    if not path.is_dir():
        return f"Error: Rust repo path is not a directory: {path}"

    crate_name = (crate_name or "").strip()
    if not crate_name:
        return "Error: crate_name is required (e.g. the package name from the cargo check suggestion)."

    cwd = path
    cmd = ["cargo", "fix", "--lib", "-p", crate_name, "--allow-dirty"]
    logger.info("=" * 80)
    logger.info("TOOL CALL: cargo_fix")
    logger.info("  Crate   : %s", crate_name)
    logger.info("  CWD     : %s", cwd)
    logger.info("  Command : cd %s && cargo fix --lib -p %r --allow-dirty", cwd, crate_name)
    logger.info("=" * 80)

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[cargo_fix] timeout (120s)")
        return "Error: cargo fix timed out after 120 seconds."
    except FileNotFoundError:
        logger.warning("[cargo_fix] cargo not found")
        return "Error: 'cargo' command not found. Ensure Rust toolchain is installed and on PATH."
    except Exception as e:
        logger.exception("[cargo_fix] subprocess error: %s", e)
        return f"Error running cargo fix: {e}"

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    logger.info("[cargo_fix] exit_code=%s stdout:\n%s\nstderr:\n%s", result.returncode, stdout, stderr)

    if result.returncode != 0:
        out = stderr.strip() or stdout.strip() or "(no output)"
        return (
            f"cargo fix failed (exit {result.returncode}). Fix the code manually if needed, then run cargo_check again.\n\n"
            "<CARGO_FIX_OUTPUT>\n"
            f"{out}\n"
            "</CARGO_FIX_OUTPUT>"
        )

    msg = f"Done. cargo fix --lib -p {crate_name!r} applied. Run cargo_check again to confirm."
    if stdout.strip() or stderr.strip():
        msg += "\n\n<CARGO_FIX_OUTPUT>\n" + (stderr.strip() or stdout.strip()) + "\n</CARGO_FIX_OUTPUT>"
    return msg


cargo_check_tool = Tool(
    function=cargo_check,
    name="cargo_check",
    description=(
        "Run `cargo check` in the translated Rust repository. "
        "scope='crate': cd into the current crate directory and run cargo check there. Use during module translation. "
        "scope='workspace': cd into the workspace root and run cargo check for the full repo. Use only after synthesis. "
        "To run for a crate we cd into that crate folder, then run cargo check. If there are errors, fix and call again until it passes."
    ),
    takes_ctx=True,
)

cargo_fix_tool = Tool(
    function=cargo_fix,
    name="cargo_fix",
    description=(
        "Run `cargo fix --lib -p <crate_name>` in the Rust repo (workspace root). "
        "Use when cargo check stderr says 'run `cargo fix --lib -p CRATE_NAME` to apply N suggestion(s)' or shows 'help: first cast to a pointer `as *const ()`' — then run cargo_fix(crate_name='CRATE_NAME') and run cargo_check again."
    ),
    takes_ctx=True,
)
