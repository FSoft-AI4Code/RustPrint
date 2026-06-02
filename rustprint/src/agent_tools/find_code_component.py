import logging
import subprocess
from pathlib import Path
from typing import Optional, Union

from pydantic_ai import RunContext, Tool

from .deps import C2RustDeps, SketchDocDeps, RefinementDeps, TestTransDeps, ExecutionRefinementDeps

logger = logging.getLogger(__name__)


def _resolve_rust_root(deps: Union[C2RustDeps, SketchDocDeps, RefinementDeps, TestTransDeps, ExecutionRefinementDeps]) -> Optional[Path]:
    if isinstance(deps, (C2RustDeps, TestTransDeps)):
        return Path(deps.absolute_rust_output_path).resolve()
    if isinstance(deps, (SketchDocDeps, RefinementDeps, ExecutionRefinementDeps)):
        return Path(deps.rust_workspace_path).resolve()
    return None


async def find_code_component(
    ctx: RunContext[Union[C2RustDeps, SketchDocDeps, RefinementDeps, TestTransDeps, ExecutionRefinementDeps]],
    pattern: str,
    path_in_repo: str = ".",
) -> str:
    deps = ctx.deps
    rust_root = _resolve_rust_root(deps)

    if rust_root is None or not rust_root.is_dir():
        return "Error: Rust workspace path is not available for this context."

    logger.info("=" * 80)
    logger.info("TOOL CALL: find_code_component")
    logger.info("  Pattern      : %s", pattern)
    logger.info("  Path in repo : %s", path_in_repo)
    logger.info("=" * 80)

    target = (rust_root / path_in_repo.lstrip("/")).resolve()
    try:
        target.relative_to(rust_root)
    except ValueError:
        return "Error: path_in_repo must stay inside the Rust workspace."

    if not target.exists():
        return f"Error: Path does not exist in Rust workspace: {path_in_repo}"

    cmd = [
        "grep",
        "-R",
        "-n",
        "-I",
        "--line-number",
        "--exclude-dir=target",
        "--exclude-dir=.cargo",
        "--",
        pattern,
        str(target),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "Error: find_code_component timed out after 60 seconds. Narrow your pattern or path_in_repo."
    except FileNotFoundError:
        return "Error: grep is not available in environment."
    except Exception as e:
        logger.exception("find_code_component failed: %s", e)
        return f"Error running find_code_component: {e}"

    raw_output = (result.stdout or "").strip()
    if not raw_output:
        logger.info("[find_code_component] No matches found for pattern: %s", pattern)
        return f"No matches found for pattern: {pattern}"

    formatted_lines = []
    for raw_line in raw_output.splitlines():
        parts = raw_line.split(":", 2)
        if len(parts) == 3:
            abs_path, line_no, content = parts
            try:
                rel_path = str(Path(abs_path).relative_to(rust_root))
            except ValueError:
                rel_path = abs_path
            formatted_lines.append(f"{rel_path} - Line {line_no} -> {content.strip()}")
        else:
            formatted_lines.append(raw_line)

    max_lines = 200
    truncated = len(formatted_lines) > max_lines
    display_lines = formatted_lines[:max_lines]
    output = "\n".join(display_lines)

    logger.info("[find_code_component] Found %d match(es):\n%s", len(formatted_lines), output)

    if truncated:
        return (
            f"Found {len(formatted_lines)} matches (showing first {max_lines}):\n"
            f"{output}\n"
            "<response clipped>"
        )
    return output


find_code_component_tool = Tool(
    function=find_code_component,
    name="find_code_component",
    description=(
        "Find Rust code locations by running grep -R in the Rust workspace. "
        "Arguments: pattern (required), path_in_repo (optional, default='.')"
    ),
    takes_ctx=True,
)
