import logging
import os
import time
from typing import Dict, List, Any
from pydantic_ai import Agent, UsageLimits

logger = logging.getLogger(__name__)

from rustprint.src.agent_tools.deps import SketchDocDeps
from rustprint.src.agent_tools.str_replace_editor import str_replace_editor_tool, reset_view_history
from rustprint.src.agent_tools.find_code_component import find_code_component_tool
from rustprint.src.agent_tools.read_documentation import read_documentation_tool
from rustprint.src.llm_services import create_main_model, build_model_settings_dict
from rustprint.src.agent_retry import run_agent_with_retry
from rustprint.src.prompt_template import SKETCH_DOC_LEAF_PROMPT, SKETCH_DOC_PARENT_PROMPT
from rustprint.src.config import Config
from rustprint.src.utils import file_manager


class SketchDocGenerator:
    
    def __init__(self, config: Config):
        self.config = config
        self.model = create_main_model(config)

    def create_leaf_doc_agent(self) -> Agent:
        return Agent(
            self.model,
            name="SketchDoc_Leaf",
            deps_type=SketchDocDeps,
            tools=[
                str_replace_editor_tool,
                find_code_component_tool,
            ],
            system_prompt=SKETCH_DOC_LEAF_PROMPT,
            retries=2,
            end_strategy='early',
        )
    
    def create_parent_doc_agent(self) -> Agent:
        return Agent(
            self.model,
            name="SketchDoc_Parent",
            deps_type=SketchDocDeps,
            tools=[
                str_replace_editor_tool,
                read_documentation_tool,
                find_code_component_tool,
            ],
            system_prompt=SKETCH_DOC_PARENT_PROMPT,
            retries=2,
            end_strategy='early',
        )
    
    def get_processing_order(self, module_tree: Dict[str, Any], parent_path: List[str] = []) -> List[tuple[List[str], str, bool]]:
        processing_order = []
        
        def collect_modules(tree: Dict[str, Any], path: List[str]):
            for module_name, module_info in tree.items():
                current_path = path + [module_name]
                children = module_info.get("children", {})
                has_children = children and isinstance(children, dict) and len(children) > 0
                
                if has_children:
                    collect_modules(children, current_path)
                    processing_order.append((current_path, module_name, True))
                else:
                    processing_order.append((current_path, module_name, False))
        
        collect_modules(module_tree, parent_path)
        return processing_order
    
    async def generate_module_doc(
        self,
        module_name: str,
        is_parent: bool,
        rust_workspace_path: str,
        sketch_docs_output_path: str,
        deps: SketchDocDeps,
        leaf_agent: Agent,
        parent_agent: Agent,
        depth: int = 0
    ):
        start_time = time.time()
        indent = "  " * depth
        
        reset_view_history()
        
        logger.info(f"{indent}[Module: {module_name}] Type: {'Parent' if is_parent else 'Leaf'}")
        
        output_doc_path = os.path.join(sketch_docs_output_path, f"{module_name}.md")
        
        deps.current_module_name = module_name
        deps.rust_workspace_path = rust_workspace_path
        deps.sketch_docs_output_path = sketch_docs_output_path
        
        logger.info(f"{indent}  Rust workspace: {rust_workspace_path}")
        logger.info(f"{indent}  Output: {output_doc_path}")
        
        if is_parent:
            logger.info(f"{indent}  [Parent Agent] Generating documentation with children context...")
            
            children_docs = []
            for file in os.listdir(sketch_docs_output_path):
                if file.endswith('.md') and file != f"{module_name}.md" and file != "overview.md":
                    children_docs.append(file.replace('.md', ''))
            
            prompt = f"""Generate high-level feature-focused documentation for the Rust module '{module_name}'.

This module is located in the Rust workspace. You need to explore and find the correct subfolder that corresponds to this module.
For example, if documenting 'child' module under 'parent', look for the folder at parent/child.

Children modules documentation available: {', '.join(children_docs) if children_docs else 'None'}

Your task:
1. Explore the Rust workspace to find the folder for module '{module_name}'
2. Read the Rust code in that module folder
3. Read children module documentation using read_documentation tool to understand their features
4. Identify the HIGH-LEVEL FEATURES this module provides
5. Generate feature-focused documentation (NOT function listings)
6. Create the file: {output_doc_path}

IMPORTANT: Focus on FEATURES (capabilities and functionality), not individual function listings.
Document what the module enables users to do, how children modules contribute to features, and how components integrate.
"""
            
            result = await run_agent_with_retry(
                parent_agent,
                prompt,
                deps=deps,
                message_history=None,
                model_settings=build_model_settings_dict(self.config.main_model, 0.0, 16000),
                usage_limits=UsageLimits(request_limit=500)
            )
        else:
            logger.info(f"{indent}  [Leaf Agent] Generating documentation from workspace...")
            
            prompt = f"""Generate high-level feature-focused documentation for the Rust module '{module_name}'.

This module is located in the Rust workspace. You need to explore and find the correct subfolder that corresponds to this module.
For example, if documenting 'child' module under 'parent', look for the folder at parent/child.

Your task:
1. Explore the Rust workspace to find the folder for module '{module_name}'
2. Read all Rust source files in that module folder
3. Identify the HIGH-LEVEL FEATURES this module provides
4. Generate feature-focused documentation (NOT function listings)
5. Create the file: {output_doc_path}

IMPORTANT: Focus on FEATURES (capabilities and functionality), not individual function listings.
Document what the module enables users to do and how features work conceptually.
"""
            
            result = await run_agent_with_retry(
                leaf_agent,
                prompt,
                deps=deps,
                message_history=None,
                model_settings=build_model_settings_dict(self.config.main_model, 0.0, 16000),
                usage_limits=UsageLimits(request_limit=500)
            )

        elapsed = time.time() - start_time
        
        if os.path.exists(output_doc_path):
            logger.info(f"{indent}  Documentation created: {output_doc_path} ({elapsed:.1f}s)")
        else:
            logger.warning(f"{indent}  Documentation not created ({elapsed:.1f}s) - Forcing generation with available information")
            
            force_prompt = f"""CRITICAL: You must generate documentation NOW with the information you have already gathered.

You have explored the Rust module '{module_name}' but did not create the documentation file yet.

IMMEDIATE ACTION REQUIRED:
1. Use ALL the information you have gathered from your exploration
2. Generate documentation based on what you were able to read
3. If you couldn't find all files, document what you DID find
4. Create the file: {output_doc_path}

Even partial documentation is better than no documentation. Generate it NOW using:
str_replace_editor(working_dir='rust_doc', command='create', path='./{module_name}.md', file_text='...')
"""
            
            try:
                retry_result = await run_agent_with_retry(
                    parent_agent if is_parent else leaf_agent,
                    force_prompt,
                    deps=deps,
                    message_history=result.all_messages() if hasattr(result, 'all_messages') else None,
                    model_settings=build_model_settings_dict(self.config.main_model, 0.0, 8000),
                    usage_limits=UsageLimits(request_limit=500)
                )

                if os.path.exists(output_doc_path):
                    logger.info(f"{indent}  Documentation created after retry: {output_doc_path}")
                else:
                    logger.error(f"{indent}  Failed to create documentation even after retry")
            except Exception as e:
                logger.error(f"{indent}  Retry failed with error: {e}")
        
        return result
    
    async def generate_documentation(
        self,
        repo_name: str,
        c_docs_path: str,
        rust_translated_path: str,
        output_sketch_docs_path: str
    ):
        logger.info("="*80)
        logger.info("SKETCH DOCUMENTATION GENERATION")
        logger.info("="*80)
        logger.info(f"Repo: {repo_name}")
        logger.info(f"C docs: {c_docs_path}")
        logger.info(f"Rust translated: {rust_translated_path}")
        logger.info(f"Output: {output_sketch_docs_path}")
        logger.info("="*80)
        
        file_manager.ensure_directory(output_sketch_docs_path)

        from pathlib import Path

        module_tree_path = os.path.join(c_docs_path, "module_tree.json")
        module_tree = None
        
        if os.path.exists(module_tree_path):
            module_tree = file_manager.load_json(module_tree_path)
        
        if not module_tree:
            logger.warning("Module tree not found or empty - will generate overview from entire Rust workspace")
            logger.info("Scanning Rust workspace to discover structure...")
            
            leaf_agent = self.create_leaf_doc_agent()
            parent_agent = self.create_parent_doc_agent()
            
            deps = SketchDocDeps(
                current_module_name="overview",
                rust_workspace_path=rust_translated_path,
                sketch_docs_output_path=output_sketch_docs_path,
            )
            
            overview_output = os.path.join(output_sketch_docs_path, "overview.md")
            
            reset_view_history()
            
            logger.info("\n[Overview] Generating repository overview from entire Rust workspace...")
            
            prompt = f"""Generate comprehensive high-level feature-focused repository overview documentation by exploring the entire Rust workspace.

Rust workspace: {rust_translated_path}

Your task:
1. Explore the entire Rust workspace structure using str_replace_editor(working_dir='rust_repo', command='view')
   - Read Cargo.toml (if exists) to understand workspace structure and dependencies
   - Read README.md (if exists) to understand project purpose
   - Explore all directories and Rust source files to discover modules and features
2. Identify all major features and capabilities provided by this Rust repository
3. Understand how different modules/components work together
4. Synthesize a HIGH-LEVEL overview of repository features
5. Create the file: {overview_output}

IMPORTANT: 
- Focus on repository-level FEATURES and capabilities, not file-by-file listings
- Document what the repository enables, key feature areas, and how components integrate
- Include architecture diagrams (Mermaid) showing feature flows and component interactions
- If you find multiple modules/crates, document how they relate and work together
"""
            
            _overview_result = await run_agent_with_retry(
                parent_agent,
                prompt,
                deps=deps,
                message_history=None,
                model_settings=build_model_settings_dict(self.config.main_model, 0.0, 16000),
                usage_limits=UsageLimits(request_limit=500)
            )

            if os.path.exists(overview_output):
                logger.info(f"  Overview created: {overview_output}")

            logger.info("\n" + "="*80)
            logger.info("SKETCH DOCUMENTATION GENERATION COMPLETED")
            logger.info("="*80)
            return
        
        logger.info(f"Loaded module tree with {len(module_tree)} top-level modules")
        
        processing_order = self.get_processing_order(module_tree)
        logger.info(f"Processing order: {len(processing_order)} modules (bottom-up)")
        
        leaf_agent = self.create_leaf_doc_agent()
        parent_agent = self.create_parent_doc_agent()
        
        deps = SketchDocDeps(
            current_module_name="",
            rust_workspace_path="",
            sketch_docs_output_path=output_sketch_docs_path,
        )
        
        for idx, (module_path, module_name, is_parent) in enumerate(processing_order, 1):
            logger.info(f"\n[{idx}/{len(processing_order)}] Processing: {module_name}")
            
            await self.generate_module_doc(
                module_name=module_name,
                is_parent=is_parent,
                rust_workspace_path=rust_translated_path,
                sketch_docs_output_path=output_sketch_docs_path,
                deps=deps,
                leaf_agent=leaf_agent,
                parent_agent=parent_agent,
                depth=len(module_path) - 1
            )
        
        overview_output = os.path.join(output_sketch_docs_path, "overview.md")
        
        reset_view_history()
        
        logger.info("\n[Overview] Generating repository overview...")
        deps.current_module_name = "overview"
        deps.rust_workspace_path = rust_translated_path
        
        generated_docs = []
        for file in os.listdir(output_sketch_docs_path):
            if file.endswith('.md'):
                generated_docs.append(file.replace('.md', ''))
        
        prompt = f"""Generate high-level feature-focused repository overview documentation.

Rust workspace: {rust_translated_path}
Generated module documentation: {', '.join(generated_docs) if generated_docs else 'None'}

Your task:
1. Read generated module documentation using read_documentation tool to understand features
2. Read the root Rust workspace (Cargo.toml, README.md) if needed using str_replace_editor
3. Synthesize a HIGH-LEVEL overview of repository features
4. Create the file: {overview_output}

IMPORTANT: Focus on repository-level FEATURES and capabilities, not module-by-module listings.
Document what the repository enables, key feature areas, and how modules integrate to provide functionality.
"""
        
        _final_overview_result = await run_agent_with_retry(
            parent_agent,
            prompt,
            deps=deps,
            message_history=None,
            model_settings=build_model_settings_dict(self.config.main_model, 0.0, 16000),
            usage_limits=UsageLimits(request_limit=500)
        )

        if os.path.exists(overview_output):
            logger.info(f"  Overview created: {overview_output}")

        logger.info("\n" + "="*80)
        logger.info("SKETCH DOCUMENTATION GENERATION COMPLETED")
        logger.info("="*80)
