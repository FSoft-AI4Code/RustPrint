"""
Preprocess Rust repos before test translation: remove LLM-generated artifacts.
- Remove all .md files except README.md
- Remove every folder and file whose name contains 'test' or 'example'
- Remove #[cfg(test)] mod ... { ... } blocks from all .rs files

Run over every repo in execution-aware/version_0 (or a given directory).
"""

import argparse
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _remove_md_except_readme(repo_root: Path) -> int:
    removed = 0
    for p in repo_root.rglob("*.md"):
        if not p.is_file():
            continue
        if p.name != "README.md":
            p.unlink()
            removed += 1
            logger.debug("Removed md: %s", p.relative_to(repo_root))
    return removed


def _name_contains_test(name: str) -> bool:
    return "test" in name.lower()


def _name_contains_example(name: str) -> bool:
    return "example" in name.lower()


def _name_contains_test_or_example(name: str) -> bool:
    return _name_contains_test(name) or _name_contains_example(name)


def _under_target(path: Path, repo_root: Path) -> bool:
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False
    return "target" in rel.parts


def _remove_paths_containing_test(repo_root: Path) -> int:
    test_dirs: list[Path] = []
    for d in repo_root.rglob("*"):
        if d.is_dir() and _name_contains_test_or_example(d.name) and not _under_target(d, repo_root):
            test_dirs.append(d)
    test_dirs.sort(key=lambda p: len(p.parts), reverse=True)
    removed = 0
    for d in test_dirs:
        if d.exists():
            shutil.rmtree(d)
            removed += 1
            logger.debug("Removed dir: %s", d.relative_to(repo_root))
    for f in repo_root.rglob("*"):
        if f.is_file() and _name_contains_test_or_example(f.name) and not _under_target(f, repo_root):
            try:
                f.unlink()
                removed += 1
                logger.debug("Removed file: %s", f.relative_to(repo_root))
            except OSError:
                pass
    return removed


def _find_matching_brace(content: str, start: int) -> int | None:
    depth = 0
    i = start
    in_string = None
    escape = False
    while i < len(content):
        c = content[i]
        if escape:
            escape = False
            i += 1
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == in_string:
                in_string = None
            i += 1
            continue
        if c in ('"', "'") and (i == 0 or content[i - 1] != "\\"):
            in_string = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _remove_cfg_test_blocks(content: str) -> str:
    out: list[str] = []
    rest = content
    cfg_test_re = re.compile(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]", re.IGNORECASE)
    mod_re = re.compile(r"\bmod\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\{")
    while True:
        m = cfg_test_re.search(rest)
        if not m:
            out.append(rest)
            break
        out.append(rest[: m.start()])
        block_start = m.start()
        after_attr = rest[m.end() :]
        pos = 0
        while pos < len(after_attr):
            line_end = after_attr.find("\n", pos)
            if line_end == -1:
                line_end = len(after_attr)
            line = after_attr[pos:line_end].strip()
            if line.startswith("#[") or (line.startswith("#") and "[" in line):
                pos = line_end + 1 if line_end < len(after_attr) else line_end
                continue
            break
        after_attrs = after_attr[pos:].lstrip()
        mod_m = mod_re.match(after_attrs)
        if not mod_m:
            out.append(rest[block_start : m.end()])
            rest = rest[m.end() :]
            continue
        brace_start = after_attrs.find("{")
        if brace_start == -1:
            out.append(rest[block_start : m.end()])
            rest = rest[m.end() :]
            continue
        in_sub = (m.end() - block_start) + pos + brace_start
        end_brace = _find_matching_brace(rest[block_start:], in_sub)
        if end_brace is None:
            out.append(rest[block_start : m.end()])
            rest = rest[m.end() :]
            continue
        skip = end_brace + 1
        while skip < len(rest[block_start:]) and rest[block_start + skip] in " \t\n\r":
            skip += 1
        rest = rest[block_start + skip :]
    return "".join(out)


def _strip_cfg_test_in_file(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    new_text = _remove_cfg_test_blocks(text)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def _remove_cfg_test_blocks_in_repo(repo_root: Path) -> int:
    changed = 0
    for p in repo_root.rglob("*.rs"):
        if p.is_file() and _strip_cfg_test_in_file(p):
            changed += 1
            logger.debug("Stripped cfg(test) in %s", p.relative_to(repo_root))
    return changed


def preprocess_repo(repo_root: Path) -> dict[str, int]:
    counts = {"md_removed": 0, "test_paths_removed": 0, "rs_files_stripped": 0}
    counts["md_removed"] = _remove_md_except_readme(repo_root)
    counts["test_paths_removed"] = _remove_paths_containing_test(repo_root)
    counts["rs_files_stripped"] = _remove_cfg_test_blocks_in_repo(repo_root)
    return counts


def preprocess_version_dir(version_dir: Path) -> dict[str, dict[str, int]]:
    results: dict[str, dict[str, int]] = {}
    if not version_dir.is_dir():
        logger.warning("Not a directory: %s", version_dir)
        return results
    for repo_path in sorted(version_dir.iterdir()):
        if not repo_path.is_dir():
            continue
        name = repo_path.name
        results[name] = preprocess_repo(repo_path)
        logger.info(
            "Preprocess %s: md_removed=%d, test_paths=%d, rs_stripped=%d",
            name,
            results[name]["md_removed"],
            results[name]["test_paths_removed"],
            results[name]["rs_files_stripped"],
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preprocess Rust repos: remove .md (keep README), test paths, #[cfg(test)] blocks."
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--version-dir",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
    )
    args = parser.parse_args()
    path = args.path or args.version_dir
    if not path:
        raise SystemExit("Error: --path or --version-dir is required.")
    path = path.resolve()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    preprocess_version_dir(path)


if __name__ == "__main__":
    main()
