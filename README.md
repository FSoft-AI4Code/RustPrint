# RustPrint

RustPrint is a multi-agent C-to-Rust migration tool. It documents your C source, translates it into an idiomatic Rust workspace, then iteratively refines the result against documentation rubrics and translated tests.

## Install

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -e .

# Rust toolchain (needed for cargo check / nextest during refinement)
curl https://sh.rustup.rs -sSf | sh -s -- -y
source "$HOME/.cargo/env"
cargo install cargo-nextest
```

## Quick start

```bash
rustprint init /path/to/c-repo     # create the project config (interactive)
cd /path/to/c-repo
rustprint migrate                  # run the migration + live web tracker
```

The translated Rust workspace lands in `<output.base_dir>/<repo-name>/`.

## `rustprint migrate`

Run it from inside the configured C repository. It executes every stage in-process and, by default, serves a live web tracker so you can watch progress in a browser.

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Host for the live web tracker. |
| `--port` | `5000` | Preferred port for the tracker (auto-picks a free one if taken). |
| `--no-web` | off | Run headless, terminal output only. |
| `-f`, `--force`, `--from-scratch` | off | Wipe this repo's cache and start over. |

### Stages

1. **C Documentation** — parse C into a dependency graph, cluster into modules, generate per-module docs.
2. **Translation** — three agents (Planner → Skeleton → Synthesis) produce the initial Rust workspace.
3. **Requirement Refinement** — score Rust docs against the C rubrics and fix failing requirements (per round). Best-scoring version becomes `best_solution`.
4. **Test Translation** — translate the C test suite to Rust.
5. **Execution Refinement** — run `cargo nextest`, then apply LLM fixes to failing tests (per round).

## Tracking in the browser

`rustprint migrate` prints a tracker URL (e.g. `http://127.0.0.1:5000`). Open it to follow each stage and the streaming log in real time; sub-steps and refinement rounds expand as they run. Folder/file icons open the artifacts in your editor (Cursor or VS Code).

To watch a run on a remote host, forward the port over SSH:

```bash
ssh -L 5000:localhost:5000 <user>@<remote-host>
# then open http://localhost:5000 locally
```

You can also launch the tracker UI on its own to configure and start a run from the browser:

```bash
rustprint web --host 0.0.0.0 --port 5000
```

## `rustprint config`

Each project has its own TOML config at `~/.config/rustprint/<repo-name>`, created by `rustprint init`.

```bash
rustprint config show                              # print the current config
rustprint config set model.name gpt-5.4            # set one dotted key
rustprint config set execution_refinement.rounds 3
```

```toml
[model]
name = "gpt-5.4"
provider = "openai"

[source]
path = "/path/to/c-repo"

[api]
api_key = "sk-..."
base_url = "https://api.openai.com/v1"

[output]
base_dir = "~/rustprint-output"      # final repo → <base_dir>/<repo-name>/
cache = "~/.cache/rustprint"         # artifacts → <cache>/<repo-name>/

[git]
branch_enabled = false
branch_name = "rustprint-migration"
commit = false

[requirement_refinement]
enabled = true
rounds = 5

[execution_refinement]
enabled = true
rounds = 5
translate_tests = true
```

`api.api_key` and `api.base_url` may instead come from the `LLM_API_KEY` / `LLM_BASE_URL` environment variables, which take precedence over the config file.

## Commands

| Command | Description |
|---|---|
| `rustprint init [SOURCE_REPO]` | Create a project config (use `--force` to overwrite). |
| `rustprint migrate` | Run the end-to-end migration + live tracker. |
| `rustprint config show` | Print the current project's config. |
| `rustprint config set <key> <value>` | Set one dotted config key. |
| `rustprint web` | Launch the tracker UI standalone. |
| `rustprint version` | Show version information. |
