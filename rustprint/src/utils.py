import os
import json
import re
import asyncio
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple
import logging
import tiktoken

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# ---------------------- File Manager ---------------------
# ------------------------------------------------------------

class FileManager:
    """Handles file I/O operations."""
    
    @staticmethod
    def ensure_directory(path: str) -> None:
        """Create directory if it doesn't exist."""
        os.makedirs(path, exist_ok=True)
    
    @staticmethod
    def save_json(data: Any, filepath: str) -> None:
        """Save data as JSON to file."""
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
    
    @staticmethod
    def load_json(filepath: str) -> Optional[Dict[str, Any]]:
        """Load JSON from file, return None if file doesn't exist."""
        if not os.path.exists(filepath):
            return None
        
        with open(filepath, 'r') as f:
            return json.load(f)
    
    @staticmethod
    def save_text(content: str, filepath: str) -> None:
        """Save text content to file."""
        with open(filepath, 'w') as f:
            f.write(content)
    
    @staticmethod
    def load_text(filepath: str) -> str:
        """Load text content from file."""
        with open(filepath, 'r') as f:
            return f.read()

file_manager = FileManager()


# ------------------------------------------------------------
# ---------------------- Complexity Check --------------------
# ------------------------------------------------------------

def is_complex_module(components: dict, core_component_ids: list) -> bool:
    files = set()
    for component_id in core_component_ids:
        if component_id in components:
            files.add(components[component_id].file_path)
    return len(files) > 1


# ------------------------------------------------------------
# ---------------------- Token Counting ---------------------
# ------------------------------------------------------------

enc = tiktoken.encoding_for_model("gpt-4")

def count_tokens(text: str) -> int:
    """Count the number of tokens in a text."""
    return len(enc.encode(text))


# ------------------------------------------------------------
# ---------------------- Mermaid Validation -----------------
# ------------------------------------------------------------

async def validate_mermaid_diagrams(md_file_path: str, relative_path: str) -> str:
    """
    Validate all Mermaid diagrams in a markdown file.

    Returns "All mermaid diagrams are syntax correct" if all diagrams are valid,
    otherwise returns an error message with details about invalid diagrams.
    """
    try:
        file_path = Path(md_file_path)
        if not file_path.exists():
            return f"Error: File '{md_file_path}' does not exist"
        content = file_path.read_text(encoding='utf-8')
        mermaid_blocks = extract_mermaid_blocks(content)
        if not mermaid_blocks:
            return "No mermaid diagrams found in the file"
        errors = []
        for i, (line_start, diagram_content) in enumerate(mermaid_blocks, 1):
            error_msg = await validate_single_diagram(diagram_content, i, line_start)
            if error_msg:
                errors.append("\n")
                errors.append(error_msg)
        if errors:
            return "Mermaid syntax errors found in file: " + relative_path + "\n" + "\n".join(errors)
        else:
            return "All mermaid diagrams in file: " + relative_path + " are syntax correct"
    except Exception as e:
        return f"Error processing file: {str(e)}"


def extract_mermaid_blocks(content: str) -> List[Tuple[int, str]]:
    """Extract all mermaid code blocks from markdown content."""
    mermaid_blocks = []
    lines = content.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line == '```mermaid' or line.startswith('```mermaid'):
            start_line = i + 1
            diagram_lines = []
            i += 1
            while i < len(lines):
                if lines[i].strip() == '```':
                    break
                diagram_lines.append(lines[i])
                i += 1
            if diagram_lines:
                mermaid_blocks.append((start_line, '\n'.join(diagram_lines)))
        i += 1
    return mermaid_blocks


async def validate_single_diagram(diagram_content: str, diagram_num: int, line_start: int) -> str:
    """Validate a single mermaid diagram. Returns error message or empty string."""
    import sys
    core_error = ""
    try:
        from mermaid_parser.parser import parse_mermaid_py
        try:
            old_stderr = sys.stderr
            sys.stderr = open(os.devnull, 'w')
            try:
                await parse_mermaid_py(diagram_content)
            finally:
                sys.stderr.close()
                sys.stderr = old_stderr
        except Exception as e:
            error_str = str(e)
            match = re.search(r"Error:(.*?)(?=Stack Trace:|$)", error_str, re.DOTALL)
            if match:
                core_error = match.group(0).strip()
            else:
                logger.error(f"No match found for error pattern, fallback to mermaid-py\n{error_str}")
                raise Exception(error_str)
    except Exception:
        logger.warning("Using mermaid-py to validate mermaid diagrams")
        try:
            import mermaid as md
            render = md.Mermaid(diagram_content)
            core_error = render.svg_response.text
        except Exception as e:
            return f"  Diagram {diagram_num}: Exception during validation - {str(e)}"
    if core_error:
        line_match = re.search(r'line (\d+)', core_error)
        if line_match:
            error_line_in_diagram = int(line_match.group(1))
            actual_line_in_file = line_start + error_line_in_diagram
            nl = '\n'
            return f"Diagram {diagram_num}: Parse error on line {actual_line_in_file}:{nl}{nl.join(core_error.split(nl)[1:])}"
        else:
            return f"Diagram {diagram_num}: {core_error}"
    return ""
