"""Tool to run `cargo test --no-run` on the Rust repo to validate tests compile without running them."""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Union

from pydantic_ai import RunContext, Tool

from .deps import TestTransDeps, ExecutionRefinementDeps

logger = logging.getLogger(__name__)


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


def _get_package_name_from_crate_root(crate_root: Path) -> Optional[str]:
    """Read Cargo.toml in crate_root and return [package] name, or fall back to directory name."""
    cargo_path = crate_root / "Cargo.toml"
    if not cargo_path.is_file():
        return crate_root.name
    try:
        text = cargo_path.read_text(encoding="utf-8")
        in_package = False
        for line in text.splitlines():
            line = line.strip()
            if line == "[package]":
                in_package = True
                continue
            if in_package and line.startswith("["):
                break
            if in_package:
                m = re.match(r'^name\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return crate_root.name


def parse_rust_workspace_crates(workspace_root: Path) -> dict:
    """
    Parse the workspace-level Cargo.toml to enumerate available crates.

    Returns {"is_workspace": bool, "crates": [{"dir": str, "name": str}, ...]}.
      - is_workspace=True  : [workspace] members found; each resolved member listed.
      - is_workspace=False : single crate at root; crates=[{"dir": ".", "name": pkg}].
    """
    cargo_path = workspace_root / "Cargo.toml"
    if not cargo_path.is_file():
        return {"is_workspace": False, "crates": []}

    try:
        text = cargo_path.read_text(encoding="utf-8")
    except Exception:
        return {"is_workspace": False, "crates": []}

    in_workspace = False
    in_members = False
    raw_members: list = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[workspace]":
            in_workspace = True
            continue
        if in_workspace:
            if stripped.startswith("[") and not stripped.startswith("[workspace"):
                in_workspace = False
                in_members = False
                continue
            if re.match(r"^members\s*=", stripped):
                in_members = True
            if in_members:
                for m in re.findall(r'"([^"]+)"', stripped):
                    raw_members.append(m)
                if "]" in stripped:
                    in_members = False

    if not raw_members:
        pkg_name = _get_package_name_from_crate_root(workspace_root) or workspace_root.name
        return {"is_workspace": False, "crates": [{"dir": ".", "name": pkg_name}]}

    crates: list = []
    for member in raw_members:
        if "*" in member:
            for crate_path in sorted(workspace_root.glob(member)):
                if (crate_path / "Cargo.toml").is_file():
                    rel = str(crate_path.relative_to(workspace_root))
                    name = _get_package_name_from_crate_root(crate_path) or crate_path.name
                    crates.append({"dir": rel, "name": name})
        else:
            crate_path = workspace_root / member
            if (crate_path / "Cargo.toml").is_file():
                name = _get_package_name_from_crate_root(crate_path) or crate_path.name
                crates.append({"dir": member, "name": name})

    return {"is_workspace": True, "crates": crates}


def get_crate_root_from_path(workspace_root: Path, path_in_repo: str) -> Optional[Path]:
    """
    Resolve path_in_repo under workspace_root, find the nearest Cargo.toml (crate root),
    and return the crate root Path if it is under workspace_root.
    """
    full = (workspace_root / path_in_repo.lstrip("/")).resolve()
    if not full.exists():
        full = full.parent
    crate_root = _find_crate_root(full)
    if crate_root is None:
        return None
    try:
        crate_root.resolve().relative_to(workspace_root.resolve())
    except ValueError:
        return None
    return crate_root.resolve()


def get_crate_name_from_path(workspace_root: Path, path_in_repo: str) -> Optional[str]:
    """
    Resolve path_in_repo under workspace_root, find the nearest Cargo.toml (crate root),
    and return the package name from that Cargo.toml (or the crate root directory name).
    path_in_repo is relative to workspace root, e.g. 'allocators/tests/foo.rs' or 'cbor/src/lib.rs'.
    """
    crate_root = get_crate_root_from_path(workspace_root, path_in_repo)
    if crate_root is None:
        return None
    return _get_package_name_from_crate_root(crate_root)


async def cargo_test_no_run(
    ctx: RunContext[Union[TestTransDeps, ExecutionRefinementDeps]],
    path_in_repo: Optional[str] = None,
) -> str:
    """
    Run `cargo test --no-run` in the Rust repository. To run for a specific crate we cd into that crate directory, then run cargo test --no-run.
    - path_in_repo: If set (e.g. path to file you edited: 'crate_1/tests/integration.rs'), find that crate's directory and cd there, then run `cargo test --no-run`. Use the path to the file you just edited.
    - path_in_repo omitted: cd into the workspace root and run `cargo test --no-run` for the entire workspace.
    If there are errors, fix the code and call again until it passes.
    """
    deps = ctx.deps

    if not isinstance(deps, (TestTransDeps, ExecutionRefinementDeps)):
        return "Error: cargo_test_no_run is only available in TestTrans or ExecutionRefinement context."

    rust_path = (
        getattr(deps, "absolute_rust_output_path", None)
        or getattr(deps, "rust_workspace_path", None)
        or getattr(deps, "absolute_rust_repo_path", None)
    )
    if not rust_path:
        return "Error: No Rust repository path in deps."

    workspace_root = Path(rust_path).resolve()
    if not workspace_root.is_dir():
        return f"Error: Rust repo path is not a directory: {workspace_root}"

    cwd: Path = workspace_root
    if path_in_repo:
        crate_root = get_crate_root_from_path(workspace_root, path_in_repo)
        if crate_root is not None:
            cwd = crate_root
        else:
            logger.info("[cargo_test_no_run] No valid crate for path_in_repo %s; running for full workspace", path_in_repo)

    logger.info("=" * 80)
    logger.info("TOOL CALL: cargo_test_no_run")
    logger.info("  Path in repo : %s", path_in_repo or "(workspace)")
    logger.info("  CWD          : %s", cwd)
    logger.info("  Command      : cd %s && cargo test --no-run", cwd)
    logger.info("=" * 80)

    cmd = ["cargo", "test", "--no-run"]
    env = {**os.environ, "RUSTFLAGS": "-Awarnings"}
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[cargo_test_no_run] cargo test --no-run timeout (300s)")
        print("[cargo_test_no_run] Error: timeout after 300s.", flush=True)
        return "Error: cargo test --no-run timed out after 300 seconds. Try simplifying the test code or check for infinite compilation."
    except FileNotFoundError:
        logger.warning("[cargo_test_no_run] cargo not found")
        return "Error: 'cargo' command not found. Ensure Rust toolchain is installed and on PATH."
    except Exception as e:
        logger.exception("[cargo_test_no_run] subprocess error: %s", e)
        return f"Error running cargo test --no-run: {e}"

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    attempts = getattr(deps, "cargo_test_attempts", 0)

    if result.returncode != 0:
        attempts += 1
        setattr(deps, "cargo_test_attempts", attempts)

        logger.info("[cargo_test_no_run] Errors (iter %d). stderr:\n%s", attempts, stderr)
        if stdout.strip():
            logger.info("[cargo_test_no_run] stdout:\n%s", stdout)

        out = stderr.strip() or stdout.strip() or "(no output)"
        return (
            f"Still has errors. Iteration {attempts}.\n\n"
            "Fix the test code according to the errors below and call cargo_test_no_run again.\n\n"
            "<CARGO_TEST_OUTPUT>\n"
            f"{out}\n"
            "</CARGO_TEST_OUTPUT>"
        )

    setattr(deps, "cargo_test_attempts", 0)
    logger.info("[cargo_test_no_run] Done. cargo test --no-run passed.")

    return "Done. cargo test --no-run passed. Proceed to the next step."


async def get_crate_name(ctx: RunContext[TestTransDeps], path_in_repo: str) -> str:
    """
    Get the crate (package) name for a path under the Rust workspace.
    path_in_repo: path relative to workspace root, e.g. 'crate_1/tests/foo.rs' or 'cbor/src/lib.rs'.
    Finds the nearest Cargo.toml above that path and returns the [package] name (or directory name).
    Use this when you need to know which crate a file belongs to, e.g. before calling cargo_test_no_run.
    """
    deps = ctx.deps
    if not isinstance(deps, TestTransDeps):
        return "Error: get_crate_name is only available in TestTrans context."
    rust_path = getattr(deps, "absolute_rust_output_path", None)
    if not rust_path:
        return "Error: No Rust repository path in deps."
    workspace_root = Path(rust_path).resolve()
    logger.info("=" * 80)
    logger.info("TOOL CALL: get_crate_name")
    logger.info("  Path in repo : %s", path_in_repo)
    logger.info("=" * 80)
    name = get_crate_name_from_path(workspace_root, path_in_repo)
    if name is None:
        return f"Error: Could not find a crate (Cargo.toml) for path: {path_in_repo}"
    return f"Crate name for path '{path_in_repo}': {name}"


cargo_test_no_run_tool = Tool(
    function=cargo_test_no_run,
    name="cargo_test_no_run",
    description=(
        "Run `cargo test --no-run` in the Rust repository. To run for a specific crate we cd into that crate directory, then run cargo test --no-run. "
        "path_in_repo: If set (e.g. path to file you edited: 'crate_1/tests/integration.rs'), we cd into that crate's folder and run cargo test --no-run there. "
        "If omitted: we cd into the workspace root and run cargo test --no-run for the entire workspace. "
        "If there are errors, fix the code and call again until it passes."
    ),
    takes_ctx=True,
)

get_crate_name_tool = Tool(
    function=get_crate_name,
    name="get_crate_name",
    description=(
        "Get the crate (package) name for a path under the Rust workspace. "
        "path_in_repo: path relative to workspace root (e.g. 'crate_1/tests/integration.rs'). "
        "Returns the [package] name from the nearest Cargo.toml above that path. Use when you need to know which crate a file belongs to."
    ),
    takes_ctx=True,
)


async def cargo_nextest_list(
    ctx: RunContext[TestTransDeps],
    test_name: str,
    path_in_repo: Optional[str] = None,
) -> str:
    """
    Run `cargo nextest list` and check whether test_name appears in the output.
    Returns "Found test <name>" or "Not found test <name>" instead of the full list.
    - test_name: the exact function name of the test to look for (e.g. "test_chacha20_encrypt").
    - path_in_repo: If set, cd into that path's crate and list tests for that crate only.
    """
    deps = ctx.deps
    if not isinstance(deps, TestTransDeps):
        return "Error: cargo_nextest_list is only available in TestTrans context."
    rust_path = getattr(deps, "absolute_rust_output_path", None)
    if not rust_path:
        return "Error: No Rust repository path in deps."

    workspace_root = Path(rust_path).resolve()
    if not workspace_root.is_dir():
        return f"Error: Rust repo path is not a directory: {workspace_root}"

    cwd: Path = workspace_root
    if path_in_repo:
        crate_root = get_crate_root_from_path(workspace_root, path_in_repo)
        if crate_root is not None:
            cwd = crate_root
        else:
            logger.info("[cargo_nextest_list] No valid crate for path_in_repo %s; running for full workspace", path_in_repo)

    logger.info("=" * 80)
    logger.info("TOOL CALL: cargo_nextest_list")
    logger.info("  test_name    : %s", test_name)
    logger.info("  Path in repo : %s", path_in_repo or "(workspace)")
    logger.info("  CWD          : %s", cwd)
    logger.info("  Command      : cd %s && cargo nextest list", cwd)
    logger.info("=" * 80)

    env = {**os.environ, "RUSTFLAGS": "-Awarnings"}
    try:
        result = subprocess.run(
            ["cargo", "nextest", "list"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[cargo_nextest_list] cargo nextest list timeout (120s)")
        return "Error: cargo nextest list timed out after 120 seconds."
    except FileNotFoundError:
        logger.warning("[cargo_nextest_list] cargo not found")
        return "Error: 'cargo' command not found. Ensure Rust toolchain is installed."
    except Exception as e:
        logger.exception("[cargo_nextest_list] subprocess error: %s", e)
        return f"Error running cargo nextest list: {e}"

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        logger.info("[cargo_nextest_list] Failed. output:\n%s", output)
        return f"cargo nextest list failed:\n{output.strip()}"

    matched = [line for line in output.splitlines() if test_name in line]
    logger.info("[cargo_nextest_list] grep '%s' -> %d match(es)", test_name, len(matched))

    if matched:
        return f"Found test '{test_name}'."
    return f"Not found test '{test_name}'. Check that #[test] is present, the file is in the correct location, and cargo_test_no_run passes."


cargo_nextest_list_tool = Tool(
    function=cargo_nextest_list,
    name="cargo_nextest_list",
    description=(
        "Run `cargo nextest list` and check whether a specific test is discoverable. "
        "test_name: the exact function name to search for (e.g. 'test_encrypt'). "
        "Returns 'Found test <name>' or 'Not found test <name>' — never the full list. "
        "path_in_repo: optional path to limit the search to one crate."
    ),
    takes_ctx=True,
)
