from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from rustprint.src.dependency_analyzer.models.core import Node
from rustprint.src.config import Config

@dataclass
class RustPrintDeps:
    absolute_docs_path: str
    absolute_repo_path: str
    registry: dict
    components: dict[str, Node]
    path_to_current_module: list[str]
    current_module_name: str
    module_tree: dict[str, any]
    max_depth: int
    current_depth: int
    config: Config

@dataclass
class C2RustDeps:
    absolute_c_repo_path: str
    absolute_rust_output_path: str
    absolute_docs_path: str
    registry: dict
    components: Dict[str, Any]
    translation_plan: dict
    current_module: str
    completed_steps: List[str]
    config: Config
    cargo_check_attempts: int = 0  # incremented on each cargo_check failure; reset on success
    # Crate root for current feature (from module_tree). Top-level: path to that crate; child: path to parent crate.
    current_crate_root: Optional[str] = None
    # When True, repo is single crate at root (no subfolders); cargo_check should always use workspace root.
    single_crate_at_root: bool = False

@dataclass
class SketchDocDeps:
    current_module_name: str
    rust_workspace_path: str
    sketch_docs_output_path: str


@dataclass
class RefinementDeps:
    """Dependencies for refinement agent - similar to SketchDocDeps but for code refinement."""
    rust_workspace_path: str  # Path to Rust code repository being refined
    sketch_docs_output_path: str  # Path to sketch documentation (for reference)
    current_module_name: str  # Current module/repo name
    requirement_path: List[str]  # Hierarchy path to current requirement
    current_requirement: Dict[str, Any]  # The requirement being addressed
    config: Config

@dataclass
class InputGenerationDeps:
    c_repo_path: str
    c_docs_path: str
    dependency_graph_path: str
    component_id: str
    component_info: Dict[str, Any]
    config: Config


@dataclass
class TestTransDeps:
    absolute_c_repo_path: str
    absolute_rust_output_path: str
    registry: Dict[str, Any]
    config: Config
    cargo_check_attempts: int = 0  # incremented on each cargo_check failure; reset on success
    cargo_test_attempts: int = 0  # incremented on each cargo_test_no_run failure; reset on success


@dataclass
class ExecutionRefinementDeps:
    rust_workspace_path: str
    current_module_name: str
    current_test_name: str
    current_stdout: str
    config: Config

