"""
CLI adapter for documentation generator backend.

This adapter wraps the existing backend documentation_generator.py
and provides CLI-specific functionality like progress reporting.
"""

from pathlib import Path
from typing import Dict, Any
import time
import asyncio
import os
import logging
import sys


from rustprint.cli.utils.progress import ProgressTracker
from rustprint.cli.models.job import DocumentationJob, LLMConfig
from rustprint.cli.utils.errors import APIError

# Import backend modules
from rustprint.src.documentation.c.documentation_generator import DocumentationGenerator
from rustprint.src.config import Config as BackendConfig, set_cli_context

logger = logging.getLogger(__name__)


class CLIDocumentationGenerator:
    """
    CLI adapter for documentation generation with progress reporting.
    
    This class wraps the backend documentation generator and adds
    CLI-specific features like progress tracking and error handling.
    """
    
    def __init__(
        self,
        repo_path: Path,
        output_dir: Path,
        config: Dict[str, Any],
        verbose: bool = False
    ):
        """
        Initialize the CLI documentation generator.

        Args:
            repo_path: Repository path
            output_dir: Output directory
            config: LLM configuration
            verbose: Enable verbose output
        """
        self.repo_path = repo_path
        self.output_dir = output_dir
        self.config = config
        self.verbose = verbose
        self.progress_tracker = ProgressTracker(total_stages=5, verbose=verbose)
        self.job = DocumentationJob()
        
        # Setup job metadata
        self.job.repository_path = str(repo_path)
        self.job.repository_name = repo_path.name
        self.job.output_directory = str(output_dir)
        self.job.llm_config = LLMConfig(
            main_model=config.get('main_model', ''),
            cluster_model=config.get('cluster_model', ''),
            base_url=config.get('base_url', '')
        )
        
        # Configure backend logging
        self._configure_backend_logging()
    
    def _configure_backend_logging(self):
        """Configure backend logger for CLI use with colored output."""
        from rustprint.src.dependency_analyzer.utils.logging_config import ColoredFormatter

        # Get backend logger (parent of all backend modules)
        backend_logger = logging.getLogger('rustprint.src')

        if os.environ.get("RUSTPRINT_LIVE_LOG") == "1":
            backend_logger.setLevel(logging.INFO)
            backend_logger.propagate = True
            return

        # Remove existing handlers to avoid duplicates
        backend_logger.handlers.clear()

        if self.verbose:
            # In verbose mode, show INFO and above
            backend_logger.setLevel(logging.INFO)
            
            # Create console handler with formatting
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)
            
            # Use colored formatter for better readability
            colored_formatter = ColoredFormatter()
            console_handler.setFormatter(colored_formatter)
            
            # Add handler to logger
            backend_logger.addHandler(console_handler)
        else:
            # In non-verbose mode, suppress backend logs (use WARNING level to hide INFO/DEBUG)
            backend_logger.setLevel(logging.WARNING)
            
            # Create console handler for warnings and errors only
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setLevel(logging.WARNING)
            
            # Use colored formatter even for warnings/errors
            colored_formatter = ColoredFormatter()
            console_handler.setFormatter(colored_formatter)
            
            backend_logger.addHandler(console_handler)
        
        # Prevent propagation to root logger to avoid duplicate messages
        backend_logger.propagate = False
    
    def generate(self) -> DocumentationJob:
        """
        Generate documentation with progress tracking.
        
        Returns:
            Completed DocumentationJob
            
        Raises:
            APIError: If LLM API call fails
        """
        self.job.start()
        start_time = time.time()
        
        try:
            # Set CLI context for backend
            set_cli_context(True)
            
            # Create backend config with CLI settings
            backend_config = BackendConfig.from_cli(
                repo_path=str(self.repo_path),
                output_dir=str(self.output_dir),
                llm_base_url=self.config.get('base_url'),
                llm_api_key=self.config.get('api_key'),
                main_model=self.config.get('main_model'),
                cluster_model=self.config.get('cluster_model')
            )
            
            # Run backend documentation generation
            asyncio.run(self._run_backend_generation(backend_config))

            # Stage 5: Finalization (metadata already created by backend)
            self._finalize_job()
            
            # Complete job
            generation_time = time.time() - start_time
            self.job.complete()
            
            return self.job
            
        except APIError as e:
            self.job.fail(str(e))
            raise
        except Exception as e:
            self.job.fail(str(e))
            raise
    
    async def _run_backend_generation(self, backend_config: BackendConfig):
        """Run the backend documentation generation with progress tracking."""
        
        # Stage 1: Dependency Analysis
        self.progress_tracker.start_stage(1, "Dependency Analysis")
        if self.verbose:
            self.progress_tracker.update_stage(0.2, "Initializing dependency analyzer...")
        
        # Create documentation generator
        doc_generator = DocumentationGenerator(backend_config)
        
        if self.verbose:
            self.progress_tracker.update_stage(0.5, "Parsing source files...")
        
        # Build dependency graph
        try:
            components, leaf_nodes = doc_generator.graph_builder.build_dependency_graph()
            top_files = doc_generator.graph_builder.parser.top_files
            self.job.statistics.total_files_analyzed = len(components)
            self.job.statistics.leaf_nodes = len(leaf_nodes)
            
            if self.verbose:
                self.progress_tracker.update_stage(1.0, f"Found {len(leaf_nodes)} leaf nodes")
        except Exception as e:
            raise APIError(f"Dependency analysis failed: {e}")
        
        self.progress_tracker.complete_stage()
        
        # Stage 2: Module Clustering
        self.progress_tracker.start_stage(2, "Module Clustering")
        if self.verbose:
            self.progress_tracker.update_stage(0.5, "Clustering modules with LLM...")
        
        # Import clustering function
        from rustprint.src.documentation.c.cluster_modules import cluster_modules
        from rustprint.src.utils import file_manager
        from rustprint.src.config import FIRST_MODULE_TREE_FILENAME, MODULE_TREE_FILENAME
        
        working_dir = backend_config.docs_dir
        logger.info(f"[_run_backend_generation @ doc_generator.py] Working dir for module tree: {working_dir}")
        file_manager.ensure_directory(working_dir)
        first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        logger.info(f"[_run_backend_generation @ doc_generator.py] Module tree paths: {first_module_tree_path}, {module_tree_path}")
        
        try:
            if os.path.exists(first_module_tree_path):
                logger.info(f"[_run_backend_generation @ doc_generator.py] Module tree cache FOUND at {first_module_tree_path}")
                module_tree = file_manager.load_json(first_module_tree_path)
                if module_tree is None:
                    module_tree = {}
                logger.info(f"[_run_backend_generation @ doc_generator.py] Loaded cached module tree with {len(module_tree)} modules")
            else:
                logger.info(f"[_run_backend_generation @ doc_generator.py] Module tree cache NOT FOUND, running clustering...")
                module_tree = cluster_modules(top_files, components, backend_config)
                if module_tree is None:
                    module_tree = {}
                file_manager.save_json(module_tree, first_module_tree_path)
                logger.info(f"[_run_backend_generation @ doc_generator.py] Clustering complete, saved {len(module_tree)} modules to cache")
            
            file_manager.save_json(module_tree, module_tree_path)
            self.job.module_count = len(module_tree) if module_tree else 0
            
            if self.verbose:
                if module_tree:
                    self.progress_tracker.update_stage(1.0, f"Created {len(module_tree)} modules")
                else:
                    self.progress_tracker.update_stage(1.0, "Skipped clustering (small repo)")
        except Exception as e:
            raise APIError(f"Module clustering failed: {e}")
        
        self.progress_tracker.complete_stage()
        
        # Stage 3: Documentation Generation
        self.progress_tracker.start_stage(3, "Documentation Generation")
        if self.verbose:
            self.progress_tracker.update_stage(0.1, "Generating module documentation...")
        
        try:
            # Run the actual documentation generation
            await doc_generator.generate_module_documentation(components, leaf_nodes)
            
            if self.verbose:
                self.progress_tracker.update_stage(0.9, "Creating repository overview...")
            
            # Create metadata
            doc_generator.create_documentation_metadata(working_dir, components, len(leaf_nodes))
            
            # Collect generated files
            for file_path in os.listdir(working_dir):
                if file_path.endswith('.md') or file_path.endswith('.json'):
                    self.job.files_generated.append(file_path)
            
        except Exception as e:
            raise APIError(f"Documentation generation failed: {e}")
        
        self.progress_tracker.complete_stage()

    def _finalize_job(self):
        """Finalize the job (metadata already created by backend)."""
        # Just verify metadata exists
        metadata_path = self.output_dir / "metadata.json"
        if not metadata_path.exists():
            # Create our own if backend didn't
            with open(metadata_path, 'w') as f:
                f.write(self.job.to_json())

