from typing import Dict, List, Any
import os
import time
from rustprint.src.config import Config, TOP_FILES_DIR
from rustprint.src.dependency_analyzer.ast_parser import DependencyParser
from rustprint.src.utils import file_manager

import logging
logger = logging.getLogger(__name__)


class DependencyGraphBuilder:
    
    def __init__(self, config: Config):
        self.config = config
        self.parser = None
    
    def build_dependency_graph(self) -> tuple[Dict[str, Any], List[str]]:
        start_time = time.time()
        logger.info("="*80)
        logger.info("STAGE 1: Building Dependency Graph")
        logger.info("="*80)
        
        logger.info(f"[build_graph @ dependency_graphs_builder.py] Config dependency_graph_dir: {self.config.dependency_graph_dir}")
        file_manager.ensure_directory(self.config.dependency_graph_dir)

        repo_name = os.path.basename(os.path.normpath(self.config.repo_path))
        sanitized_repo_name = ''.join(c if c.isalnum() else '_' for c in repo_name)
        dependency_graph_path = os.path.join(
            self.config.dependency_graph_dir, 
            f"{sanitized_repo_name}_dependency_graph.json"
        )
        filtered_folders_path = os.path.join(
            self.config.dependency_graph_dir, 
            f"{sanitized_repo_name}_filtered_folders.json"
        )

        parser = DependencyParser(self.config.repo_path)
        self.parser = parser

        filtered_folders = None
        
        top_files_dir = os.path.join(os.path.dirname(self.config.dependency_graph_dir), TOP_FILES_DIR)
        logger.info(f"[build_graph @ dependency_graphs_builder.py] Computed top_files_dir: {top_files_dir}")
        file_manager.ensure_directory(top_files_dir)
        top_files_path = os.path.join(top_files_dir, f"{sanitized_repo_name}_top_files.json")

        dep_graph_exists = os.path.exists(dependency_graph_path)
        top_files_exists = os.path.exists(top_files_path)
        
        logger.info(f"[build_graph @ dependency_graphs_builder.py] Repository: {repo_name}")
        logger.info(f"[build_graph @ dependency_graphs_builder.py] Dependency graph cache: {'FOUND' if dep_graph_exists else 'NOT FOUND'} at {dependency_graph_path}")
        logger.info(f"[build_graph @ dependency_graphs_builder.py] Top files cache: {'FOUND' if top_files_exists else 'NOT FOUND'} at {top_files_path}")

        if dep_graph_exists:
            load_start = time.time()
            logger.info(f"[build_graph @ dependency_graphs_builder.py] Loading dependency graph from cache...")
            components = parser.load_dependency_graph(dependency_graph_path)
            load_time = time.time() - load_start
            logger.info(f"[build_graph @ dependency_graphs_builder.py] Loaded {len(components)} components in {load_time:.2f}s")
            
            if top_files_exists:
                top_load_start = time.time()
                logger.info(f"[build_graph @ dependency_graphs_builder.py] Loading top files from cache...")
                parser.load_top_files(top_files_path)
                top_load_time = time.time() - top_load_start
                logger.info(f"[build_graph @ dependency_graphs_builder.py] Loaded {len(parser.top_files)} top files in {top_load_time:.2f}s")
            else:
                logger.info(f"[build_graph @ dependency_graphs_builder.py] Computing top files from components...")
                top_compute_start = time.time()
                parser.find_top_files(components)
                parser.save_top_files(top_files_path)
                top_compute_time = time.time() - top_compute_start
                logger.info(f"[build_graph @ dependency_graphs_builder.py] Computed {len(parser.top_files)} top files in {top_compute_time:.2f}s")
        else:
            logger.info(f"[build_graph @ dependency_graphs_builder.py] Parsing repository from scratch...")
            parse_start = time.time()
            components = parser.parse_repository(filtered_folders)
            parse_time = time.time() - parse_start
            logger.info(f"[build_graph @ dependency_graphs_builder.py] Parsed {len(components)} components in {parse_time:.2f}s")
            
            save_start = time.time()
            parser.save_dependency_graph(dependency_graph_path)
            parser.save_top_files(top_files_path)
            save_time = time.time() - save_start
            logger.info(f"[build_graph @ dependency_graphs_builder.py] Saved dependency graph and top files ({len(parser.top_files)} files) in {save_time:.2f}s")
        
        filter_start = time.time()
        c_components = {}
        for comp_id, comp in components.items():
            relative_path = comp.relative_path.lower()
            if relative_path.endswith('.c') or relative_path.endswith('.h'):
                c_components[comp_id] = comp
        
        filter_time = time.time() - filter_start
        logger.info(f"[build_graph @ dependency_graphs_builder.py] Filtered {len(c_components)} C components from {len(components)} total in {filter_time:.2f}s")
        
        leaf_components = []
        for file_path in parser.top_files:
            for comp_id, comp in c_components.items():
                if comp.relative_path == file_path:
                    if comp.component_type not in ["method"]:
                        leaf_components.append(comp_id)
        
        logger.info(f"Extracted {len(leaf_components)} leaf components from {len(parser.top_files)} top files")
        
        total_time = time.time() - start_time
        logger.info("="*80)
        logger.info(f"STAGE 1 COMPLETED in {total_time:.2f}s")
        logger.info(f"Components: {len(c_components)}, Leaf components: {len(leaf_components)}, Top files: {len(parser.top_files)}")
        logger.info("="*80)
        
        return c_components, leaf_components