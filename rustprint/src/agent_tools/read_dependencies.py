from pydantic_ai import RunContext, Tool
from rustprint.src.agent_tools.deps import InputGenerationDeps
import logging
import json

logger = logging.getLogger(__name__)

async def read_dependencies(ctx: RunContext[InputGenerationDeps], dependency_ids: list[str]) -> str:
    """Read the source code of dependency components
    
    Args:
        dependency_ids: List of component IDs to read
    """
    
    logger.info(f"TOOL: read_dependencies - Reading {len(dependency_ids)} dependency component(s)")
    for dep_id in dependency_ids:
        logger.info(f"  Dependency: {dep_id}")
    
    dependency_graph_path = ctx.deps.dependency_graph_path
    dependency_graph = {}
    
    try:
        import os
        from rustprint.src.utils import file_manager
        if os.path.exists(dependency_graph_path):
            dependency_graph = file_manager.load_json(dependency_graph_path) or {}
    except Exception as e:
        logger.error(f"Error loading dependency graph: {e}")
        return f"Error loading dependency graph: {e}"
    
    results = []
    
    for dep_id in dependency_ids:
        if dep_id not in dependency_graph:
            results.append(f"# Dependency {dep_id} not found in dependency graph\n")
        else:
            dep_component = dependency_graph[dep_id]
            results.append(f"# Dependency {dep_id}:\n")
            results.append(f"Name: {dep_component.get('name', 'N/A')}\n")
            results.append(f"Source Code:\n{dep_component.get('source_code', 'N/A')}\n\n")
    
    return "\n".join(results)

read_dependencies_tool = Tool(
    function=read_dependencies,
    name="read_dependencies",
    description="Read the source code of dependency components by their IDs",
    takes_ctx=True
)
