from typing import List, Dict, Any
from collections import defaultdict
import logging
logger = logging.getLogger(__name__)

from rustprint.src.dependency_analyzer.models.core import Node
from rustprint.src.llm_services import call_llm
from rustprint.src.utils import count_tokens
from rustprint.src.config import MAX_TOKEN_PER_MODULE, Config
from rustprint.src.prompt_template import format_cluster_prompt


def format_top_files_for_prompt(top_files: Dict[str, Any]) -> str:
    formatted = ""
    for file_path in sorted(top_files.keys()):
        formatted += f"{file_path}\n"
    return formatted


def format_potential_core_components(leaf_nodes: List[str], components: Dict[str, Node]) -> tuple[str, str]:
    valid_leaf_nodes = []
    for leaf_node in leaf_nodes:
        if leaf_node in components:
            valid_leaf_nodes.append(leaf_node)
        else:
            logger.warning(f"Skipping invalid leaf node '{leaf_node}' - not found in components")
    
    leaf_nodes_by_file = defaultdict(list)
    for leaf_node in valid_leaf_nodes:
        leaf_nodes_by_file[components[leaf_node].relative_path].append(leaf_node)

    potential_core_components = ""
    potential_core_components_with_code = ""
    for file, leaf_nodes in dict(sorted(leaf_nodes_by_file.items())).items():
        potential_core_components += f"# {file}\n"
        potential_core_components_with_code += f"# {file}\n"
        for leaf_node in leaf_nodes:
            potential_core_components += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"{components[leaf_node].source_code}\n"

    return potential_core_components, potential_core_components_with_code


def cluster_modules(
    top_files: Dict[str, Any],
    components: Dict[str, Node],
    config: Config,
    current_module_tree: dict[str, Any] = {},
    current_module_name: str = None,
    current_module_path: List[str] = []
) -> Dict[str, Any]:
    if not top_files:
        logger.info("[cluster_modules @ cluster_modules.py] No top files to cluster")
        return {}
    
    logger.info(f"[cluster_modules @ cluster_modules.py] Starting clustering for {len(top_files)} files")
    
    files_list = format_top_files_for_prompt(top_files)
    
    num_files = len(top_files)
    estimated_tokens = num_files * 10000
    
    if estimated_tokens <= MAX_TOKEN_PER_MODULE or num_files <= 3:
        logger.info(f"[cluster_modules @ cluster_modules.py] Skipping clustering: {num_files} files, estimated {estimated_tokens} tokens (threshold: {MAX_TOKEN_PER_MODULE})")
        return {}

    logger.info(f"[cluster_modules @ cluster_modules.py] Calling LLM for clustering {num_files} files")
    prompt = format_cluster_prompt(files_list, current_module_tree, current_module_name)
    
    response = call_llm(prompt, config, model=config.cluster_model)
    
    logger.info(f"[cluster_modules @ cluster_modules.py] Received LLM response, parsing module tree...")

    try:
        if "<GROUPED_COMPONENTS>" not in response or "</GROUPED_COMPONENTS>" not in response:
            logger.error(f"[cluster_modules @ cluster_modules.py] Invalid LLM response format - missing component tags")
            return {}
        
        response_content = response.split("<GROUPED_COMPONENTS>")[1].split("</GROUPED_COMPONENTS>")[0]
        module_tree = eval(response_content)
        
        if not isinstance(module_tree, dict):
            logger.error(f"[cluster_modules @ cluster_modules.py] Invalid module tree format - expected dict, got {type(module_tree)}")
            return {}
        
        logger.info(f"[cluster_modules @ cluster_modules.py] Successfully parsed module tree with {len(module_tree)} top-level modules")
            
    except Exception as e:
        logger.error(f"[cluster_modules @ cluster_modules.py] Failed to parse LLM response: {e}")
        return {}

    if len(module_tree) <= 1:
        logger.info(f"[cluster_modules @ cluster_modules.py] Module tree too small ({len(module_tree)} modules), skipping clustering")
        return {}

    if current_module_tree == {}:
        current_module_tree = module_tree
    else:
        value = current_module_tree
        for key in current_module_path:
            value = value[key]["children"]
        for module_name, module_info in module_tree.items():
            del module_info["path"]
            value[module_name] = module_info

    for module_name, module_info in module_tree.items():
        clustered_files = module_info.get("components", [])
        
        sub_top_files = {}
        for file_path in clustered_files:
            if file_path in top_files:
                sub_top_files[file_path] = top_files[file_path]
        
        all_components = []
        for file_data in sub_top_files.values():
            all_components.extend(file_data.get("components", []))
        
        module_info["components"] = all_components
        
        current_module_path.append(module_name)
        module_info["children"] = {}
        module_info["children"] = cluster_modules(sub_top_files, components, config, current_module_tree, module_name, current_module_path)
        current_module_path.pop()

    return module_tree