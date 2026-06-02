from pydantic_ai import Agent
# import logfire
import logging
import os
from typing import Dict, List, Any

# Configure logging and monitoring

logger = logging.getLogger(__name__)

# try:
#     # Configure logfire with environment variables for Docker compatibility
#     logfire_token = os.getenv('LOGFIRE_TOKEN')
#     logfire_project = os.getenv('LOGFIRE_PROJECT_NAME', 'default')
#     logfire_service = os.getenv('LOGFIRE_SERVICE_NAME', 'default')
    
#     if logfire_token:
#         # Configure with explicit token (for Docker)
#         logfire.configure(
#             token=logfire_token,
#             project_name=logfire_project,
#             service_name=logfire_service,
#         )
#     else:
#         # Use default configuration (for local development with logfire auth)
#         logfire.configure(
#             project_name=logfire_project,
#             service_name=logfire_service,
#         )
    
#     logfire.instrument_pydantic_ai()
#     logger.debug(f"Logfire configured successfully for project: {logfire_project}")
    
# except Exception as e:
#     logger.warning(f"Failed to configure logfire: {e}")

# Local imports
from rustprint.src.agent_tools.deps import RustPrintDeps
from rustprint.src.agent_tools.read_code_components import read_code_components_tool
from rustprint.src.agent_tools.str_replace_editor import str_replace_editor_tool
from rustprint.src.agent_tools.find_code_component import find_code_component_tool
from rustprint.src.documentation.c.generate_sub_module_documentations import generate_sub_module_documentation_tool
from rustprint.src.llm_services import create_main_model
from rustprint.src.agent_retry import run_agent_with_retry
from rustprint.src.documentation.c.module_tree_utils import clean_empty_children
from rustprint.src.prompt_template import (
    SYSTEM_PROMPT,
    LEAF_SYSTEM_PROMPT,
    format_user_prompt,
)
from rustprint.src.utils import is_complex_module
from rustprint.src.config import (
    Config,
    MODULE_TREE_FILENAME,
    OVERVIEW_FILENAME,
)
from rustprint.src.utils import file_manager
from rustprint.src.dependency_analyzer.models.core import Node


class AgentOrchestrator:
    """Orchestrates the AI agents for documentation generation."""
    
    def __init__(self, config: Config):
        self.config = config
        self.model = create_main_model(config)

    def create_agent(self, module_name: str, components: Dict[str, Any],
                    core_component_ids: List[str], has_children: bool = False) -> Agent:
        if is_complex_module(components, core_component_ids) and has_children:
            return Agent(
                self.model,
                name=module_name,
                deps_type=RustPrintDeps,
                tools=[
                    read_code_components_tool, 
                    str_replace_editor_tool, 
                    find_code_component_tool,
                    generate_sub_module_documentation_tool
                ],
                system_prompt=SYSTEM_PROMPT.format(module_name=module_name),
            )
        else:
            return Agent(
                self.model,
                name=module_name,
                deps_type=RustPrintDeps,
                tools=[read_code_components_tool, str_replace_editor_tool, find_code_component_tool],
                system_prompt=LEAF_SYSTEM_PROMPT.format(module_name=module_name),
            )
    
    async def process_module(self, module_name: str, components: Dict[str, Node], 
                           core_component_ids: List[str], module_path: List[str], working_dir: str) -> Dict[str, Any]:
        logger.info(f"[process_module @ agent_orchestrator.py] Processing module: {module_name}")
        
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)
        if module_tree is None:
            module_tree = {}
        
        current_module_info = module_tree
        for path_part in module_path:
            current_module_info = current_module_info.get(path_part, {})
            if isinstance(current_module_info, dict) and "children" in current_module_info:
                current_module_info = current_module_info["children"]
        
        has_children = False
        if isinstance(current_module_info, dict) and module_name in current_module_info:
            children = current_module_info[module_name].get("children", {})
            has_children = bool(children and isinstance(children, dict) and len(children) > 0)
        
        logger.info(f"[process_module @ agent_orchestrator.py] Module has children: {has_children}")
        
        agent = self.create_agent(module_name, components, core_component_ids, has_children=has_children)
        
        deps = RustPrintDeps(
            absolute_docs_path=working_dir,
            absolute_repo_path=str(os.path.abspath(self.config.repo_path)),
            registry={},
            components=components,
            path_to_current_module=module_path,
            current_module_name=module_name,
            module_tree=module_tree,
            max_depth=self.config.max_depth,
            current_depth=1,
            config=self.config
        )

        overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
        if os.path.exists(overview_docs_path):
            logger.info(f"[process_module @ agent_orchestrator.py] Overview docs already exists, skipping")
            return module_tree

        docs_path = os.path.join(working_dir, f"{module_name}.md")
        if os.path.exists(docs_path):
            logger.info(f"[process_module @ agent_orchestrator.py] Module docs already exists, skipping")
            return module_tree
        
        user_prompt = format_user_prompt(
            module_name=module_name,
            core_component_ids=core_component_ids,
            components=components,
            module_tree=deps.module_tree
        )
        
        logger.info(f"[process_module @ agent_orchestrator.py] Calling LLM agent for module: {module_name}")
        
        # Retry mechanism: try up to 3 times if file is not created
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    logger.warning(f"[process_module @ agent_orchestrator.py] Retry attempt {attempt}/{max_retries} for module: {module_name}")
                
                result = await run_agent_with_retry(agent, user_prompt, deps=deps)


                logger.info(f"[process_module @ agent_orchestrator.py] Agent completed for module: {module_name} (attempt {attempt})")
                
                # Check if the documentation file was actually created
                if os.path.exists(docs_path):
                    file_size = os.path.getsize(docs_path)
                    logger.info(f"[process_module @ agent_orchestrator.py] ✓ Documentation file created: {docs_path} ({file_size} bytes)")
                    
                    # Clean empty children before saving
                    logger.debug(f"[process_module @ agent_orchestrator.py] Cleaning empty children from module tree before saving")
                    cleaned_module_tree = clean_empty_children(deps.module_tree)
                    
                    # Success! Save cleaned module tree and return
                    file_manager.save_json(cleaned_module_tree, module_tree_path)
                    return cleaned_module_tree
                else:
                    # File not created - log error and prepare for retry
                    logger.error(f"[process_module @ agent_orchestrator.py] ✗ Documentation file NOT created: {docs_path}")
                    logger.error(f"[process_module @ agent_orchestrator.py] Working directory: {working_dir}")
                    logger.error(f"[process_module @ agent_orchestrator.py] Expected file name: {module_name}.md")
                    
                    if os.path.exists(working_dir):
                        existing_files = [f for f in os.listdir(working_dir) if f.endswith('.md')]
                        logger.error(f"[process_module @ agent_orchestrator.py] Markdown files in working_dir: {existing_files}")
                    
                    if attempt < max_retries:
                        # Prepare retry with explicit reminder
                        logger.warning(f"[process_module @ agent_orchestrator.py] Retrying with explicit file creation reminder...")
                        user_prompt = f"""{user_prompt}

⚠️ CRITICAL REMINDER:
You MUST create the file `{module_name}.md` in working_dir='c_doc' (for C code) or 'rust_doc' (for Rust code).
Previous attempt did not create this file. Please ensure you use:
- Tool: read_code_components
- Action: read multiple core component IDs first to ground the documentation
- Tool: str_replace_editor
- Command: create
- Working Directory: c_doc (or rust_doc if documenting Rust code)
- Path: {module_name}.md
- File Text: [your documentation content]

This is attempt {attempt + 1} of {max_retries}. The file MUST be created."""
                    else:
                        # Final attempt failed - raise error
                        raise RuntimeError(
                            f"Agent failed to create documentation file after {max_retries} attempts: {docs_path}. "
                            f"The agent may be using the wrong working_dir or filename. "
                            f"Expected: {module_name}.md in working_dir='c_doc' or 'rust_doc'."
                        )
                        
            except RuntimeError:
                # Re-raise RuntimeError (from failed retries)
                raise
            except Exception as e:
                # Handle other exceptions during agent.run()
                import traceback
                logger.error(f"[process_module @ agent_orchestrator.py] Error during agent.run() for {module_name}: {str(e)}")
                logger.error(f"[process_module @ agent_orchestrator.py] Exception type: {type(e).__name__}")
                logger.error(f"[process_module @ agent_orchestrator.py] Full traceback:\n{traceback.format_exc()}")
                
                if hasattr(e, '__cause__') and e.__cause__:
                    logger.error(f"[process_module @ agent_orchestrator.py] Caused by: {e.__cause__}")

                raise