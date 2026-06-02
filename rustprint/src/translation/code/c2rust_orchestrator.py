from pydantic_ai import Agent, UsageLimits
import logging
import os
import time
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

pydantic_logger = logging.getLogger('pydantic_ai')
pydantic_logger.setLevel(logging.DEBUG)

MAX_REQUESTS_PLANNER = 500
MAX_REQUESTS_SKELETON = 500
MAX_REQUESTS_SYNTHESIS = 500

def log_info(msg: str, func_name: str = ""):
    if func_name:
        logger.info(f"[{func_name} @ c2rust_orchestrator.py] {msg}")
    else:
        logger.info(msg)

from rustprint.src.agent_tools.deps import C2RustDeps
from rustprint.src.agent_tools.read_code_components import read_code_components_tool
from rustprint.src.agent_tools.str_replace_editor import str_replace_editor_tool
from rustprint.src.agent_tools.read_documentation import read_documentation_tool
from rustprint.src.agent_tools.find_code_component import find_code_component_tool
from rustprint.src.agent_tools.cargo_check import cargo_check_tool, cargo_fix_tool
from rustprint.src.agent_tools.unsafe_detect import unsafe_detect_tool
from rustprint.src.llm_services import create_main_model
from rustprint.src.agent_retry import run_agent_with_retry
from rustprint.src.prompt_template import PLANNER_PROMPT, SKELETON_PROMPT, SYNTHESIS_PROMPT
from rustprint.src.config import Config


class C2RustOrchestrator:
    
    def __init__(self, config: Config):
        self.config = config
        self.model = create_main_model(config)

    def create_planner_agent(self) -> Agent:
        """Planner creates IMPLEMENTATION_PLAN.md for each feature"""
        return Agent(
            self.model,
            name="C2Rust_Planner",
            deps_type=C2RustDeps,
            tools=[
                read_code_components_tool,
                str_replace_editor_tool,
                read_documentation_tool,
                find_code_component_tool,
            ],
            system_prompt=PLANNER_PROMPT,
            retries=2,
            end_strategy='early',
        )
    
    def create_skeleton_agent(self) -> Agent:
        """Skeleton reads plan and generates Rust code"""
        return Agent(
            self.model,
            name="C2Rust_Skeleton",
            deps_type=C2RustDeps,
            tools=[
                read_code_components_tool,
                str_replace_editor_tool,
                read_documentation_tool,
                find_code_component_tool,
                cargo_check_tool,
                cargo_fix_tool,
                unsafe_detect_tool,
            ],
            system_prompt=SKELETON_PROMPT,
            retries=2,
            end_strategy='early',
        )
    
    def create_synthesis_agent(self) -> Agent:
        return Agent(
            self.model,
            name="C2Rust_Synthesis",
            deps_type=C2RustDeps,
            tools=[
                str_replace_editor_tool,
                find_code_component_tool,
                cargo_check_tool,
                cargo_fix_tool,
            ],
            system_prompt=SYNTHESIS_PROMPT,
            end_strategy='early',
        )
    
    def create_all_folders(
        self,
        module_tree: Dict[str, Any],
        parent_path: str,
        depth: int = 0,
        repo_name: Optional[str] = None,
    ):
        if repo_name and len(module_tree) == 1 and next(iter(module_tree.keys())) == repo_name:
            log_info("Single crate at root: no subfolder created", "create_all_folders")
            return
        for feature_name, feature_info in module_tree.items():
            feature_path = os.path.join(parent_path, feature_name)
            os.makedirs(feature_path, exist_ok=True)
            log_info(f"Created: {feature_name}/", "create_all_folders")
    
    async def process_feature_recursive(
        self,
        feature_name: str,
        feature_info: Dict[str, Any],
        parent_path: str,
        deps: C2RustDeps,
        planner: Agent,
        skeleton_agent: Agent,
        depth: int = 0,
        output_path_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        BOTTOM-UP processing: Process children first, then parent.
        When output_path_override is set (e.g. single crate at root), use it as feature_output_path.
        """
        feature_start = time.time()
        indent = "  " * depth
        
        feature_output_path = output_path_override if output_path_override else os.path.join(parent_path, feature_name)
        feature_components = feature_info.get("components", [])
        feature_children = feature_info.get("children", {})
        
        log_info(f"{indent}[Feature] {feature_name}", "process_feature_recursive")
        log_info(f"{indent}  Components: {len(feature_components)}, Children: {len(feature_children)}", "process_feature_recursive")
        log_info(f"{indent}  Output: {feature_output_path}", "process_feature_recursive")
        
        deps.current_module = feature_name
        deps.absolute_rust_output_path = feature_output_path
        # Crate root = this feature's output path for top-level; parent path for children (crate from module_tree).
        deps.current_crate_root = feature_output_path if depth == 0 else parent_path

        component_list = "\n".join([f"  - {comp_id}" for comp_id in feature_components])

        doc_files_list = [f"{feature_name}.md"]
        for child_name in feature_children.keys():
            doc_files_list.append(f"{child_name}.md")
        
        doc_files_info = "\n".join([f"  - {doc}" for doc in doc_files_list])
        
        planner_start = time.time()
        log_info(f"{indent}  [Phase 1: Planner] Creating implementation plan...", "process_feature_recursive")
        
        plan_path = os.path.join(feature_output_path, "IMPLEMENTATION_PLAN.md")
        max_planner_attempts = 3
        plan_created = False
        
        for attempt in range(1, max_planner_attempts + 1):
            if attempt > 1:
                log_info(f"{indent}  [Retry {attempt}/{max_planner_attempts}] Attempting to create plan...", "process_feature_recursive")
            
            planner_prompt = f"""
Create a comprehensive implementation plan file named IMPLEMENTATION_PLAN.md for translating the C module '{feature_name}' to Rust.

<MODULE_INFO>
Module Name: {feature_name}
C Components: {len(feature_components)}
Children Modules: {len(feature_children)}
Output Directory: {feature_output_path}
</MODULE_INFO>

<C_COMPONENTS>
{component_list}
</C_COMPONENTS>

<DOCUMENTATION_FILES>
Available documentation in c_docs:
{doc_files_info}
</DOCUMENTATION_FILES>
"""
            
            try:
                planner_result = await run_agent_with_retry(
                    planner,
                    planner_prompt,
                    deps=deps,
                    message_history=None,
                    usage_limits=UsageLimits(request_limit=MAX_REQUESTS_PLANNER)
                )


                if os.path.exists(plan_path):
                    plan_created = True
                    log_info(f"{indent}  ✓ IMPLEMENTATION_PLAN.md created", "process_feature_recursive")
                    break
                else:
                    log_info(f"{indent}  ⚠ Attempt {attempt}: Plan not created", "process_feature_recursive")
                    if attempt < max_planner_attempts:
                        log_info(f"{indent}    Agent may have only analyzed code. Retrying with stronger instruction...", "process_feature_recursive")
            except Exception as e:
                log_info(f"{indent}  ⚠ Attempt {attempt} failed: {str(e)}", "process_feature_recursive")
        
        planner_time = time.time() - planner_start
        log_info(f"{indent}  Planner completed in {planner_time:.2f}s", "process_feature_recursive")
        
        if not plan_created:
            log_info(f"{indent}  ✗ FAILED: Could not create IMPLEMENTATION_PLAN.md after {max_planner_attempts} attempts", "process_feature_recursive")
            log_info(f"{indent}  Skipping this feature", "process_feature_recursive")
            return {
                "output_path": feature_output_path,
                "has_children": False,
                "children_results": {},
                "timing": {"planner": planner_time, "failed": True},
                "error": "Failed to create implementation plan"
            }
        
        skeleton_start = time.time()
        log_info(f"{indent}  [Phase 2: Implementation] Generating Rust code...", "process_feature_recursive")
        
        skeleton_prompt = f"""
Implement Rust code for module '{feature_name}' following IMPLEMENTATION_PLAN.md specifications.

<OUTPUT_PATH>
{feature_output_path}
</OUTPUT_PATH>

"""
        
        skeleton_result = await run_agent_with_retry(
            skeleton_agent,
            skeleton_prompt,
            deps=deps,
            message_history=None,
            usage_limits=UsageLimits(request_limit=MAX_REQUESTS_SKELETON)
        )


        skeleton_time = time.time() - skeleton_start
        log_info(f"{indent}  Implementation completed in {skeleton_time:.2f}s", "process_feature_recursive")
        
        # Show what was created
        if os.path.exists(feature_output_path):
            created_files = []
            for root, dirs, files in os.walk(feature_output_path):
                for file in files:
                    rel_path = os.path.relpath(os.path.join(root, file), feature_output_path)
                    created_files.append(rel_path)
            
            if created_files:
                log_info(f"{indent}  Created {len(created_files)} files:", "process_feature_recursive")
                for f in sorted(created_files)[:15]:
                    log_info(f"{indent}    - {f}", "process_feature_recursive")
                if len(created_files) > 15:
                    log_info(f"{indent}    ... and {len(created_files) - 15} more", "process_feature_recursive")
            else:
                log_info(f"{indent}  ⚠ WARNING: No files created!", "process_feature_recursive")
        
        result = {
            "output_path": feature_output_path,
            "has_children": len(feature_children) > 0,
            "children_results": {},
            "timing": {"planner": planner_time, "skeleton": skeleton_time}
        }
        
        feature_time = time.time() - feature_start
        result["timing"]["total"] = feature_time
        
        log_info(f"{indent}✓ {feature_name} complete (total: {feature_time:.2f}s)", "process_feature_recursive")
        return result
    
    async def translate_module(
        self,
        c_repo_path: str,
        docs_path: str,
        components: Dict[str, Any],
        top_files: Dict[str, Any],
        module_tree: Dict[str, Any],
        output_dir: str
    ) -> C2RustDeps:
        
        translation_start = time.time()
        
        repo_name = os.path.basename(os.path.normpath(c_repo_path))
        # Output directory should already include the full path
        # Don't add extra 'translated_repos' folder
        rust_output_path = os.path.join(output_dir, repo_name)
        os.makedirs(rust_output_path, exist_ok=True)

        from pathlib import Path

        log_info(f"Initializing Rust translation for: {repo_name}", "translate_module")
        log_info(f"   C source: {c_repo_path}", "translate_module")
        log_info(f"   Documentation: {docs_path}", "translate_module")
        log_info(f"   Rust output: {rust_output_path}", "translate_module")
        log_info(f"   Modules to translate: {len(module_tree)}", "translate_module")
        
        deps = C2RustDeps(
            absolute_c_repo_path=c_repo_path,
            absolute_rust_output_path=rust_output_path,
            absolute_docs_path=docs_path,
            registry=top_files,
            components=components,
            translation_plan={},
            current_module="",
            completed_steps=[],
            config=self.config,
        )
        
        logger.info("\n" + "─"*80)
        logger.info("Starting Rust code generation workflow")
        logger.info("  Step 1: Planner - Creates IMPLEMENTATION_PLAN.md from docs")
        logger.info("  Step 2: Implementation - Generates actual Rust code from plan")
        logger.info("  Folder structure: Top-level only (sub-folders created by agent)")
        logger.info("─"*80)
        
        logger.info("\n📁 Creating top-level folder structure...")
        self.create_all_folders(module_tree, rust_output_path, repo_name=repo_name)
        logger.info("✓ Top-level folders created\n")
        
        planner = self.create_planner_agent()
        skeleton_agent = self.create_skeleton_agent()
        synthesis_agent = self.create_synthesis_agent()
        
        feature_results = {}
        all_timings = []
        
        single_crate_at_root = len(module_tree) == 1 and next(iter(module_tree.keys())) == repo_name
        deps.single_crate_at_root = single_crate_at_root
        for feature_idx, (feature_name, feature_info) in enumerate(module_tree.items(), 1):
            logger.info(f"\n{'='*80}")
            logger.info(f"Feature [{feature_idx}/{len(module_tree)}]: {feature_name}")
            logger.info(f"{'='*80}")
            out_override = rust_output_path if single_crate_at_root else None
            result = await self.process_feature_recursive(
                feature_name,
                feature_info,
                rust_output_path,
                deps,
                planner,
                skeleton_agent,
                depth=0,
                output_path_override=out_override,
            )
            
            feature_results[feature_name] = result
            all_timings.append({
                "feature": feature_name,
                "time": result.get("timing", {}).get("total", 0)
            })
            
            logger.info(f"\nFeature {feature_idx}/{len(module_tree)} completed: {feature_name}")
            if result['has_children']:
                logger.info(f"   Children processed: {len(result['children_results'])}")
        
        features_time = time.time() - translation_start
        
        logger.info("\n" + "="*80)
        logger.info("Starting ROOT workspace synthesis")
        logger.info("="*80)
        
        synthesis_start = time.time()
        
        logger.info(f"Features to synthesize: {len(feature_results)}")
        logger.info(f"Workspace directory: {rust_output_path}")
        
        deps.absolute_rust_output_path = rust_output_path
        deps.current_module = repo_name
        
        crate_names = list(module_tree.keys())
        crate_list_quoted = ', '.join(f'"{c}"' for c in crate_names)
        if single_crate_at_root:
            synthesis_prompt = f"""
Single-crate project '{repo_name}': the Rust crate is already at the repo root (Cargo.toml and src/ were created by the implementation step). Do NOT create a new Cargo.toml or workspace. Only create README.md (project overview, build: cargo build) and .gitignore (target/, Cargo.lock). Use str_replace_editor with working_dir='rust_repo' to create ./README.md and ./.gitignore only.
"""
        else:
            synthesis_prompt = f"""
Create ROOT workspace files for translated Rust project '{repo_name}'.

<PARAMETER>
crate_names (from module_tree): [{crate_list_quoted}]
</PARAMETER>

<CONTEXT>
Workspace ROOT directory: {rust_output_path}
</CONTEXT>

For each crate in the parameter list above, cd into that folder: use view with paths like ./<crate_name>/Cargo.toml, ./<crate_name>/README.md, ./<crate_name>/src/lib.rs (e.g. ./allocators/Cargo.toml, ./cbor/Cargo.toml). Read each crate's files once, then create root Cargo.toml (members = [{crate_list_quoted}]), README.md, .gitignore. Do NOT view path='.' repeatedly.
"""

        try:
            synthesis_result = await run_agent_with_retry(
                synthesis_agent,
                synthesis_prompt,
                deps=deps,
                message_history=None,
                usage_limits=UsageLimits(request_limit=MAX_REQUESTS_SYNTHESIS)
            )
            synthesis_time = time.time() - synthesis_start


            logger.info(f"Workspace synthesis completed ({synthesis_time:.2f}s)")
        except Exception as e:
            synthesis_time = time.time() - synthesis_start
            error_msg = str(e)
            logger.warning(f"Workspace synthesis exceeded limit ({synthesis_time:.2f}s): {error_msg}")

            if "request_limit" in error_msg.lower() or "usage" in error_msg.lower():
                if single_crate_at_root:
                    logger.info("Single crate at root: creating README and .gitignore only...")
                    try:
                        readme_path = os.path.join(rust_output_path, "README.md")
                        if not os.path.exists(readme_path):
                            with open(readme_path, 'w') as f:
                                f.write(f"""# {repo_name}

Rust translation of {repo_name} C repository.

## Build

```bash
cargo build
```
""")
                            logger.info("✓ Created README.md")
                        gitignore_path = os.path.join(rust_output_path, ".gitignore")
                        if not os.path.exists(gitignore_path):
                            with open(gitignore_path, 'w') as f:
                                f.write("target/\nCargo.lock\n")
                            logger.info("✓ Created .gitignore")
                    except Exception as fallback_error:
                        logger.error(f"Failed to create fallback files: {str(fallback_error)}")
                else:
                    logger.info("Using agent's exploration history to create workspace files...")
                    force_create_prompt = f"""
You have explored the workspace features. Now CREATE the workspace files based on what you've seen.

DO NOT explore more. Just CREATE these files:

1. Create ./Cargo.toml:
   ```toml
   [workspace]
   members = [{', '.join(f'"{k}"' for k in crate_names)}]

   [workspace.package]
   edition = "2026"
   ```

2. Create ./README.md:
   - Project: {repo_name}
   - Modules: {', '.join(crate_names)}
   - Build: cargo build

3. Create ./.gitignore:
   ```
   target/
   Cargo.lock
   ```

Use str_replace_editor with working_dir='rust_repo' to CREATE these 3 files NOW.
"""
                    try:
                        synthesis_result = await run_agent_with_retry(
                            synthesis_agent,
                            force_create_prompt,
                            deps=deps,
                            message_history=synthesis_agent._result_schema if hasattr(synthesis_agent, '_result_schema') else None,
                            usage_limits=UsageLimits(request_limit=MAX_REQUESTS_SYNTHESIS)
                        )
                        logger.info("✓ Created workspace files using agent history")
                    except Exception as retry_error:
                        logger.warning(f"Retry also failed: {str(retry_error)}")
                        logger.warning("Creating minimal workspace files as final fallback...")
                        try:
                            cargo_toml_path = os.path.join(rust_output_path, "Cargo.toml")
                            with open(cargo_toml_path, 'w') as f:
                                f.write(f"""[workspace]
members = [
    {', '.join(f'"{k}"' for k in crate_names)}
]

[workspace.package]
edition = "2025"
""")
                            readme_path = os.path.join(rust_output_path, "README.md")
                            with open(readme_path, 'w') as f:
                                f.write(f"""# {repo_name}

Rust translation of {repo_name} C repository.

## Modules

{chr(10).join(f'- `{k}`' for k in crate_names)}

## Build

```bash
cargo build
```
""")
                            gitignore_path = os.path.join(rust_output_path, ".gitignore")
                            with open(gitignore_path, 'w') as f:
                                f.write("target/\nCargo.lock\n")
                            logger.info("✓ Created minimal workspace files manually")
                        except Exception as fallback_error:
                            logger.error(f"Failed to create fallback files: {str(fallback_error)}")
            else:
                raise
        
        total_time = time.time() - translation_start
        
        logger.info("\n" + "="*80)
        logger.info("TRANSLATION WORKFLOW COMPLETED")
        logger.info("="*80)
        logger.info(f"Repository: {repo_name}")
        logger.info(f"Output: {rust_output_path}")
        logger.info(f"Features translated: {len(feature_results)}")
        logger.info(f"\nPhase timing:")
        logger.info(f"   Per-feature translation: {features_time:.2f}s")
        logger.info(f"   Workspace synthesis:     {synthesis_time:.2f}s")
        logger.info(f"   Total Stage 3 time:      {total_time:.2f}s")
        
        if all_timings:
            logger.info(f"\nFeature timing breakdown (top 5):")
            sorted_timings = sorted(all_timings, key=lambda x: x['time'], reverse=True)
            for idx, timing in enumerate(sorted_timings[:5], 1):
                logger.info(f"   {idx}. {timing['feature']}: {timing['time']:.2f}s")
            if len(sorted_timings) > 5:
                logger.info(f"   ... and {len(sorted_timings) - 5} more features")
        
        logger.info("="*80 + "\n")
        
        return deps
