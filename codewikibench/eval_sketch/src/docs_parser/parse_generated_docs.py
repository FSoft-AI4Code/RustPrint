import os
import json
import markdown_to_json
from typing import Any, List, Dict, Optional
from pydantic import BaseModel
import argparse

class DocPage(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    content: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}
    subpages: List['DocPage'] = []

def convert_to_dict(obj):
    """Convert Pydantic models to dictionaries for JSON serialization"""
    if isinstance(obj, DocPage):
        result = obj.model_dump()
        if "metadata" in result and "path" in result["metadata"]:
            path = result["metadata"]["path"]
            if isinstance(path, str) and (os.path.isabs(path) or "/" in path or "\\" in path):
                result["metadata"]["path"] = os.path.normpath(path) if path else None
        return result
    elif isinstance(obj, list):
        return [convert_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: convert_to_dict(value) for key, value in obj.items()}
    else:
        return obj

def generate_detailed_keys_tree(obj, path=None):
    """
    Generate a detailed tree structure showing only keys until reaching string values.
    Traverses the complete structure and returns only the key hierarchy.
    Includes path information for DocPage objects.
    """
    if path is None:
        path = []
    
    if isinstance(obj, DocPage):
        result = {}
        if obj.title:
            result["title"] = obj.title
        if obj.description:
            result["description"] = obj.description
        if obj.content:
            result["content"] = generate_detailed_keys_tree(obj.content, path)
        # if obj.metadata:
        #     result["metadata"] = generate_detailed_keys_tree(obj.metadata, path)
        if obj.subpages:
            result["subpages"] = generate_detailed_keys_tree(obj.subpages, path + ["subpages"])
        return result
    elif isinstance(obj, list):
        if not obj:
            return []
        # Show structure of first item and indicate it's a list
        result = []
        if isinstance(obj[0], str):
            return "<detail_content>"#json.dumps(obj, indent=2)
        for i, item in enumerate(obj):
            result.append(generate_detailed_keys_tree(item, path + [i]))
        return result
    elif isinstance(obj, dict):
        if not obj:
            return {}
        result = {}
        for key, value in obj.items():
            if key == "On this page":
                continue
            if isinstance(value, str):
                # Stop at string values, just indicate it's a string
                result[key] = "<detail_content>"
            elif isinstance(value, (int, float, bool)):
                # Stop at primitive values
                result[key] = f"<{type(value).__name__}>"
            elif value is None:
                result[key] = None
            else:
                # Continue traversing for complex objects
                result[key] = generate_detailed_keys_tree(value, path)
        return result
    elif isinstance(obj, str):
        return "<detail_content>"
    elif isinstance(obj, (int, float, bool)):
        return f"<{type(obj).__name__}>"
    elif obj is None:
        return None
    else:
        return f"<{type(obj).__name__}>"

def process_markdown_file(file_path: str, title_index: Dict[str, List[int]]) -> tuple[str, Dict[str, Any], list]:
    """
    Process a single markdown file and extract title, content, and index information.
    
    Args:
        file_path (str): Path to the markdown file
        title_index (Dict[str, List[int]]): Title index
    
    Returns:
        tuple: (title, content, sub_indexes) or None if file doesn't match expected format
    """
    try:
        with open(file_path, "r", encoding='utf-8') as file:
            content = file.read()
    except (UnicodeDecodeError, FileNotFoundError) as e:
        print(f"Warning: Could not read {file_path}: {e}")
        return None
    
    if not title_index:
        first_line = content.split("\n")[0]
        title = first_line.split("/")[-1].strip()

        if "-" not in title:
            return None

        try:
            index = title.split("-")[0].strip()
            sub_indexs = [int(sub_index) for sub_index in index.split(".")]
        except ValueError:
            print(f"Warning: Invalid index format in {file_path}: {title}")
            return None

        title = "-".join(title.split("-")[1:])
    
    # get title, sub_indexes from module_tree
    else:
        basename = os.path.basename(file_path)
        title = basename.replace(".md", "").replace(".mdx", "")
        if title in title_index:
            sub_indexs = title_index[title]
        else:
            print(f"Warning: Title {title} not found in title_index")
            return None

    try:
        content = json.loads((markdown_to_json.jsonify(content)))
    except (json.JSONDecodeError, Exception) as e:
        print(f"Warning: Could not parse markdown content in {file_path}: {e}")
        return None

    _title = title.replace("-", " ").lower()
    for key in content.keys():
        if _title == key.lower():
            if isinstance(content[key], dict) and len(content) == 1:
                content = content[key]
            break
    
    if isinstance(content, dict):
        if "On this page" in content:
            del content["On this page"]
        for key, value in content.items():
            if isinstance(value, dict):
                if "On this page" in value:
                    del value["On this page"]

    return title, content, sub_indexs


def parse_deepwiki(path: str, project_name: str, output_dir: str = None):
    """
    Recursively parse deepwiki documentation from markdown files and generate structured output.
    
    Args:
        path (str): Path to the directory containing markdown files (supports nested directories)
        project_name (str): Name of the project
        output_dir (str, optional): Directory to save output files. If None, saves to the input path.
    
    Returns:
        tuple: (structured_docs, detailed_keys_tree)
    """
    if output_dir is None:
        output_dir = path

    module_tree_path = os.path.join(path, "module_tree.json")
    if os.path.exists(module_tree_path):
        with open(module_tree_path, "r", encoding='utf-8') as f:
            module_tree = json.load(f)
        module_tree = {**{"overview": {}}, **module_tree}
    else:
        module_tree = {}

    # build {index: title} dict from module_tree
    title_index = {}
    def build_index_title(module_info: Dict[str, Any], indexes: List[int]):
        i = 1
        for module_name, sub_module_info in module_info.items():
            sub_indexes = indexes + [i]
            title_index[module_name] = sub_indexes
            i += 1

            if "children" in sub_module_info and sub_module_info["children"]:
                build_index_title(sub_module_info["children"], sub_indexes)
    
    try:
        normalized_path = os.path.normpath(os.path.abspath(path))
    except (ValueError, OSError) as e:
        print(f"Warning: Could not normalize path {path}: {e}")
        normalized_path = path
    
    metadata_path = normalized_path
    try:
        if os.path.isabs(normalized_path) and output_dir:
            try:
                rel_path = os.path.relpath(normalized_path, os.path.abspath(output_dir))
                if not rel_path.startswith(".."):
                    metadata_path = rel_path
            except (ValueError, OSError):
                pass
    except Exception:
        pass
    
    root_page = DocPage(
        title=project_name,
        description=f"Documentation for {project_name}",
        content={},
        metadata={"type": "root", "path": metadata_path, "source": "deepwiki"},
        subpages=[]
    )


    temp_structure = {}
    
    def find_markdown_files(root_dir: str) -> List[str]:
        """Recursively find all .md and .mdx files in directory"""
        markdown_files = []
        try:
            abs_root_dir = os.path.abspath(root_dir)
            normalized_root = os.path.normpath(abs_root_dir)
            
            for root, dirs, filenames in os.walk(normalized_root):
                for filename in filenames:
                    if filename.endswith(".md") or filename.endswith(".mdx"):
                        file_path = os.path.join(root, filename)
                        try:
                            normalized_path = os.path.normpath(os.path.abspath(file_path))
                            
                            if not normalized_path.startswith(normalized_root):
                                print(f"Warning: File path {normalized_path} is outside root directory {normalized_root}, skipping")
                                continue
                            
                            if os.path.isfile(normalized_path):
                                markdown_files.append(normalized_path)
                            else:
                                print(f"Warning: Path is not a file: {normalized_path}")
                        except (ValueError, OSError) as e:
                            print(f"Warning: Could not normalize path {file_path}: {e}")
                            continue
        except (PermissionError, FileNotFoundError) as e:
            print(f"Warning: Could not access directory {root_dir}: {e}")
        return sorted(markdown_files)
    
    files = find_markdown_files(normalized_path)
    
    if not files:
        print(f"Warning: No markdown files found in {normalized_path}")
        return root_page, {}

    if module_tree:
        for file_path in files:
            basename = os.path.basename(file_path)
            title = basename.replace(".md", "").replace(".mdx", "")
            if title not in title_index:
                module_tree[title] = {}
                
        build_index_title(module_tree, [])
        


    
    # Process markdown files
    for file_path in files:
        
        result = process_markdown_file(file_path, title_index)
        if result is None:
            continue
            
        title, content, sub_indexs = result
        
        # Build temporary structure with arbitrary depth
        def build_nested_structure(struct: dict, indexes: list, title: str, content: dict, depth: int = 0):
            """Recursively build nested structure for arbitrary depth indexing"""
            if not indexes:
                return
            
            current_index = indexes[0]
            remaining_indexes = indexes[1:]
            
            # Initialize current level if it doesn't exist
            if current_index not in struct:
                struct[current_index] = {
                    "title": None,
                    "content": {},
                    "subpages": {}
                }
            
            # If this is the final level, set the content and title
            if not remaining_indexes:
                struct[current_index]["title"] = title
                struct[current_index]["content"] = content
            else:
                # Continue building nested structure
                build_nested_structure(
                    struct[current_index]["subpages"], 
                    remaining_indexes, 
                    title, 
                    content, 
                    depth + 1
                )
        
        # Build the structure for this file
        if sub_indexs:
            build_nested_structure(temp_structure, sub_indexs, title, content)

    # Convert temporary structure to DocPage hierarchy
    def convert_temp_to_docpage(temp_data: dict) -> DocPage:
        """Convert temporary structure to DocPage"""
        raw_content = temp_data.get("content", {})
        if isinstance(raw_content, str):
            raw_content = {"text": raw_content}
        elif not isinstance(raw_content, dict):
            raw_content = {"text": str(raw_content)}
        doc_page = DocPage(
            title=temp_data.get("title") or "Untitled Section",
            description=None,
            content=raw_content,
            metadata={"source": "deepwiki"},
            subpages=[]
        )
        
        # Convert subpages
        subpages_dict = temp_data.get("subpages", {})
        for key in sorted(subpages_dict.keys()):
            subpage_data = subpages_dict[key]
            subpage = convert_temp_to_docpage(subpage_data)
            doc_page.subpages.append(subpage)
        
        return doc_page

    # Convert all top-level sections to DocPages
    for key in sorted(temp_structure.keys()):
        section_data = temp_structure[key]
        section_page = convert_temp_to_docpage(section_data)
        root_page.subpages.append(section_page)

    # Generate detailed keys tree
    detailed_keys_tree = generate_detailed_keys_tree(root_page)

    # Save outputs
    os.makedirs(output_dir, exist_ok=True)

    # save detailed_keys_tree to a json file
    with open(os.path.join(output_dir, "docs_tree.json"), "w", encoding='utf-8') as f:
        json.dump(detailed_keys_tree, f, indent=2, ensure_ascii=False)

    # save structured_docs to a json file
    try:
        structured_dict = convert_to_dict(root_page)
        with open(os.path.join(output_dir, "structured_docs.json"), "w", encoding='utf-8') as f:
            json.dump(structured_dict, f, indent=2, ensure_ascii=False)
    except (TypeError, ValueError, OSError) as e:
        print(f"Error saving structured_docs.json: {e}")
        raise
    
    return root_page, detailed_keys_tree

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=str, required=True)
    parser.add_argument("--output-dir", type=str)
    args = parser.parse_args()

    input_dir = args.input_dir
    output_dir = args.output_dir
    project_name = input_dir.split("/")[-2]
    parse_deepwiki(input_dir, project_name, output_dir)