import json
import asyncio
import argparse
import os
from pathlib import Path

from pydantic_ai import Agent

from utils import get_llm, run_llm_natively
from rustprint.src.agent_retry import run_agent_with_retry
import config
from tools import AgentDeps, docs_navigator_tool
from rubrics_generator.visualize_rubrics import visualize_rubrics

def parse_args():
    parser = argparse.ArgumentParser(description="Generate hierarchical rubrics from documentation")
    parser.add_argument("--repo-name", required=True, help="Name of the repository")
    parser.add_argument("--data-dir", help="Custom data directory path (default: uses config)")
    parser.add_argument("--use-tools", action="store_true", help="Enable tools for document navigation")
    parser.add_argument("--model", help="Model to use (default: claude-3-5-haiku-20241022 for anthropic, deepseek-r1-0528 for fireworks, gemini-2.0-flash for google)")

    return parser.parse_args()



# --- Agent ---
SYSTEM_PROMPT = """
You are a helpful assistant tasked with analyzing the official documentation of a software repository. You will be given a documentation tree and access to individual documentation files. The documentation outlines the core features and purpose of the repository, though some sections may contain redundant or non-essential information — ignore these.

<REQUIREMENTS>
Your goal is to construct a **hierarchical rubrics** of the repository focusing on **FEATURES and FUNCTIONALITY**, not language-specific implementation details. This rubrics should:

- Start from abstract, high-level rubrics and progressively drill down into more specific subrubrics.
- Cover all major functionalities and architectural constructs.
- Focus on **WHAT the system does** and **WHAT features it provides**, not HOW it's implemented in a specific language.
- Be structured to help users clearly understand how the repository is organized and how its components systematically work together.
- Evaluate features and capabilities, ensuring the rubric is language-agnostic and applicable across different implementations.

Each rubric must include:
- A **descriptive name** focusing on functionality/features
- A **clear explanation** of its purpose (what feature/capability it represents)
- A **weight** representing its importance:
  - **3**: Essential
  - **2**: Important but not essential
  - **1**: Supportive or minor
- A list of **reference paths** to the documentation that supports the rubric (only required for **leaf rubrics**).

<LANGUAGE-AGNOSTIC FOCUS>
IMPORTANT: This rubric will be used to evaluate documentation across different languages (e.g., C and Rust).
- Focus on **FEATURES, CAPABILITIES, and FUNCTIONALITY** rather than language-specific syntax or implementation details
- Emphasize **WHAT** the system does, not language-specific HOW
- Ensure rubrics can be fairly evaluated regardless of the implementation language

Use the following JSON format to represent the rubrics:
```json
[
  {
    "name": "Rubric 1",
    "description": "High-level purpose of Rubric 1",
    "reference": [],
    "weight": 3,
    "children": [
      {
        "name": "Rubric 1.1",
        "description": "Specific functionality under Rubric 1",
        "reference": [],
        "weight": 2,
        "children": [
          {
            "name": "Rubric 1.1.1",
            "description": "Leaf-level functionality",
            "weight": 3,
            "reference": ["ref_path_1", "ref_path_2"]
          }
        ]
      },
      {
        "name": "Rubric 1.2",
        "description": "Another function under Rubric 1",
        "weight": 1,
        "reference": ["ref_path_3"]
      }
    ]
  },
  {
    "name": "Rubric 2",
    "description": "High-level purpose of Rubric 2",
    "weight": 2,
    "reference": [],
    "children": [
      {
        "name": "Rubric 2.1",
        "description": "Functionality under Rubric 2",
        "weight": 2,
        "reference": ["ref_path_4"]
      }
    ]
  }
]
```

</REQUIREMENTS>

<GUIDELINES>
- Prioritize accessing documentation files that are **critical for understanding** the system's structure and behavior.
- Build the rubrics **iteratively**, updating and refining it as more information is gathered.
</GUIDELINES>
""".strip()

SYSTEM_PROMPT_WO_TOOLS = """
You are a skilled technical assistant assigned to analyze the official documentation of a software repository.
You will be provided with a documentation tree written primarily in a **HOW-TO-USE** format, which focuses on how to operate the repository's features and tools.
Your task is to **reverse-engineer and reconstruct the internal structure and logic of the system** by transforming this HOW-TO-USE information into a **HOW-DOES-IT-WORK** perspective.

# OBJECTIVE
Develop a **hierarchical rubric** that captures the underlying architecture and working principles of the repository. This rubric should reflect **what the system does and how its parts interact**, abstracting away from usage instructions into architectural insight.

<LANGUAGE-AGNOSTIC FOCUS>
IMPORTANT: This rubric will be used to evaluate documentation across different languages (e.g., C and Rust).
- Focus on **FEATURES, CAPABILITIES, and FUNCTIONALITY** rather than language-specific syntax or implementation details
- Emphasize **WHAT** the system does, not language-specific HOW
- Ensure rubrics can be fairly evaluated regardless of the implementation language

# DELIVERABLE FORMAT
Return the rubrics in the following **nested JSON format**, where:
- Each rubric item includes a `"requirements"` field summarizing the system concept or functionality.
- Each item is assigned a `"weight"` to indicate its importance:
  - **3** = Essential to the system's core functionality
  - **2** = Important but not core
  - **1** = Minor or supporting functionality
- Items can recursively contain `"sub_tasks"` that break down more specific elements.

```json
[
  {
    "requirements": "Top-level concept or component",
    "weight": 3,
    "sub_tasks": [
      {
        "requirements": "More specific concept or subcomponent",
        "weight": 2,
        "sub_tasks": [
          {
            "requirements": "Detailed technical element or behavior",
            "weight": 3,
            "sub_tasks": [
              {
                "requirements": "Leaf-level functionality",
                "weight": 3
              },
              {
                "requirements": "More specific concept or subcomponent",
                "weight": 3,
                "sub_tasks": [
                  {
                    "requirements": "Leaf-level functionality",
                    "weight": 3,
                    "sub_tasks": [...] # dive deeper into the functionality
                  }
                ]
              }
            ]
          }
        ]
      },
      {
        "requirements": "Alternative aspect or feature",
        "weight": 1
      }
    ]
  }
]
```

# REQUIREMENTS
- Begin with **abstract, high-level components**, then drill down to concrete sub-elements.
- Structure the rubric to support **deep understanding** of the system's architecture and internal logic.
- If needed, refine the rubric **iteratively** as more parts of the documentation are reviewed.

# NOTES
- Be analytical: DO NOT mimic the documentation structure. Instead, distill and reframe it.
- Treat the documentation as evidence from which you infer **the design intent and system structure**.
""".strip()

# --- Run ---
async def run(args):
    # Setup paths automatically from repo name or use custom data dir
    if args.data_dir:
        # Use custom data directory
        base_path = args.data_dir
        # Check if docs_tree.json is at root or in 'original' subfolder
        if os.path.exists(os.path.join(base_path, "docs_tree.json")):
            docs_path = base_path
            docs_tree_path = os.path.join(base_path, "docs_tree.json")
        elif os.path.exists(os.path.join(base_path, "original", "docs_tree.json")):
            docs_path = os.path.join(base_path, "original")
            docs_tree_path = os.path.join(docs_path, "docs_tree.json")
        else:
            print(f"Error: docs_tree.json not found in {base_path}")
            return
        output_dir = os.path.join(base_path, "rubrics")
    else:
        # Use default config-based paths
        base_path = config.get_data_path(args.repo_name)
        docs_path = os.path.join(base_path, "original")
        docs_tree_path = os.path.join(docs_path, "docs_tree.json")
        output_dir = os.path.join(base_path, "rubrics")
    
    sanitized_model = args.model.replace("/", "_") if args.model else "default"

    #check if output file already exists
    if os.path.exists(os.path.join(output_dir, f"{sanitized_model}.json")):
        print(f"Rubrics already generated for {args.model}")
        return
    
    # Create output directory if it doesn't exist
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    module_tree_path = os.path.join(docs_path, "module_tree.json")
    use_structured_docs = False
    
    md_files = [f for f in os.listdir(docs_path) if f.endswith('.md')] if os.path.exists(docs_path) else []
    
    if os.path.exists(module_tree_path):
        with open(module_tree_path, "r") as f:
            module_tree = json.load(f)
            if not module_tree or len(module_tree) == 0:
                use_structured_docs = True
                print("Module tree is empty, using structured_docs.json")
    else:
        use_structured_docs = True
        print("Module tree not found, using structured_docs.json")
    
    if len(md_files) == 1:
        use_structured_docs = True
        print(f"Only one markdown file found ({md_files[0]}), using structured_docs.json")
    
    if use_structured_docs:
        structured_docs_path = os.path.join(docs_path, "structured_docs.json")
        if not os.path.exists(structured_docs_path):
            print(f"Error: structured_docs.json not found at {structured_docs_path}")
            return
        
        with open(structured_docs_path, "r") as f:
            structured_docs = json.load(f)
        
        prompt = f"""
Given the structured documentation:
\"\"\"
{json.dumps(structured_docs, indent=2)}
\"\"\"

Analyze the documentation and construct a hierarchical rubric focusing on FEATURES and FUNCTIONALITY.
Focus on what the system does and what capabilities it provides, not language-specific implementation details.
""".strip()
        
        print("Using structured_docs.json - running with prompt-only mode (no tools)")
        system_prompt = SYSTEM_PROMPT_WO_TOOLS
        
        final_output = await run_llm_natively(
            args.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        )
    else:
        with open(docs_tree_path, "r") as f:
            docs_tree = json.load(f)
        
        prompt = f"""
Given the docs tree:
\"\"\"
{json.dumps(docs_tree, indent=2)}
\"\"\"
""".strip()
        
        if args.use_tools:
            tools = [docs_navigator_tool]
            system_prompt = SYSTEM_PROMPT
        else:
            tools = []
            system_prompt = SYSTEM_PROMPT_WO_TOOLS
        
        agent = Agent(
            model=get_llm(args.model),
            deps_type=AgentDeps,
            system_prompt=system_prompt,
            tools=tools,
        )

        deps = AgentDeps(docs_path)
        result = await run_agent_with_retry(agent, prompt, deps=deps)
        final_output = result.output
    
    # Parse and save rubrics
    try:
        # Extract JSON from the final output
        json_start = final_output.find('[')
        json_end = final_output.rfind(']') + 1
        
        if json_start != -1 and json_end > json_start:
            rubrics_json = final_output[json_start:json_end]
            rubrics = json.loads(rubrics_json)
            
            # Save rubrics to file
            rubrics_file = os.path.join(output_dir, f"{sanitized_model}.json")
            with open(rubrics_file, "w") as f:
                json.dump(rubrics, f, indent=2)
            
            print(f"Rubrics saved to: {rubrics_file}")
            # visualize rubrics
            visualize_rubrics(rubrics_file)
        else:
            print("No valid JSON rubrics found in output")
            # Save raw output for debugging
            raw_output_file = os.path.join(output_dir, f"{sanitized_model}_raw_output.txt")
            with open(raw_output_file, "w") as f:
                f.write(final_output)
            print(f"Raw output saved to: {raw_output_file}")
            
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        # Save raw output for debugging
        raw_output_file = os.path.join(output_dir, f"{sanitized_model}_raw_output.txt")
        with open(raw_output_file, "w") as f:
            f.write(final_output)
        print(f"Raw output saved to: {raw_output_file}")

if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args))


