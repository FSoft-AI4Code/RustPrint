import logging
from typing import List, Tuple
from pathlib import Path
import os

from tree_sitter import Parser, Language
import tree_sitter_rust
from rustprint.src.dependency_analyzer.models.core import Node, CallRelationship

logger = logging.getLogger(__name__)


class TreeSitterRustAnalyzer:
	def __init__(self, file_path: str, content: str, repo_path: str = None):
		self.file_path = Path(file_path)
		self.content = content
		self.repo_path = repo_path or ""
		self.nodes: List[Node] = []
		self.call_relationships: List[CallRelationship] = []
		self._analyze()

	def _get_module_path(self) -> str:
		if self.repo_path:
			try:
				rel_path = os.path.relpath(str(self.file_path), self.repo_path)
			except ValueError:
				rel_path = str(self.file_path)
		else:
			rel_path = str(self.file_path)

		if rel_path.endswith('.rs'):
			rel_path = rel_path[:-3]
		return rel_path.replace('/', '.').replace('\\', '.')

	def _get_relative_path(self) -> str:
		if self.repo_path:
			try:
				return os.path.relpath(str(self.file_path), self.repo_path)
			except ValueError:
				return str(self.file_path)
		else:
			return str(self.file_path)

	def _get_component_id(self, name: str, parent: str = None) -> str:
		module_path = self._get_module_path()
		if parent:
			return f"{module_path}.{parent}.{name}" if module_path else f"{parent}.{name}"
		return f"{module_path}.{name}" if module_path else name

	def _analyze(self):
		language_capsule = tree_sitter_rust.language()
		rust_language = Language(language_capsule)
		parser = Parser(rust_language)
		tree = parser.parse(bytes(self.content, "utf8"))
		root = tree.root_node
		lines = self.content.splitlines()

		top_level_nodes = {}

		self._extract_nodes(root, top_level_nodes, lines)
		self._extract_relationships(root, top_level_nodes)

	def _extract_nodes(self, node, top_level_nodes, lines):
		"""Recursively extract components: functions, structs, enums, traits, impl blocks, type aliases."""
		node_type = None
		node_name = None
		parent_name = None

		if node.type == "function_item":
			parent_name = self._find_containing_impl_or_trait(node)
			node_type = "method" if parent_name else "function"
			node_name = self._get_child_text(node, "identifier")

		elif node.type == "struct_item":
			node_type = "struct"
			node_name = self._get_child_text(node, "type_identifier")

		elif node.type == "enum_item":
			node_type = "enum"
			node_name = self._get_child_text(node, "type_identifier")

		elif node.type == "trait_item":
			node_type = "trait"
			node_name = self._get_child_text(node, "type_identifier")

		elif node.type == "type_item":
			node_type = "type_alias"
			node_name = self._get_child_text(node, "type_identifier")

		elif node.type == "static_item" or node.type == "const_item":
			node_type = "variable"
			node_name = self._get_child_text(node, "identifier")

		elif node.type == "mod_item":
			node_type = "module"
			node_name = self._get_child_text(node, "identifier")

		if node_type and node_name:
			component_id = self._get_component_id(node_name, parent_name)
			top_level_key = component_id if parent_name else node_name
			relative_path = self._get_relative_path()

			node_obj = Node(
				id=component_id,
				name=node_name,
				component_type=node_type,
				file_path=str(self.file_path),
				relative_path=relative_path,
				source_code="\n".join(lines[node.start_point[0]:node.end_point[0]+1]),
				start_line=node.start_point[0]+1,
				end_line=node.end_point[0]+1,
				has_docstring=self._has_doc_comment(node, lines),
				docstring=self._extract_doc_comment(node, lines),
				parameters=self._extract_parameters(node) if node_type in ("function", "method") else None,
				node_type=node_type,
				base_classes=None,
				class_name=parent_name if node_type == "method" else None,
				display_name=f"{node_type} {node_name}",
				component_id=component_id,
			)

			top_level_nodes[top_level_key] = node_obj

			if node_type in ("function", "struct", "enum", "trait"):
				self.nodes.append(node_obj)

		for child in node.children:
			self._extract_nodes(child, top_level_nodes, lines)

	def _extract_relationships(self, node, top_level_nodes):
		"""Extract call relationships, trait implementations, and type usage."""

		# Function / method calls
		if node.type == "call_expression":
			caller = self._find_containing_function(node, top_level_nodes)
			if caller:
				caller_id = self._component_id_for(caller, top_level_nodes)
				callee_name = self._extract_callee_name(node)
				if callee_name and not self._is_std_function(callee_name):
					self.call_relationships.append(CallRelationship(
						caller=caller_id,
						callee=callee_name,
						call_line=node.start_point[0]+1,
						is_resolved=False,
					))

		# Macro invocations (e.g. println!, vec!)
		elif node.type == "macro_invocation":
			caller = self._find_containing_function(node, top_level_nodes)
			if caller:
				macro_name = self._get_child_text(node, "identifier")
				if macro_name and not self._is_std_macro(macro_name):
					caller_id = self._component_id_for(caller, top_level_nodes)
					self.call_relationships.append(CallRelationship(
						caller=caller_id,
						callee=macro_name,
						call_line=node.start_point[0]+1,
						is_resolved=False,
					))

		# impl Trait for Type — link Type to Trait
		elif node.type == "impl_item":
			trait_name, type_name = self._parse_impl_item(node)
			if trait_name and type_name:
				type_id = self._get_component_id(type_name)
				self.call_relationships.append(CallRelationship(
					caller=type_id,
					callee=trait_name,
					call_line=node.start_point[0]+1,
					is_resolved=False,
				))

		# Static / const usage of known top-level names
		elif node.type == "identifier":
			var_name = node.text.decode()
			if var_name in top_level_nodes and top_level_nodes[var_name].component_type == "variable":
				caller = self._find_containing_function(node, top_level_nodes)
				if caller and caller != var_name:
					caller_id = self._component_id_for(caller, top_level_nodes)
					var_id = self._get_component_id(var_name)
					self.call_relationships.append(CallRelationship(
						caller=caller_id,
						callee=var_id,
						call_line=node.start_point[0]+1,
						is_resolved=True,
					))

		for child in node.children:
			self._extract_relationships(child, top_level_nodes)

	# ── helpers ──────────────────────────────────────────────

	def _get_child_text(self, node, child_type: str):
		"""Return decoded text of the first child matching child_type, or None."""
		for child in node.children:
			if child.type == child_type:
				return child.text.decode()
		return None

	def _find_containing_impl_or_trait(self, node):
		"""Walk up to find the impl/trait that owns this function_item."""
		current = node.parent
		while current:
			if current.type == "impl_item":
				# The type being implemented
				for child in current.children:
					if child.type == "type_identifier":
						return child.text.decode()
					if child.type == "generic_type":
						ident = self._get_child_text(child, "type_identifier")
						if ident:
							return ident
			elif current.type == "trait_item":
				return self._get_child_text(current, "type_identifier")
			current = current.parent
		return None

	def _find_containing_function(self, node, top_level_nodes):
		"""Walk up to find the function_item that contains this node. Returns function name."""
		current = node.parent
		while current:
			if current.type == "function_item":
				name = self._get_child_text(current, "identifier")
				if name:
					return name
			current = current.parent
		return None

	def _component_id_for(self, func_name: str, top_level_nodes):
		"""Get the component ID for a function name that may be in top_level_nodes."""
		# Check if it's stored under a qualified key (impl method)
		for key, node_obj in top_level_nodes.items():
			if node_obj.name == func_name:
				return node_obj.id
		return self._get_component_id(func_name)

	def _extract_callee_name(self, call_node):
		"""Extract the function/method name from a call_expression."""
		func = call_node.children[0] if call_node.children else None
		if not func:
			return None
		if func.type == "identifier":
			return func.text.decode()
		if func.type == "field_expression":
			# obj.method() — extract method name
			for child in func.children:
				if child.type == "field_identifier":
					return child.text.decode()
		if func.type == "scoped_identifier":
			# Type::method() — extract last identifier
			identifiers = [c for c in func.children if c.type in ("identifier", "type_identifier")]
			if identifiers:
				return identifiers[-1].text.decode()
		return None

	def _parse_impl_item(self, node):
		"""Parse 'impl Trait for Type' or 'impl Type'. Returns (trait_name, type_name) or (None, type_name)."""
		has_for = any(c.type == "for" or (c.type == "identifier" and c.text.decode() == "for") or getattr(c, 'text', b'') == b'for' for c in node.children)
		# Simpler: look at children sequence
		type_ids = [c for c in node.children if c.type == "type_identifier"]
		generic_types = [c for c in node.children if c.type == "generic_type"]

		# Collect all type names in order
		all_types = []
		for child in node.children:
			if child.type == "type_identifier":
				all_types.append(child.text.decode())
			elif child.type == "generic_type":
				ident = self._get_child_text(child, "type_identifier")
				if ident:
					all_types.append(ident)

		# Check if 'for' keyword is present (impl Trait for Type)
		children_texts = [c.text.decode() if hasattr(c, 'text') else '' for c in node.children]
		if 'for' in children_texts and len(all_types) >= 2:
			return all_types[0], all_types[1]  # (trait, type)
		elif len(all_types) >= 1:
			return None, all_types[0]  # inherent impl
		return None, None

	def _has_doc_comment(self, node, lines) -> bool:
		"""Check if the node is preceded by a /// or //! doc comment."""
		line_idx = node.start_point[0] - 1
		while line_idx >= 0:
			stripped = lines[line_idx].strip()
			if stripped.startswith("///") or stripped.startswith("//!"):
				return True
			if stripped == "" or stripped.startswith("#["):
				line_idx -= 1
				continue
			break
		return False

	def _extract_doc_comment(self, node, lines) -> str:
		"""Extract consecutive /// or //! doc comment lines before a node."""
		doc_lines = []
		line_idx = node.start_point[0] - 1
		while line_idx >= 0:
			stripped = lines[line_idx].strip()
			if stripped.startswith("///"):
				doc_lines.append(stripped[3:].strip())
				line_idx -= 1
			elif stripped.startswith("//!"):
				doc_lines.append(stripped[3:].strip())
				line_idx -= 1
			elif stripped == "" or stripped.startswith("#["):
				line_idx -= 1
			else:
				break
		doc_lines.reverse()
		return "\n".join(doc_lines)

	def _extract_parameters(self, func_node) -> List[str]:
		"""Extract parameter names from a function_item's parameter list."""
		params = []
		for child in func_node.children:
			if child.type == "parameters":
				for param in child.children:
					if param.type == "parameter":
						ident = self._get_child_text(param, "identifier")
						if ident:
							params.append(ident)
					elif param.type == "self_parameter":
						params.append("self")
		return params

	def _is_std_function(self, name: str) -> bool:
		"""Filter out common Rust standard library functions."""
		std_functions = {
			"drop", "clone", "into", "from", "default",
			"unwrap", "expect", "ok", "err", "map", "and_then",
			"iter", "collect", "push", "pop", "len", "is_empty",
			"to_string", "as_ref", "as_mut", "to_owned",
			"write", "read", "flush",
		}
		return name in std_functions

	def _is_std_macro(self, name: str) -> bool:
		"""Filter out common Rust standard library macros."""
		std_macros = {
			"println", "print", "eprintln", "eprint",
			"format", "write", "writeln",
			"vec", "dbg", "todo", "unimplemented", "unreachable",
			"assert", "assert_eq", "assert_ne", "debug_assert",
			"panic", "cfg", "env", "include", "include_str", "include_bytes",
			"concat", "stringify", "line", "column", "file", "module_path",
		}
		return name in std_macros


def analyze_rust_file(file_path: str, content: str, repo_path: str = None) -> Tuple[List[Node], List[CallRelationship]]:
	analyzer = TreeSitterRustAnalyzer(file_path, content, repo_path)
	return analyzer.nodes, analyzer.call_relationships
