from dataclasses import dataclass
import argparse
import os
import sys
from dotenv import load_dotenv
load_dotenv()

OUTPUT_BASE_DIR = 'output'
TEMP_DIR = 'temp'
DEPENDENCY_GRAPHS_DIR = 'dependency_graphs'
TOP_FILES_DIR = 'top_files'
DOCS_DIR = 'docs'
FIRST_MODULE_TREE_FILENAME = 'first_module_tree.json'
MODULE_TREE_FILENAME = 'module_tree.json'
OVERVIEW_FILENAME = 'overview.md'
MAX_DEPTH = 2
MAX_TOKEN_PER_MODULE = 30000
MAX_TOKEN_PER_LEAF_MODULE = 10000

_CLI_CONTEXT = False

def set_cli_context(enabled: bool = True):
    global _CLI_CONTEXT
    _CLI_CONTEXT = enabled

def is_cli_context() -> bool:
    return _CLI_CONTEXT

MAIN_MODEL = os.getenv('MAIN_MODEL', '')
CLUSTER_MODEL = os.getenv('CLUSTER_MODEL', MAIN_MODEL)
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1/')
LLM_API_KEY = os.getenv('LLM_API_KEY', 'sk-proj-xxx')

@dataclass
class Config:
    repo_path: str
    output_dir: str
    dependency_graph_dir: str
    docs_dir: str
    max_depth: int
    llm_base_url: str
    llm_api_key: str
    main_model: str
    cluster_model: str
    requirement_refine_iterations: int = 5
    max_tool_calls_per_test: int = 50
    translate_only: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'Config':
        repo_name = os.path.basename(os.path.normpath(args.repo_path))
        sanitized_repo_name = ''.join(c if c.isalnum() else '_' for c in repo_name)
        base_output_dir = os.path.join(OUTPUT_BASE_DIR, TEMP_DIR)

        return cls(
            repo_path=args.repo_path,
            output_dir=base_output_dir,
            dependency_graph_dir=os.path.join(base_output_dir, DEPENDENCY_GRAPHS_DIR),
            docs_dir=os.path.join(OUTPUT_BASE_DIR, DOCS_DIR, f"{sanitized_repo_name}"),
            max_depth=MAX_DEPTH,
            llm_base_url=LLM_BASE_URL,
            llm_api_key=LLM_API_KEY,
            main_model=MAIN_MODEL,
            cluster_model=CLUSTER_MODEL,
        )

    @classmethod
    def from_cli(
        cls,
        repo_path: str,
        output_dir: str,
        llm_base_url: str,
        llm_api_key: str,
        main_model: str,
        cluster_model: str,
        docs_dir: str = None,
        requirement_refine_iterations: int = 5,
        max_tool_calls_per_test: int = 50,
        translate_only: bool = False,
    ) -> 'Config':
        repo_name = os.path.basename(os.path.normpath(repo_path))
        sanitized_repo_name = ''.join(c if c.isalnum() else '_' for c in repo_name)
        base_output_dir = os.path.join(output_dir, "temp")

        if docs_dir is None:
            docs_dir = os.path.join(output_dir, sanitized_repo_name, DOCS_DIR)

        return cls(
            repo_path=repo_path,
            output_dir=base_output_dir,
            dependency_graph_dir=os.path.join(base_output_dir, DEPENDENCY_GRAPHS_DIR),
            docs_dir=docs_dir,
            max_depth=MAX_DEPTH,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            main_model=main_model,
            cluster_model=cluster_model,
            requirement_refine_iterations=requirement_refine_iterations,
            max_tool_calls_per_test=max_tool_calls_per_test,
            translate_only=translate_only,
        )

    @classmethod
    def for_llm_only(
        cls,
        llm_base_url: str,
        llm_api_key: str,
        model: str,
        requirement_refine_iterations: int = 5,
        max_tool_calls_per_test: int = 50,
        translate_only: bool = False,
    ) -> 'Config':
        return cls(
            repo_path='',
            output_dir='',
            dependency_graph_dir='',
            docs_dir='',
            max_depth=MAX_DEPTH,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            main_model=model,
            cluster_model=model,
            requirement_refine_iterations=requirement_refine_iterations,
            max_tool_calls_per_test=max_tool_calls_per_test,
            translate_only=translate_only,
        )
