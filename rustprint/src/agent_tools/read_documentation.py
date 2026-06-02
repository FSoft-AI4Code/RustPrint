from pydantic_ai import RunContext, Tool
from rustprint.src.agent_tools.deps import C2RustDeps, SketchDocDeps
from typing import Union
import os
import logging

logger = logging.getLogger(__name__)


async def read_documentation(
    ctx: RunContext[Union[C2RustDeps, SketchDocDeps]],
    file_path: str
) -> str:
    
    logger.info("=" * 80)
    logger.info("TOOL CALL: read_documentation")
    logger.info("  File path : %s", file_path)
    logger.info("=" * 80)

    deps = ctx.deps
    
    if isinstance(deps, SketchDocDeps):
        docs_dir = deps.sketch_docs_output_path
    else:
        docs_dir = deps.absolute_docs_path
    
    if not file_path.endswith('.md'):
        file_path = f"{file_path}.md"
    
    full_path = os.path.join(docs_dir, file_path)
    
    if not os.path.exists(full_path):
        available_docs = []
        if os.path.exists(docs_dir):
            for root, dirs, files in os.walk(docs_dir):
                for file in files:
                    if file.endswith('.md'):
                        rel_path = os.path.relpath(os.path.join(root, file), docs_dir)
                        available_docs.append(rel_path)
        
        return f"Documentation file not found: {file_path}\nAvailable files: {', '.join(available_docs[:10])}"
    
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        logger.info(f"Read documentation: {file_path}")
        return content
    except Exception as e:
        return f"Error reading documentation {file_path}: {str(e)}"


read_documentation_tool = Tool(
    function=read_documentation,
    name="read_documentation",
    description="Read generated markdown documentation files to understand system architecture and design",
    takes_ctx=True
)
