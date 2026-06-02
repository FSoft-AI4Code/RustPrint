"""Utility functions for working with module trees."""
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def clean_empty_children(module_tree: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove children nodes that have empty components list recursively.
    
    Args:
        module_tree: The module tree dictionary to clean
        
    Returns:
        Cleaned module tree with empty children removed
    """
    if not isinstance(module_tree, dict):
        return module_tree
    
    cleaned_tree = {}
    
    for module_name, module_data in module_tree.items():
        if not isinstance(module_data, dict):
            cleaned_tree[module_name] = module_data
            continue
        
        module_data = module_data.copy()
        
        components = module_data.get("components", [])
        children = module_data.get("children", {})
        
        if children:
            cleaned_children = {}
            for child_name, child_data in children.items():
                if not isinstance(child_data, dict):
                    cleaned_children[child_name] = child_data
                    continue
                
                child_components = child_data.get("components", [])
                child_children = child_data.get("children", {})
                
                if not child_components and not child_children:
                    logger.debug(f"[clean_empty_children @ module_tree_utils.py] Removing empty child: {child_name}")
                    continue
                
                if child_children:
                    child_data = child_data.copy()
                    child_data["children"] = clean_empty_children(child_children)
                
                cleaned_children[child_name] = child_data
            
            module_data["children"] = cleaned_children
        
        cleaned_tree[module_name] = module_data
    
    return cleaned_tree
