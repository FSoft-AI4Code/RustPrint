#!/usr/bin/env python3

"""Source: https://github.com/SWE-agent/SWE-agent/blob/main/tools/edit_anthropic/bin/str_replace_editor
This tool is used to view the given source code and view/edit the documentation files in the separate docs directory.
"""

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, List, Optional, Tuple, Literal, Union
import io

import logging

logger = logging.getLogger(__name__)

from pydantic_ai import RunContext, Tool

from .deps import RustPrintDeps, SketchDocDeps, RefinementDeps, InputGenerationDeps, TestTransDeps, ExecutionRefinementDeps
from ..utils import validate_mermaid_diagrams

_view_history = {}
_view_count = 0
_modified_files = []
MAX_VIEWS_BEFORE_FORCE_STOP = 25

# Path segments that indicate LLM mistakenly passed an absolute or machine path
_ABSOLUTE_LIKE_PREFIXES = ("Users/", "home/", "usr/", "User/", "Home/")

def _sanitize_rust_doc_path(path: str) -> str:
    """Use only a safe relative path for rust_doc to avoid creating nested dirs like docs/Users/brian/...
    If path looks like an absolute path fragment, return only the basename (filename)."""
    clean = path.lstrip("/")
    if not clean:
        return path
    if clean.startswith(_ABSOLUTE_LIKE_PREFIXES):
        return Path(clean).name
    parts = Path(clean).parts
    if len(parts) >= 3 and parts[0] in ("Users", "home", "usr") and parts[1] not in (".", ".."):
        return Path(clean).name
    if "Documents" in parts or "Code_Migration" in parts or "RustPrint" in parts:
        return Path(clean).name
    return clean

def reset_view_history():
    """Reset the view history tracker. Call this between different module processing."""
    global _view_history, _view_count, _modified_files
    logger.info(f"[reset_view_history] Resetting tracking. Previous _modified_files count: {len(_modified_files)}")
    _view_history.clear()
    _view_count = 0
    _modified_files.clear()

def get_modified_files():
    """Get the list of files that were modified in the current session."""
    global _modified_files
    logger.info(f"[get_modified_files] Returning {len(_modified_files)} modified files")
    for f in _modified_files:
        logger.info(f"[get_modified_files]   - {f}")
    return list(_modified_files)


# There are some super strange "ascii can't decode x" errors,
# that can be solved with setting the default encoding for stdout
# (note that python3.6 doesn't have the reconfigure method)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TRUNCATED_MESSAGE: str = "<response clipped><NOTE>To save on context only part of this file has been shown to you. You should retry this tool after you have searched inside the file with `grep -n` in order to find the line numbers of what you are looking for.</NOTE>"
MAX_RESPONSE_LEN: int = 16000

MAX_WINDOW_EXPANSION_VIEW = 0
MAX_WINDOW_EXPANSION_EDIT_CONFIRM = 0
USE_FILEMAP = False
USE_LINTER = False
Command = str
SNIPPET_LINES: int = 4
LINT_WARNING_TEMPLATE = """

<NOTE>Your edits have been applied, but the linter has found syntax errors.</NOTE>

<ERRORS>
{errors}
</ERRORS>

Please review the changes and make sure they are correct.
In addition to the above errors, please also check the following:

1. The edited file is correctly indented
2. The edited file does not contain duplicate lines
3. The edit does not break existing functionality

<IMPORTANT>In rare cases, the linter errors might not actually be errors or caused by your edit. Please use your own judgement.</IMPORTANT>

Edit the file again if necessary.
"""


def maybe_truncate(content: str, truncate_after: Optional[int] = MAX_RESPONSE_LEN):
    """Truncate content and append a notice if content exceeds the specified length."""
    return (
        content
        if not truncate_after or len(content) <= truncate_after
        else content[:truncate_after] + TRUNCATED_MESSAGE
    )


class Flake8Error:
    """A class to represent a single flake8 error"""

    def __init__(self, filename: str, line_number: int, col_number: int, problem: str):
        self.filename = filename
        self.line_number = line_number
        self.col_number = col_number
        self.problem = problem

    @classmethod
    def from_line(cls, line: str):
        try:
            prefix, _sep, problem = line.partition(": ")
            filename, line_number, col_number = prefix.split(":")
        except (ValueError, IndexError) as e:
            msg = f"Invalid flake8 error line: {line}"
            raise ValueError(msg) from e
        return cls(filename, int(line_number), int(col_number), problem)

    def __eq__(self, other):
        if not isinstance(other, Flake8Error):
            return NotImplemented
        return (
            self.filename == other.filename
            and self.line_number == other.line_number
            and self.col_number == other.col_number
            and self.problem == other.problem
        )

    def __repr__(self):
        return f"Flake8Error(filename={self.filename}, line_number={self.line_number}, col_number={self.col_number}, problem={self.problem})"


def _update_previous_errors(
    previous_errors: List[Flake8Error], replacement_window: Tuple[int, int], replacement_n_lines: int
) -> List[Flake8Error]:
    """Update the line numbers of the previous errors to what they would be after the edit window.
    This is a helper function for `_filter_previous_errors`.

    All previous errors that are inside of the edit window should not be ignored,
    so they are removed from the previous errors list.

    Args:
        previous_errors: list of errors with old line numbers
        replacement_window: the window of the edit/lines that will be replaced
        replacement_n_lines: the number of lines that will be used to replace the text

    Returns:
        list of errors with updated line numbers
    """
    updated = []
    lines_added = replacement_n_lines - (replacement_window[1] - replacement_window[0] + 1)
    for error in previous_errors:
        if error.line_number < replacement_window[0]:
            # no need to adjust the line number
            updated.append(error)
            continue
        if replacement_window[0] <= error.line_number <= replacement_window[1]:
            # The error is within the edit window, so let's not ignore it
            # either way (we wouldn't know how to adjust the line number anyway)
            continue
        # We're out of the edit window, so we need to adjust the line number
        updated.append(Flake8Error(error.filename, error.line_number + lines_added, error.col_number, error.problem))
    return updated


def format_flake8_output(
    input_string: str,
    show_line_numbers: bool = False,
    *,
    previous_errors_string: str = "",
    replacement_window: Optional[Tuple[int, int]] = None,
    replacement_n_lines: Optional[int] = None,
) -> str:
    """Filter flake8 output for previous errors and print it for a given file.

    Args:
        input_string: The flake8 output as a string
        show_line_numbers: Whether to show line numbers in the output
        previous_errors_string: The previous errors as a string
        replacement_window: The window of the edit (lines that will be replaced)
        replacement_n_lines: The number of lines used to replace the text

    Returns:
        The filtered flake8 output as a string
    """
    # print(f"Replacement window: {replacement_window}")
    # print("Replacement n lines:", replacement_n_lines)
    # print("Previous errors string:", previous_errors_string)
    # print("Input string:", input_string)
    errors = [Flake8Error.from_line(line.strip()) for line in input_string.split("\n") if line.strip()]
    # print(f"New errors before filtering: {errors=}")
    lines = []
    if previous_errors_string:
        assert replacement_window is not None
        assert replacement_n_lines is not None
        previous_errors = [
            Flake8Error.from_line(line.strip()) for line in previous_errors_string.split("\n") if line.strip()
        ]
        # print(f"Previous errors before updating: {previous_errors=}")
        previous_errors = _update_previous_errors(previous_errors, replacement_window, replacement_n_lines)
        # print(f"Previous errors after updating: {previous_errors=}")
        errors = [error for error in errors if error not in previous_errors]
        # Sometimes new errors appear above the replacement window that were 'shadowed' by the previous errors
        # they still clearly aren't caused by the edit.
        errors = [error for error in errors if error.line_number >= replacement_window[0]]
        # print(f"New errors after filtering: {errors=}")
    for error in errors:
        if not show_line_numbers:
            lines.append(f"- {error.problem}")
        else:
            lines.append(f"- line {error.line_number} col {error.col_number}: {error.problem}")
    return "\n".join(lines)


def flake8(file_path: str) -> str:
    """Run flake8 on a given file and return the output as a string"""
    if Path(file_path).suffix != ".py":
        return ""
    cmd = "flake8 --isolated --select=F821,F822,F831,E111,E112,E113,E999,E902 {file_path}"
    # don't use capture_output because it's not compatible with python3.6
    out = subprocess.run(cmd.format(file_path=file_path), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return out.stdout.decode()


class Filemap:
    def show_filemap(self, file_contents: str, encoding: str = "utf8"):
        import warnings
        from tree_sitter_languages import get_language, get_parser

        warnings.simplefilter("ignore", category=FutureWarning)

        parser = get_parser("python")
        language = get_language("python")

        tree = parser.parse(bytes(file_contents.encode(encoding, errors="replace")))

        # See https://tree-sitter.github.io/tree-sitter/using-parsers#pattern-matching-with-queries.
        query = language.query("""
        (function_definition
        body: (_) @body)
        """)

        # TODO: consider special casing docstrings such that they are not elided. This
        # could be accomplished by checking whether `body.text.decode('utf8')` starts
        # with `"""` or `'''`.
        elide_line_ranges = [
            (node.start_point[0], node.end_point[0])
            for node, _ in query.captures(tree.root_node)
            # Only elide if it's sufficiently long
            if node.end_point[0] - node.start_point[0] >= 5
        ]
        # Note that tree-sitter line numbers are 0-indexed, but we display 1-indexed.
        elide_lines = {line for start, end in elide_line_ranges for line in range(start, end + 1)}
        elide_messages = [(start, f"... eliding lines {start+1}-{end+1} ...") for start, end in elide_line_ranges]
        out = []
        for i, line in sorted(
            elide_messages + [(i, line) for i, line in enumerate(file_contents.splitlines()) if i not in elide_lines]
        ):
            out.append(f"{i+1:6d} {line}")
        return "\n".join(out)


class WindowExpander:
    def __init__(self, suffix: str = ""):
        """Try to expand viewports to include whole functions, classes, etc. rather than
        using fixed line windows.

        Args:
            suffix: Filename suffix
        """
        self.suffix = suffix
        if self.suffix:
            assert self.suffix.startswith(".")

    def _find_breakpoints(self, lines: List[str], current_line: int, direction=1, max_added_lines: int = 30) -> int:
        """Returns 1-based line number of breakpoint. This line is meant to still be included in the viewport.

        Args:
            lines: List of lines of the file
            current_line: 1-based line number of the current viewport
            direction: 1 for down, -1 for up
            max_added_lines: Maximum number of lines to extend

        Returns:
            1-based line number of breakpoint. This line is meant to still be included in the viewport.
        """
        assert 1 <= current_line <= len(lines)
        assert 0 <= max_added_lines

        # 1. Find line range that we want to search for breakpoints in

        if direction == 1:
            # down
            if current_line == len(lines):
                # already last line, can't extend down
                return current_line
            iter_lines = range(current_line, 1 + min(current_line + max_added_lines, len(lines)))
        elif direction == -1:
            # up
            if current_line == 1:
                # already first line, can't extend up
                return current_line
            iter_lines = range(current_line, -1 + max(current_line - max_added_lines, 1), -1)
        else:
            msg = f"Invalid direction {direction}"
            raise ValueError(msg)

        # 2. Find the best breakpoint in the line range

        # Every condition gives a score, the best score is the best breakpoint
        best_score = 0
        best_breakpoint = current_line
        for i_line in iter_lines:
            next_line = None
            line = lines[i_line - 1]
            if i_line + direction in iter_lines:
                next_line = lines[i_line + direction - 1]
            score = 0
            if line == "":
                score = 1
                if next_line == "":
                    # Double new blank line:
                    score = 2
            if self.suffix == ".py" and any(
                re.match(regex, line) for regex in [r"^\s*def\s+", r"^\s*class\s+", r"^\s*@"]
            ):
                # We include decorators here, because they are always on top of the function/class definition
                score = 3
            if score > best_score:
                best_score = score
                best_breakpoint = i_line
                if direction == 1 and i_line != current_line:
                    best_breakpoint -= 1
            if i_line == 1 or i_line == len(lines):
                score = 3
                if score > best_score:
                    best_score = score
                    best_breakpoint = i_line
            # print(f"Score {score} for line {i_line} ({line})")

        # print(f"Best score {best_score} for line {best_breakpoint} ({lines[best_breakpoint-1]})")
        if direction == 1 and best_breakpoint < current_line or direction == -1 and best_breakpoint > current_line:
            # We don't want to shrink the view port, so we return the current line
            return current_line

        return best_breakpoint

    def expand_window(self, lines: List[str], start: int, stop: int, max_added_lines: int) -> Tuple[int, int]:
        """

        Args:
            lines: All lines of the file
            start: 1-based line number of the start of the viewport
            stop: 1-based line number of the end of the viewport
            max_added_lines: Maximum number of lines to extend (separately for each side)

        Returns:
            Tuple of 1-based line numbers of the start and end of the viewport.
            Both inclusive.
        """
        # print("Input:", start, stop)
        assert 1 <= start <= stop <= len(lines), (start, stop, len(lines))
        if max_added_lines <= 0:
            # Already at max range, no expansion
            return start, stop
        new_start = self._find_breakpoints(lines, start, direction=-1, max_added_lines=max_added_lines)
        new_stop = self._find_breakpoints(lines, stop, direction=1, max_added_lines=max_added_lines)
        # print(f"Expanded window is {new_start} to {new_stop}")
        assert new_start <= new_stop, (new_start, new_stop)
        assert new_start <= start, (new_start, start)
        assert start - new_start <= max_added_lines, (start, new_start)
        assert new_stop >= stop, (new_stop, stop)
        assert new_stop - stop <= max_added_lines, (new_stop, stop)
        return new_start, new_stop


class EditTool:
    """
    An filesystem editor tool that allows the agent to view, create, and edit files.
    The tool parameters are defined by Anthropic and are not editable.
    """

    name = "str_replace_editor"

    def __init__(self, REGISTRY, absolute_docs_path=None):
        super().__init__()
        self._encoding = None
        self.REGISTRY = REGISTRY
        self.logs = []
        self.absolute_docs_path = Path(absolute_docs_path) if absolute_docs_path else None

    def _get_display_path(self, path: Path) -> str:
        """Get path for display purposes - relative to absolute_docs_path if available"""
        if self.absolute_docs_path and path.is_absolute():
            try:
                return str(path.relative_to(self.absolute_docs_path))
            except ValueError:
                # Path is not under absolute_docs_path, return as-is
                return str(path)
        return str(path)

    @property
    def _file_history(self):
        return defaultdict(list, json.loads(self.REGISTRY.get("file_history", "{}")))

    @_file_history.setter
    def _file_history(self, value: dict):
        self.REGISTRY["file_history"] = json.dumps(value)

    def __call__(
        self,
        *,
        command: Command,
        path: str,
        file_text: Optional[str] = None,
        view_range: Optional[List[int]] = None,
        old_str: Optional[str] = None,
        new_str: Optional[str] = None,
        insert_line: Optional[int] = None,
        **kwargs,
    ):
        _path = Path(path)
        logger.info(f"[EditTool.__call__] Command: {command}, Path: {path}")
        if not self.validate_path(command, _path):
            logger.warning(f"[EditTool.__call__] Path validation failed for: {path}")
            return
        if command == "view":
            return self.view(_path, view_range)
        elif command == "create":
            if file_text is None:
                self.logs.append("Parameter `file_text` is required for command: create")
                return
            logger.info(f"[EditTool.__call__] Calling create_file for: {_path}")
            self.create_file(_path, file_text)
            return None
        elif command == "str_replace":
            if old_str is None:
                self.logs.append("Parameter `old_str` is required for command: str_replace")
                return
            return self.str_replace(_path, old_str, new_str)
        elif command == "insert":
            if insert_line is None:
                self.logs.append("Parameter `insert_line` is required for command: insert")
                return
            if new_str is None:
                self.logs.append("Parameter `new_str` is required for command: insert")
                return
            return self.insert(_path, insert_line, new_str)
        elif command == "undo_edit":
            return self.undo_edit(_path)
        self.logs.append(
            f'Unrecognized command {command}. The allowed commands for the {self.name} tool are: "view", "create", "str_replace", "insert", "undo_edit"'
        )
        return

    def validate_path(self, command: str, path: Path):
        """
        Check that the path/command combination is valid.
        """
        logger.info(f"[validate_path] Validating command '{command}' for path: {path}")
        logger.info(f"[validate_path] Path is_absolute: {path.is_absolute()}, exists: {path.exists()}, is_dir: {path.is_dir() if path.exists() else 'N/A'}")
        
        # Check if its an absolute path
        if not path.is_absolute():
            suggested_path = Path.cwd() / path
            self.logs.append(
                f"The path {self._get_display_path(path)} is not an absolute path, it should start with `/`. Maybe you meant {self._get_display_path(suggested_path)}?"
            )
            logger.warning(f"[validate_path] Path is not absolute: {path}")
            return False
        # Check if path exists
        if not path.exists() and command != "create":
            self.logs.append(f"""ERROR: Path does not exist: {self._get_display_path(path)}

This path does not exist in the filesystem. You may be making up or guessing paths.

REQUIRED ACTION:
1. Go back and VIEW the parent directory to see what actually exists
2. ONLY use paths that appear in directory listings
3. Do NOT guess or invent file names

If you cannot find the files you need, work with what you have on.
""")
            logger.warning(f"[validate_path] Path does not exist and command is not 'create': {path}")
            return False
        if path.exists() and command == "create":
            self.logs.append(f"File already exists at: {self._get_display_path(path)}. Cannot overwrite files using command `create`.")
            logger.warning(f"[validate_path] File already exists, cannot create: {path}")
            return False
        # Check if the path points to a directory
        if path.is_dir():
            if command != "view":
                self.logs.append(f"The path {self._get_display_path(path)} is a directory and only the `view` command can be used on directories")
                logger.warning(f"[validate_path] Path is a directory but command is not 'view': {path}")
                return False
        logger.info(f"[validate_path] Validation passed for: {path}")
        return True

    def create_file(self, path: Path, file_text: str):
        logger.info(f"[create_file] Attempting to create file at: {path}")
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.logs.append(f"Created directory: {self._get_display_path(path.parent)}")
        self.write_file(path, file_text)
        self._file_history[path].append(file_text)
        
        # Track that this file was modified (created) - use absolute path
        global _modified_files
        absolute_file_path = str(path.absolute())
        _modified_files.append(absolute_file_path)
        logger.info(f"[create_file] File created and tracked: {absolute_file_path}")
        logger.info(f"[create_file] Current _modified_files count: {len(_modified_files)}")
        
        self.logs.append(f"File created successfully at: {self._get_display_path(path)}")

    def view(self, path: Path, view_range: Optional[List[int]] = None):
        if path.is_dir():
            if view_range:
                self.logs.append("The `view_range` parameter is not allowed when `path` points to a directory.")
                return

            out = subprocess.run(
                rf"find {path} -maxdepth 5 -not -path '*/\.*'",
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout = out.stdout.decode()
            stderr = out.stderr.decode()

            if not stderr:
                stdout = stdout.replace(str(path), self._get_display_path(path))
                stdout = f"Here's the files and directories up to 5 levels deep in {self._get_display_path(path)}, excluding hidden items:\n{stdout}\n"
                self.logs.append(stdout)
            return

        file_content = self.read_file(path)
        if view_range:
            if len(view_range) != 2 or not all(isinstance(i, int) for i in view_range):
                self.logs.append("Invalid `view_range`. It should be a list of two integers.")
                return
            file_lines = file_content.split("\n")
            n_lines_file = len(file_lines)
            init_line, final_line = view_range
            if init_line < 1 or init_line > n_lines_file:
                self.logs.append(
                    f"Invalid `view_range`: {view_range}. Its first element `{init_line}` should be within the range of lines of the file: {[1, n_lines_file]}"
                )
                return
            if final_line > n_lines_file:
                self.logs.append(
                    f"Invalid `view_range`: {view_range}. Its second element `{final_line}` should be smaller than the number of lines in the file: `{n_lines_file}`"
                )
                return
            if final_line != -1 and final_line < init_line:
                self.logs.append(
                    f"Invalid `view_range`: {view_range}. Its second element `{final_line}` should be larger or equal than its first `{init_line}`"
                )
                return

            if final_line == -1:
                final_line = n_lines_file

            # Expand the viewport to include the whole function or class
            init_line, final_line = WindowExpander(suffix=path.suffix).expand_window(
                file_lines, init_line, final_line, max_added_lines=MAX_WINDOW_EXPANSION_VIEW
            )

            file_content = "\n".join(file_lines[init_line - 1 : final_line])
        else:
            if path.suffix == ".py" and len(file_content) > MAX_RESPONSE_LEN and USE_FILEMAP:
                try:
                    filemap = Filemap().show_filemap(file_content, encoding=self._encoding or "utf-8")
                except Exception:
                    # If we fail to show the filemap, just show the truncated file content
                    pass
                else:
                    self.logs.append(
                        "<NOTE>This file is too large to display entirely. Showing abbreviated version. "
                        "Please use `str_replace_editor view` with the `view_range` parameter to show selected lines next.</NOTE>"
                    )
                    filemap = maybe_truncate(filemap.expandtabs())
                    self.logs.append(filemap)
                    self.logs.append(
                        "<IMPORTANT><NOTE>The above file has been abbreviated. Please use `str_replace editor view` with `view_range` to look at relevant files in detail.</NOTE></IMPORTANT>"
                    )
                    return
            # Else just show
            init_line = 1

        # init_line is 1-based
        self.logs.append(self._make_output(file_content, self._get_display_path(path), init_line=init_line))

    def str_replace(self, path: Path, old_str: str, new_str: Optional[str]):
        """Implement the str_replace command, which replaces old_str with new_str in the file content"""
        # Read the file content
        file_content = self.read_file(path).expandtabs()
        old_str = old_str.expandtabs()
        new_str = new_str.expandtabs() if new_str is not None else ""

        # Check if old_str is unique in the file
        occurrences = file_content.count(old_str)
        if occurrences == 0:
            self.logs.append(f"No replacement was performed, old_str `{old_str}` did not appear verbatim in {self._get_display_path(path)}.")
            return
        elif occurrences > 1:
            file_content_lines = file_content.split("\n")
            lines = [idx + 1 for idx, line in enumerate(file_content_lines) if old_str in line]
            self.logs.append(
                f"No replacement was performed. Multiple occurrences of old_str `{old_str}` in lines {lines}. Please ensure it is unique"
            )
            return

        if new_str == old_str:
            self.logs.append(f"No replacement was performed, old_str `{old_str}` is the same as new_str `{new_str}`.")
            return

        pre_edit_lint = ""
        if USE_LINTER:
            try:
                pre_edit_lint = flake8(str(path))
            except Exception as e:
                self.logs.append(f"Warning: Failed to run pre-edit linter on {path}: {e}")

        # Replace old_str with new_str
        new_file_content = file_content.replace(old_str, new_str)

        # Write the new content to the file
        self.write_file(path, new_file_content)

        # Track that this file was modified - use absolute path
        global _modified_files
        _modified_files.append(str(path.absolute()))

        post_edit_lint = ""
        if USE_LINTER:
            try:
                post_edit_lint = flake8(str(path))
            except Exception as e:
                self.logs.append(f"Warning: Failed to run post-edit linter on {path}: {e}")

        epilogue = ""
        if post_edit_lint:
            ...
            replacement_window_start_line = file_content.split(old_str)[0].count("\n") + 1
            replacement_lines = len(new_str.split("\n"))
            replacement_window_end_line = replacement_window_start_line + replacement_lines - 1
            replacement_window = (replacement_window_start_line, replacement_window_end_line)
            errors = format_flake8_output(
                post_edit_lint,
                previous_errors_string=pre_edit_lint,
                replacement_window=replacement_window,
                replacement_n_lines=replacement_lines,
            )
            if errors.strip():
                epilogue = LINT_WARNING_TEMPLATE.format(errors=errors)

        # Save the content to history
        self._file_history[path].append(file_content)

        # Create a snippet of the edited section
        replacement_line = file_content.split(old_str)[0].count("\n")
        start_line = max(1, replacement_line - SNIPPET_LINES)
        end_line = min(replacement_line + SNIPPET_LINES + new_str.count("\n"), len(new_file_content.splitlines()))
        start_line, end_line = WindowExpander(suffix=path.suffix).expand_window(
            new_file_content.split("\n"), start_line, end_line, max_added_lines=MAX_WINDOW_EXPANSION_EDIT_CONFIRM
        )
        snippet = "\n".join(new_file_content.split("\n")[start_line - 1 : end_line])

        # Prepare the success message
        success_msg = f"The file {self._get_display_path(path)} has been edited. "
        success_msg += self._make_output(snippet, f"a snippet of {self._get_display_path(path)}", start_line)
        success_msg += "Review the changes and make sure they are as expected. Edit the file again if necessary."
        success_msg += epilogue

        self.logs.append(success_msg)

    def insert(self, path: Path, insert_line: int, new_str: str):
        """Implement the insert command, which inserts new_str at the specified line in the file content."""
        file_text = self.read_file(path).expandtabs()
        new_str = new_str.expandtabs()
        file_text_lines = file_text.split("\n")
        n_lines_file = len(file_text_lines)

        if insert_line < 0 or insert_line > n_lines_file:
            self.logs.append(
                f"Invalid `insert_line` parameter: {insert_line}. It should be within the range of lines of the file: {[0, n_lines_file]}"
            )
            return

        new_str_lines = new_str.split("\n")
        new_file_text_lines = file_text_lines[:insert_line] + new_str_lines + file_text_lines[insert_line:]
        snippet_lines = (
            file_text_lines[max(0, insert_line - SNIPPET_LINES) : insert_line]
            + new_str_lines
            + file_text_lines[insert_line : insert_line + SNIPPET_LINES]
        )

        new_file_text = "\n".join(new_file_text_lines)
        snippet = "\n".join(snippet_lines)

        self.write_file(path, new_file_text)
        self._file_history[path].append(file_text)

        # todo: Also expand these windows

        success_msg = f"The file {self._get_display_path(path)} has been edited. "
        success_msg += self._make_output(
            snippet,
            "a snippet of the edited file",
            max(1, insert_line - SNIPPET_LINES + 1),
        )
        success_msg += "Review the changes and make sure they are as expected (correct indentation, no duplicate lines, etc). Edit the file again if necessary."
        self.logs.append(success_msg)

    def undo_edit(self, path: Path):
        """Implement the undo_edit command."""
        if not self._file_history[path]:
            self.logs.append(f"No edit history found for {self._get_display_path(path)}.")
            return

        old_text = self._file_history[path].pop()
        self.write_file(path, old_text)

        self.logs.append(f"Last edit to {self._get_display_path(path)} undone successfully. {self._make_output(old_text, self._get_display_path(path))}")

    def read_file(self, path: Path):
        """Read the content of a file from a given path; raise a ToolError if an error occurs."""
        encodings = [
            (None, None),
            ("utf-8", None),
            ("latin-1", None),
            ("utf-8", "replace"),
        ]
        exception = None
        for self._encoding, errors in encodings:
            try:
                text = path.read_text(encoding=self._encoding, errors=errors)
            except UnicodeDecodeError as e:
                exception = e
            else:
                break
        else:
            self.logs.append(f"Ran into UnicodeDecodeError {exception} while trying to read {self._get_display_path(path)}")
            return
        return text

    def write_file(self, path: Path, file: str):
        """Write the content of a file to a given path; raise a ToolError if an error occurs."""
        try:
            path.write_text(file, encoding=self._encoding or "utf-8")
        except Exception as e:
            self.logs.append(f"Ran into {e} while trying to write to {self._get_display_path(path)}")
            return

    def _make_output(
        self,
        file_content: str,
        file_descriptor: str,
        init_line: int = 1,
        expand_tabs: bool = True,
    ):
        """Generate output for the CLI based on the content of a file."""
        file_content = maybe_truncate(file_content)
        if expand_tabs:
            file_content = file_content.expandtabs()
        file_content = "\n".join([f"{i + init_line:6}\t{line}" for i, line in enumerate(file_content.split("\n"))])
        return f"Here's the result of running `cat -n` on {file_descriptor}:\n" + file_content + "\n"

async def str_replace_editor(
    ctx: RunContext[Union[RustPrintDeps, SketchDocDeps, RefinementDeps, InputGenerationDeps, TestTransDeps, ExecutionRefinementDeps]],
    working_dir: Literal["c_repo", "c_doc", "rust_repo", "rust_doc"],
    command: Literal["view", "create", "str_replace", "insert", "undo_edit"],
    path: str,
    file_text: Optional[str] = None,
    view_range: Optional[List[int]] = None,
    old_str: Optional[str] = None,
    new_str: Optional[str] = None,
    insert_line: Optional[int] = None,
) -> str:
    """
    Custom editing tool for viewing, creating and editing files
        * State is persistent across command calls and discussions with the user
        * If `path` is a file, `view` displays the result of applying `cat -n`. If `path` is a directory, `view` lists non-hidden files and directories up to 2 levels deep.
        * The `create` command cannot be used if the specified `path` already exists as a file
        * If a `command` generates a long output, it will be truncated and marked with `<response clipped>`
        * The `undo_edit` command will revert the last edit made to the file at `path`
        * Only `view` command is allowed for `c_repo` (READ-ONLY, C source should never be modified)

    Args:
        working_dir: The working directory to use. Four types:
            - `c_repo`: C source code repository (READ-ONLY, view-only)
            - `c_doc`: Generated C documentation (flexible access based on task)
            - `rust_repo`: Translated Rust code repository (flexible access based on task)
            - `rust_doc`: Generated Rust sketch documentation (flexible access based on task)
        command: The command to run. Allowed options are: `view`, `create`, `str_replace`, `insert`, `undo_edit`.
        path: Path to file or directory, e.g. `./chat_core.md` or `./agents/`
        file_text: Required parameter of `create` command, with the content of the file to be created.
        view_range: Optional parameter of `view` command when `path` points to a file. If none is given, the full file is shown. If provided, the file will be shown in the indicated line number range, e.g. [11, 12] will show lines 11 and 12. Indexing at 1 to start. Setting `[start_line, -1]` shows all lines from `start_line` to the end of the file.
        old_str: Required parameter of `str_replace` command containing the string in `path` to replace.
        new_str: Optional parameter of `str_replace` command containing the new string (if not given, no string will be added). Required parameter of `insert` command containing the string to insert.
    """
    
    logger.info("="*80)
    logger.info(f"TOOL CALL: str_replace_editor")
    logger.info(f"  Command: {command}")
    logger.info(f"  Working Directory: {working_dir}")
    logger.info(f"  Target Path: {path}")
    if view_range:
        logger.info(f"  View Range: lines {view_range[0]}-{view_range[1]}")
    if command == "str_replace":
        logger.info(f"  Action: Replacing text in file")
    elif command == "create":
        logger.info(f"  Action: Creating new file")
    elif command == "view":
        logger.info(f"  Action: Reading file/directory")
    logger.info("="*80)

    deps = ctx.deps
    
    global _view_count
    
    if command == "view":
        _view_count += 1
        
        if _view_count > MAX_VIEWS_BEFORE_FORCE_STOP:
            return f"""CRITICAL ERROR: Exceeded maximum view limit ({MAX_VIEWS_BEFORE_FORCE_STOP} views)!

You have viewed too many files..

IMMEDIATE ACTION REQUIRED:
1. STOP reading more files
2. Use the information you have already gathered
"""
        
        view_key = f"{working_dir}:{path}"
        if view_key in _view_history:
            logger.warning(f"  WARNING: Already viewed {view_key} - redundant read detected")
            if _view_history.get(view_key, 0) >= 1:
                return f"""ERROR: You have already viewed '{path}' multiple times.

STOP reading the same path.

Views: {_view_count}/{MAX_VIEWS_BEFORE_FORCE_STOP}
"""
            _view_history[view_key] = _view_history.get(view_key, 0) + 1
        else:
            _view_history[view_key] = 1
    
    if command != "view" and working_dir == "c_repo":
        return "Error: Only 'view' command is allowed for 'c_repo' (READ-ONLY). C source code should never be modified."
    
    if isinstance(deps, RefinementDeps):
        tool = EditTool({}, "")
        
        if working_dir == "rust_repo":
            # Strip leading slash to prevent absolute path override
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.rust_workspace_path).resolve() / clean_path)
        elif working_dir == "rust_doc":
            return "Error: Cannot use rust_doc in Refinement context. You can ONLY modify Rust source code (.rs files) using working_dir='rust_repo'. DO NOT attempt to read or modify documentation files."
        else:
            return f"Error: Invalid working_dir '{working_dir}' for Refinement context. Only 'rust_repo' is allowed for code refinement."
            
    elif isinstance(deps, SketchDocDeps):
        tool = EditTool({}, "")
        
        if working_dir == "rust_repo":
            # Strip leading slash to prevent absolute path override
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.rust_workspace_path).resolve() / clean_path)
        elif working_dir == "rust_doc":
            # Sanitize path so we never create docs/Users/brian/... when LLM passes absolute-like path
            clean_path = _sanitize_rust_doc_path(path)
            absolute_path = str(Path(deps.sketch_docs_output_path).resolve() / clean_path)
        else:
            return f"Error: Invalid working_dir '{working_dir}' for SketchDoc context. Only rust_repo and rust_doc allowed to prevent data leakage."
    
    elif isinstance(deps, InputGenerationDeps):
        tool = EditTool({}, "")
        
        if working_dir == "c_repo":
            c_repo_path = Path(deps.c_repo_path).resolve()
            path_obj = Path(path)
            
            if path_obj.is_absolute():
                try:
                    clean_path = path_obj.relative_to(c_repo_path)
                except ValueError:
                    clean_path = Path(path.lstrip('/'))
            else:
                clean_path = Path(path.lstrip('/'))
            
            if command == "create":
                test_tmp_dir = c_repo_path / "test_tmp"
                test_tmp_dir.mkdir(parents=True, exist_ok=True)
                absolute_path = str(test_tmp_dir / clean_path)
            else:
                absolute_path = str(c_repo_path / clean_path)
        else:
            return f"Error: Invalid working_dir '{working_dir}' for InputGeneration context. Only 'c_repo' is allowed."

    elif isinstance(deps, TestTransDeps):
        tool = EditTool(deps.registry, None)
        if working_dir == "c_repo":
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.absolute_c_repo_path).resolve() / clean_path)
        elif working_dir == "rust_repo":
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.absolute_rust_output_path).resolve() / clean_path)
        else:
            return f"Error: Invalid working_dir '{working_dir}' for TestTrans context. Use c_repo or rust_repo."

    elif isinstance(deps, ExecutionRefinementDeps):
        tool = EditTool({}, "")
        if working_dir == "rust_repo":
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.rust_workspace_path).resolve() / clean_path)
        else:
            return f"Error: Invalid working_dir '{working_dir}' for Execution Refinement context. Only rust_repo is allowed."

    elif hasattr(deps, 'absolute_c_repo_path'):
        tool = EditTool(deps.registry, deps.absolute_docs_path)
        
        if working_dir == "c_repo":
            # Strip leading slash to prevent absolute path override
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.absolute_c_repo_path).resolve() / clean_path)
        elif working_dir == "c_doc":
            # Strip leading slash to prevent absolute path override
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.absolute_docs_path).resolve() / clean_path)
        elif working_dir == "rust_repo":
            # Strip leading slash to prevent absolute path override
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.absolute_rust_output_path).resolve() / clean_path)
        else:
            return f"Error: Invalid working_dir '{working_dir}' for C2Rust context. Use: c_repo, c_doc, or rust_repo"
            
    else:
        tool = EditTool(deps.registry, deps.absolute_docs_path)
        
        if working_dir == "c_repo":
            # Strip leading slash to prevent absolute path override
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.absolute_repo_path).resolve() / clean_path)
        elif working_dir == "c_doc":
            # Strip leading slash to prevent absolute path override
            clean_path = path.lstrip('/')
            absolute_path = str(Path(deps.absolute_docs_path).resolve() / clean_path)
        else:
            return f"Error: Invalid working_dir '{working_dir}' for RustPrint context. Use: c_repo or c_doc"
    
    logger.info(f"[str_replace_editor] Executing command '{command}' on path: {absolute_path}")
    
    try:
        tool(
            command=command,
            path=absolute_path,
            file_text=file_text,
            view_range=view_range,
            old_str=old_str,
            new_str=new_str,
            insert_line=insert_line,
        )
        logger.info(f"[str_replace_editor] Command '{command}' completed successfully")
    except Exception as e:
        logger.error(f"[str_replace_editor] Command '{command}' failed with error: {e}")
        raise

    result = "\n".join(tool.logs)

    if command != "view" and path.endswith(".md"):
        mermaid_validation = await validate_mermaid_diagrams(absolute_path, path)
        result = result + "\n---------- Mermaid validation ----------\n" + mermaid_validation

    return result


str_replace_editor_tool = Tool(
    function=str_replace_editor,
    name="str_replace_editor",
    description="""
Custom editing tool for viewing, creating and editing files
    * State is persistent across command calls and discussions with the user
    * If `path` is a file, `view` displays the result of applying `cat -n`. If `path` is a directory, `view` lists non-hidden files and directories up to 2 levels deep.
    * The `create` command cannot be used if the specified `path` already exists as a file
    * If a `command` generates a long output, it will be truncated and marked with `<response clipped>`
    * The `undo_edit` command will revert the last edit made to the file at `path`
    * Only `view` command is allowed for `c_repo` (READ-ONLY)
    
Four working directory types:
    * `c_repo`: C source code repository (READ-ONLY, view-only, never modified)
    * `c_doc`: Generated C documentation (flexible access based on task)
    * `rust_repo`: Translated Rust code repository (flexible access based on task)
    * `rust_doc`: Generated Rust sketch documentation (flexible access based on task)
""".strip(),
    takes_ctx=True
)


