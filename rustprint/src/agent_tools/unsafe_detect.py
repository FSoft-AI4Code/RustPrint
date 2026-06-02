import re
import logging
from pathlib import Path
from typing import Union

from pydantic_ai import RunContext, Tool

from .deps import C2RustDeps, RefinementDeps

logger = logging.getLogger(__name__)

UNSAFE_PATTERN = re.compile(r"\bunsafe\b")


def _count_unsafe_in_file(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return len(UNSAFE_PATTERN.findall(text))
    except Exception:
        return 0


async def unsafe_detect(
    ctx: RunContext[Union[C2RustDeps, RefinementDeps]],
    crate: str,
) -> str:
    deps = ctx.deps
    if not isinstance(deps, (C2RustDeps, RefinementDeps)):
        msg = "Error: unsafe_detect is only available in C2Rust Skeleton or Refinement context."
        logger.warning("[unsafe_detect] %s", msg)
        return msg
    rust_path = getattr(deps, "absolute_rust_output_path", None) or getattr(deps, "rust_workspace_path", None)
    if not rust_path:
        msg = "Error: No Rust repository path in deps."
        logger.warning("[unsafe_detect] %s", msg)
        return msg
    crate_dir = Path(rust_path).resolve()
    if not crate_dir.is_dir():
        msg = f"Error: Crate path is not a directory: {crate_dir}"
        logger.warning("[unsafe_detect] %s", msg)
        return msg

    logger.info("=" * 80)
    logger.info("TOOL CALL: unsafe_detect")
    logger.info("  Crate : %s", crate)
    logger.info("  Path  : %s", crate_dir)
    logger.info("=" * 80)

    lines = []
    for path in sorted(crate_dir.rglob("*.rs")):
        n = _count_unsafe_in_file(path)
        if n > 0:
            rel = path.relative_to(crate_dir)
            lines.append(f"FILE {rel} has {n} unsafe block(s)")
    if not lines:
        out = f"No unsafe blocks found in crate '{crate}'."
        logger.info("[unsafe_detect] crate=%s %s", crate, out)
        return out
    out = "\n".join(lines)
    logger.info("[unsafe_detect] crate=%s results:\n%s", crate, out)
    return out


unsafe_detect_tool = Tool(
    function=unsafe_detect,
    name="unsafe_detect",
    description=(
        "Scan the current crate for Rust files containing 'unsafe' and return which files have how many. "
        "Call after every file create or edit in this crate. Parameter crate: the current crate name (from module_tree). "
        "Returns lines like 'FILE path has n unsafe block(s)'. Minimize unsafe; only keep unsafe when there is no better solution."
    ),
    takes_ctx=True,
)
