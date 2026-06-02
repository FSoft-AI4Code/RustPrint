import logging
import os
import time
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)


def _remove_non_readme_markdown(workspace_root: str) -> None:
    """Remove all .md files under workspace_root except README.md (case-insensitive)."""
    root = Path(workspace_root)
    if not root.is_dir():
        return
    removed = []
    for path in root.rglob("*.md"):
        if path.name.lower() != "readme.md":
            try:
                path.unlink()
                removed.append(str(path.relative_to(root)))
            except OSError as e:
                logger.warning("[c2rust_generator] Could not remove %s: %s", path, e)
    if removed:
        logger.info("[c2rust_generator] Removed non-README markdown files: %s", removed)

from rustprint.src.dependency_analyzer import DependencyGraphBuilder
from rustprint.src.translation.code.c2rust_orchestrator import C2RustOrchestrator
from rustprint.src.documentation.c.documentation_generator import DocumentationGenerator
from rustprint.src.config import Config
from rustprint.src.utils import file_manager


class C2RustGenerator:
    
    def __init__(self, config: Config):
        self.config = config
        self.graph_builder = DependencyGraphBuilder(config)
        self.orchestrator = C2RustOrchestrator(config)
        self.doc_generator = DocumentationGenerator(config)
    
    async def run(self) -> None:
        overall_start = time.time()
        logger.info("\n" + "="*80)
        logger.info("[run @ c2rust_generator.py] C2RUST TRANSLATION WORKFLOW STARTED")
        logger.info("="*80)
        
        try:
            logger.info("[run @ c2rust_generator.py] Stage 1: Building dependency graph...")
            stage1_start = time.time()
            components, leaf_nodes = self.graph_builder.build_dependency_graph()
            top_files = self.graph_builder.parser.top_files
            stage1_time = time.time() - stage1_start
            
            logger.info(f"[run @ c2rust_generator.py] Stage 1 Results:")
            logger.info(f"[run @ c2rust_generator.py]    - Components: {len(components)}")
            logger.info(f"[run @ c2rust_generator.py]    - Leaf nodes: {len(leaf_nodes)}")
            logger.info(f"[run @ c2rust_generator.py]    - Top files: {len(top_files)}")
            logger.info(f"[run @ c2rust_generator.py]    - Time: {stage1_time:.2f}s")
            
            stage2_start = time.time()
            docs_dir = self.config.docs_dir
            
            module_tree_path = os.path.join(docs_dir, "module_tree.json")
            overview_path = os.path.join(docs_dir, "overview.md")
            
            has_docs = (
                os.path.exists(docs_dir) and 
                (os.path.exists(module_tree_path) or os.path.exists(overview_path))
            )
            
            logger.info("\n" + "="*80)
            logger.info("[run @ c2rust_generator.py] STAGE 2: Documentation Generation")
            logger.info("="*80)
            
            if not has_docs:
                logger.info(f"[run @ c2rust_generator.py] Documentation not found in: {docs_dir}")
                logger.info(f"[run @ c2rust_generator.py] Generating documentation from scratch...")
                await self.doc_generator.run()
                stage2_time = time.time() - stage2_start
                logger.info(f"[run @ c2rust_generator.py] Documentation generated in {stage2_time:.2f}s")
            else:
                logger.info(f"[run @ c2rust_generator.py] Documentation found in: {docs_dir}")
                logger.info(f"[run @ c2rust_generator.py]    - module_tree.json: {'✓' if os.path.exists(module_tree_path) else '✗'}")
                logger.info(f"[run @ c2rust_generator.py]    - overview.md: {'✓' if os.path.exists(overview_path) else '✗'}")
                
                md_files = [f for f in os.listdir(docs_dir) if f.endswith('.md')]
                logger.info(f"[run @ c2rust_generator.py]    - Markdown files: {len(md_files)}")
                logger.info(f"[run @ c2rust_generator.py]    - Skipping documentation generation (using cache)")
                stage2_time = time.time() - stage2_start
                logger.info(f"[run @ c2rust_generator.py]    - Cache check time: {stage2_time:.2f}s")
            
            module_tree_path = os.path.join(docs_dir, "module_tree.json")
            import json
            module_tree = {}
            if os.path.exists(module_tree_path):
                with open(module_tree_path, 'r') as f:
                    module_tree = json.load(f)
                logger.info(f"[run @ c2rust_generator.py] Module tree loaded from: {module_tree_path}")
                logger.info(f"[run @ c2rust_generator.py]    - Top-level modules: {len(module_tree)}")
                
                total_features = 0
                for module_name, module_data in module_tree.items():
                    if isinstance(module_data, dict) and 'children' in module_data:
                        total_features += len(module_data['children'])
                    else:
                        total_features += 1
                logger.info(f"[run @ c2rust_generator.py]    - Total features to translate: {total_features}")
            else:
                logger.warning(f"[run @ c2rust_generator.py] module_tree.json not found at: {module_tree_path}")
                logger.warning(f"[run @ c2rust_generator.py]    Will use flat structure for translation")
            if not module_tree and components is not None:
                repo_name = os.path.basename(os.path.normpath(self.config.repo_path))
                module_tree = {
                    repo_name: {
                        "components": list(components.keys()),
                        "children": {},
                    }
                }
                logger.info(f"[run @ c2rust_generator.py] module_tree was empty; using single top-level feature: {repo_name}")
            
            stage3_start = time.time()
            logger.info("\n" + "="*80)
            logger.info("[run @ c2rust_generator.py] STAGE 3: Rust Translation")
            logger.info("="*80)
            
            output_dir = self.config.output_dir
            if output_dir.endswith('/temp'):
                output_dir = os.path.dirname(output_dir)
            os.makedirs(output_dir, exist_ok=True)
            
            logger.info(f"[run @ c2rust_generator.py] Output directory: {output_dir}")
            logger.info(f"[run @ c2rust_generator.py] Calling translate_module...")
            
            deps = await self.orchestrator.translate_module(
                c_repo_path=self.config.repo_path,
                docs_path=docs_dir,
                components=components,
                top_files=top_files,
                module_tree=module_tree,
                output_dir=output_dir
            )
            
            # Rule-based cleanup: remove .md files that are not README.md (keep only README per crate/root)
            _remove_non_readme_markdown(deps.absolute_rust_output_path)
            
            stage3_time = time.time() - stage3_start
            
            overall_time = time.time() - overall_start
            logger.info("\n" + "="*80)
            logger.info("[run @ c2rust_generator.py] C2RUST TRANSLATION COMPLETED SUCCESSFULLY")
            logger.info("="*80)
            logger.info(f"[run @ c2rust_generator.py] Rust output: {deps.absolute_rust_output_path}")
            logger.info(f"[run @ c2rust_generator.py] Completed steps: {len(deps.completed_steps)}")
            logger.info(f"[run @ c2rust_generator.py] Timing Summary:")
            logger.info(f"[run @ c2rust_generator.py]    Stage 1 (Dependency Graph): {stage1_time:.2f}s")
            logger.info(f"[run @ c2rust_generator.py]    Stage 2 (Documentation):    {stage2_time:.2f}s")
            logger.info(f"[run @ c2rust_generator.py]    Stage 3 (Translation):      {stage3_time:.2f}s")
            logger.info(f"[run @ c2rust_generator.py]    Total time:                 {overall_time:.2f}s")
            logger.info("="*80 + "\n")
            
        except Exception as e:
            overall_time = time.time() - overall_start
            logger.error("\n" + "="*80)
            logger.error("[run @ c2rust_generator.py] C2RUST TRANSLATION FAILED")
            logger.error("="*80)
            logger.error(f"[run @ c2rust_generator.py] Error: {str(e)}")
            logger.error(f"[run @ c2rust_generator.py] Time elapsed before failure: {overall_time:.2f}s")
            logger.error(f"[run @ c2rust_generator.py] Full traceback:")
            logger.error(traceback.format_exc())
            logger.error("="*80 + "\n")
            raise
