import os
import json
import logging
import argparse
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple, Optional, Any, Union
from pathlib import Path
import re

from rustprint.src.dependency_analyzer.analysis.analysis_service import AnalysisService
from rustprint.src.dependency_analyzer.models.core import Node


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class DependencyParser:
    """Parser for extracting code components from multi-language repositories."""
    
    def __init__(self, repo_path: str):
        self.repo_path = os.path.abspath(repo_path)
        self.components: Dict[str, Node] = {}
        self.modules: Set[str] = set()
        self.top_files: Dict[str, Any] = {}
        
        self.analysis_service = AnalysisService()

    def parse_repository(self, filtered_folders: List[str] = None) -> Dict[str, Node]:
        logger.debug(f"Parsing repository at {self.repo_path}")
        
        structure_result = self.analysis_service._analyze_structure(
            self.repo_path, 
            include_patterns=None,
            exclude_patterns=None
        )
        
        call_graph_result = self.analysis_service._analyze_call_graph(
            structure_result["file_tree"], 
            self.repo_path
        )
        
        self._build_components_from_analysis(call_graph_result)
        
        self.find_top_files(self.components)
        
        logger.debug(f"Found {len(self.components)} components across {len(self.modules)} modules")
        return self.components
    
    def _build_components_from_analysis(self, call_graph_result: Dict):
        functions = call_graph_result.get("functions", [])
        relationships = call_graph_result.get("relationships", [])
        
        component_id_mapping = {}
        
        for func_dict in functions:
            component_id = func_dict.get("id", "")
            if not component_id:
                continue
                
            node = Node(
                id=component_id,
                name=func_dict.get("name", ""),
                component_type=func_dict.get("component_type", func_dict.get("node_type", "function")),
                file_path=func_dict.get("file_path", ""),
                relative_path=func_dict.get("relative_path", ""),
                source_code=func_dict.get("source_code", func_dict.get("code_snippet", "")),
                start_line=func_dict.get("start_line", 0),
                end_line=func_dict.get("end_line", 0),
                has_docstring=func_dict.get("has_docstring", bool(func_dict.get("docstring", ""))),
                docstring=func_dict.get("docstring", "") or "",
                parameters=func_dict.get("parameters", []),
                node_type=func_dict.get("node_type", "function"),
                base_classes=func_dict.get("base_classes"),
                class_name=func_dict.get("class_name"),
                display_name=func_dict.get("display_name", ""),
                component_id=component_id
            )
            
            self.components[component_id] = node
            
            component_id_mapping[component_id] = component_id
            legacy_id = f"{func_dict.get('file_path', '')}:{func_dict.get('name', '')}"
            if legacy_id and legacy_id != component_id:
                component_id_mapping[legacy_id] = component_id
            
            if "." in component_id:
                module_parts = component_id.split(".")[:-1]  
                module_path = ".".join(module_parts)
                if module_path:
                    self.modules.add(module_path)
        
        processed_relationships = 0
        for rel_dict in relationships:
            caller_id = rel_dict.get("caller", "")
            callee_id = rel_dict.get("callee", "")
            is_resolved = rel_dict.get("is_resolved", False)
            
            caller_component_id = component_id_mapping.get(caller_id)
            
            callee_component_id = component_id_mapping.get(callee_id)
            if not callee_component_id:
                for comp_id, comp_node in self.components.items():
                    if comp_node.name == callee_id:
                        callee_component_id = comp_id
                        break
            
            if caller_component_id and caller_component_id in self.components:
                if callee_component_id:
                    self.components[caller_component_id].depends_on.add(callee_component_id)
                    processed_relationships += 1
    
    def _determine_component_type(self, func_dict: Dict) -> str:
        if func_dict.get("is_method", False):
            return "method"
        
        node_type = func_dict.get("node_type", "")
        if node_type in ["class", "interface", "struct", "enum", "record", "abstract class", "annotation", "delegate"]:
            return node_type
            
        return "function"
    
    def _file_to_module_path(self, file_path: str) -> str:
        path = file_path
        extensions = ['.py', '.js', '.ts', '.java', '.cs', '.cpp', '.hpp', '.h', '.c', '.tsx', '.jsx', '.cc', '.mjs', '.cxx', '.cc', '.cjs', '.rs']
        for ext in extensions:
            if path.endswith(ext):
                path = path[:-len(ext)]
                break
        return path.replace(os.path.sep, ".")
    
    def load_dependency_graph(self, input_path: str) -> Dict[str, Node]:
        if not os.path.exists(input_path):
            logger.debug(f"Dependency graph file not found at {input_path}")
            return {}
        
        with open(input_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        components = {}
        for component_id, component_dict in data.items():
            if 'depends_on' in component_dict and isinstance(component_dict['depends_on'], list):
                component_dict['depends_on'] = set(component_dict['depends_on'])
            
            node = Node(**component_dict)
            components[component_id] = node
            
            if "." in component_id:
                module_parts = component_id.split(".")[:-1]
                module_path = ".".join(module_parts)
                if module_path:
                    self.modules.add(module_path)
        
        self.components = components
        logger.debug(f"Loaded {len(components)} components from {input_path}")
        return components
    
    def save_dependency_graph(self, output_path: str):
        result = {}
        for component_id, component in self.components.items():
            component_dict = component.model_dump()
            if 'depends_on' in component_dict and isinstance(component_dict['depends_on'], set):
                component_dict['depends_on'] = list(component_dict['depends_on'])
            result[component_id] = component_dict
        
        dir_name = os.path.dirname(output_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        
        logger.debug(f"Saved {len(self.components)} components to {output_path}")
        return result
    
    def find_top_files(self, components: Dict[str, Node]) -> Dict[str, Any]:
        SOURCE_EXTENSIONS = ('.c', '.h', '.rs')
        c_files_components = {}
        for comp_id, comp in components.items():
            file_path = comp.relative_path
            if not file_path.lower().endswith(SOURCE_EXTENSIONS):
                continue
            if file_path not in c_files_components:
                c_files_components[file_path] = []
            c_files_components[file_path].append(comp_id)
        
        top_files = {}
        for file_path, comp_ids in c_files_components.items():
            is_top_file = True
            
            for comp_id in comp_ids:
                is_called_externally = False
                
                for other_comp_id, other_comp in components.items():
                    if other_comp.relative_path == file_path:
                        continue
                    
                    if 'test' in other_comp.relative_path.lower():
                        continue
                    
                    if comp_id in other_comp.depends_on:
                        is_called_externally = True
                        break
                
                if is_called_externally:
                    is_top_file = False
                    break
            
            if is_top_file:
                uncalled_components = []
                for comp_id in comp_ids:
                    if components[comp_id].component_type == "method":
                        continue
                    
                    is_called = False
                    for other_comp_id, other_comp in components.items():
                        if comp_id == other_comp_id:
                            continue
                        
                        if 'test' in other_comp.relative_path.lower():
                            continue
                        
                        if comp_id in other_comp.depends_on:
                            is_called = True
                            break
                    
                    if not is_called:
                        uncalled_components.append(comp_id)
                
                if uncalled_components:
                    top_files[file_path] = {
                        "file_name": file_path,
                        "components": uncalled_components
                    }
        
        self.top_files = top_files
        logger.debug(f"Found {len(top_files)} top files")
        return top_files
    
    def save_top_files(self, output_path: str):
        dir_name = os.path.dirname(output_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(self.top_files, f, indent=2, ensure_ascii=False)
        
        logger.debug(f"Saved {len(self.top_files)} top files to {output_path}")
        return self.top_files
    
    def load_top_files(self, input_path: str) -> Dict[str, Any]:
        if not os.path.exists(input_path):
            logger.debug(f"Top files file not found at {input_path}")
            return {}
        
        with open(input_path, 'r', encoding='utf-8') as f:
            top_files = json.load(f)
        
        self.top_files = top_files
        logger.debug(f"Loaded {len(top_files)} top files from {input_path}")
        return top_files


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    parser = argparse.ArgumentParser(description='Parse repository dependency graph')
    parser.add_argument('--repo-path', required=True, help='Path to repository')
    
    args = parser.parse_args()
    
    repo_path = os.path.abspath(args.repo_path)
    
    if not os.path.exists(repo_path):
        logger.error(f"Repository path does not exist: {repo_path}")
        return 1
    
    output_base_dir = 'output'
    dependency_graph_dir = os.path.join(output_base_dir, 'dependency_graphs')
    os.makedirs(dependency_graph_dir, exist_ok=True)
    
    repo_name = os.path.basename(os.path.normpath(repo_path))
    sanitized_repo_name = ''.join(c if c.isalnum() else '_' for c in repo_name)
    output_path = os.path.join(dependency_graph_dir, f"{sanitized_repo_name}_dependency_graph.json")
    
    top_files_dir = os.path.join(output_base_dir, 'top_files')
    os.makedirs(top_files_dir, exist_ok=True)
    top_files_path = os.path.join(top_files_dir, f"{sanitized_repo_name}_top_files.json")
    
    logger.info(f"Parsing repository: {repo_path}")
    logger.info(f"Output path: {output_path}")
    logger.info(f"Top files path: {top_files_path}")
    
    dep_parser = DependencyParser(repo_path)
    
    if os.path.exists(output_path):
        logger.info(f"Loading existing dependency graph from {output_path}")
        components = dep_parser.load_dependency_graph(output_path)
    else:
        logger.info(f"Creating new dependency graph")
        components = dep_parser.parse_repository()
        dep_parser.save_dependency_graph(output_path)
    
    if os.path.exists(top_files_path):
        logger.info(f"Loading existing top files from {top_files_path}")
        top_files = dep_parser.load_top_files(top_files_path)
    else:
        logger.info(f"Finding top files")
        top_files = dep_parser.find_top_files(components)
        dep_parser.save_top_files(top_files_path)
    
    logger.info(f"Successfully processed {len(components)} components")
    logger.info(f"Found {len(top_files)} top files")
    return 0


if __name__ == '__main__':
    exit(main())
