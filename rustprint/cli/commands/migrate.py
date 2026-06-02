import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import click

from rustprint.cli import pipeline
from rustprint.cli.pipeline import PipelineCreds
from rustprint.cli.config_manager import ConfigManager, expand_path, resolve_project_dir
from rustprint.cli.git_manager import GitManager
from rustprint.cli.help import ColorCommand
from rustprint.cli.utils.errors import ConfigurationError, RepositoryError, handle_error


ROOT_DIR = Path(__file__).resolve().parents[3]
BENCHMARK_DIR = ROOT_DIR / "codewikibench"


def _has_c_files(path: Path) -> bool:
    return any(path.rglob("*.c")) or any(path.rglob("*.h"))


def _has_source_tests(path: Path) -> bool:
    return (path / "tests").is_dir() or (path / "test").is_dir()


def _prepare_single_repo_folder(cache_project_dir: Path, source_path: Path, project_name: str) -> Path:
    repos_dir = cache_project_dir / "source_repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    link_path = repos_dir / project_name

    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink() or link_path.is_file():
            link_path.unlink()
        else:
            shutil.rmtree(link_path)

    try:
        link_path.symlink_to(source_path, target_is_directory=True)
    except OSError:
        shutil.copytree(source_path, link_path)
    return repos_dir


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        if not click.confirm(f"Final output already exists at {dst}. Overwrite?", default=False):
            raise ConfigurationError("Migration cancelled before overwriting final output.")
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


def _copy_best_solution(cache_project_dir: Path, project_name: str) -> Path:
    src = cache_project_dir / "translated_repos" / "version_0" / project_name
    dst = cache_project_dir / "translated_repos" / "best_solution" / project_name
    if not src.is_dir():
        raise ConfigurationError(f"Translated version_0 repo not found: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return dst


def _find_final_execution_repo(cache_project_dir: Path, project_name: str, max_round: int) -> Path | None:
    base = cache_project_dir / "translated_repos" / "execution-aware"
    for version in range(max_round, -1, -1):
        candidate = base / f"version_{version}" / project_name
        if (candidate / "Cargo.toml").is_file():
            return candidate
    return None


def _materialize_final_repo(cache_project_dir: Path, output_base_dir: Path, project_name: str, execution_rounds: int, used_execution: bool) -> Path:
    if used_execution:
        final_src = _find_final_execution_repo(cache_project_dir, project_name, execution_rounds)
        if final_src is None:
            final_src = cache_project_dir / "translated_repos" / "best_solution" / project_name
    else:
        final_src = cache_project_dir / "translated_repos" / "best_solution" / project_name

    if not final_src.is_dir():
        raise ConfigurationError(f"Final translated repository not found: {final_src}")

    final_dst = resolve_project_dir(output_base_dir, project_name)
    _copy_tree(final_src, final_dst)
    return final_dst


def _configure_optional_branch(source_path: Path, git_config: dict) -> None:
    if not bool(git_config.get("branch_enabled", False)):
        return

    branch_name = str(git_config.get("branch_name") or "rustprint-migration")
    git_manager = GitManager(source_path)
    is_clean, status = git_manager.check_clean_working_directory()
    if not is_clean:
        raise RepositoryError(
            "Working directory has uncommitted changes.\n\n"
            f"{status}\n\n"
            "Commit or stash changes before running branch-enabled migration."
        )
    git_manager.create_or_checkout_branch(branch_name)
    click.secho(f"Using Git branch: {branch_name}", fg="green")


def _build_pipeline_env(project_config: dict, cache_project_dir: Path, source_repos_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    model = str(project_config["model"]["name"])
    api_key = str(project_config["api"].get("api_key") or "")
    base_url = str(project_config["api"].get("base_url") or "")

    env.update(
        {
            "PYTHONPATH": f"{ROOT_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
            "RUSTPRINT_LIVE_LOG": "1",
            "RUSTPRINT_OUTPUT_ROOT": str(cache_project_dir),
            "RUSTPRINT_REPO_DATA_DIR": str(source_repos_dir),
            "RUSTPRINT_BENCHMARK_DIR": str(ROOT_DIR / "codewikibench"),
            "RUSTPRINT_CLI": f"{sys.executable} -m rustprint",
            "MODEL": model,
            "MAIN_MODEL": model,
            "CLUSTER_MODEL": model,
            "LLM_API_KEY": api_key,
            "LLM_BASE_URL": base_url,
            "API_KEY": api_key,
            "BASE_URL": base_url,
            "OPENAI_API_KEY": api_key,
            "OPENAI_BASE_URL": base_url,
            "FIREWORKS_API_KEY": api_key,
            "FIREWORKS_BASE_URL": base_url,
        }
    )
    return env


def _start_web_server(host: str, port: int, runner):
    from rustprint.web.server import create_server

    try:
        httpd = create_server(host, port, runner=runner)
    except OSError:
        httpd = create_server(host, 0, runner=runner)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


@click.command(name="migrate", cls=ColorCommand)
@click.option("--host", default="127.0.0.1", show_default=True, help="Host for the live web tracker.")
@click.option("--port", default=5000, show_default=True, type=int, help="Preferred port for the live web tracker.")
@click.option("--no-web", "no_web", is_flag=True, help="Disable the live web tracker.")
@click.option("-f", "--force", "force", is_flag=True, help="Remove the cache folder for this repo and run from scratch.")
def migrate_command(host: str, port: int, no_web: bool, force: bool):
    """Run the end-to-end RustPrint migration pipeline."""
    from rustprint.cli.utils.live import PhaseStream, StdoutRedirector, banner
    from rustprint.web.runner import MigrationRunner

    real_stdout = sys.stdout
    real_stderr = sys.stderr
    httpd = None
    try:
        manager = ConfigManager()
        project_config = manager.load_project_config()

        project_name = manager.project_name
        source_path = expand_path(str(project_config["source"]["path"]))
        if not source_path.is_dir():
            raise ConfigurationError(f"Configured source.path does not exist: {source_path}")
        if source_path.name != project_name:
            raise ConfigurationError(
                "Current directory does not match the configured project.\n\n"
                f"Current project: {project_name}\n"
                f"Configured source: {source_path}\n\n"
                "Run rustprint migrate from the configured source repository directory."
            )
        if not _has_c_files(source_path):
            raise RepositoryError(f"No C source or header files found in {source_path}")

        api_key = str(project_config["api"].get("api_key") or "")
        base_url = str(project_config["api"].get("base_url") or "")
        if not api_key or not base_url:
            raise ConfigurationError("api.api_key and api.base_url must be configured before migration.")

        cache_project_dir = resolve_project_dir(expand_path(str(project_config["output"]["cache"])), project_name)
        output_base_dir = expand_path(str(project_config["output"]["base_dir"]))

        runner = MigrationRunner()
        url = None
        if not no_web:
            try:
                httpd, actual_port = _start_web_server(host, port, runner)
                display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
                url = f"http://{display_host}:{actual_port}"
            except OSError as exc:
                click.secho(f"Could not start web tracker: {exc}", fg="yellow", err=True)

        click.echo(banner())
        label = lambda t: click.style(t, fg="bright_black")
        click.echo(label("Project:  ") + click.style(project_name, bold=True))
        click.echo(label("Source:   ") + str(source_path))
        click.echo(label("Cache:    ") + str(cache_project_dir))
        click.echo(label("Final:    ") + str(resolve_project_dir(output_base_dir, project_name)))
        if url:
            click.echo(label("Track:    ") + click.style(url, fg="bright_cyan", bold=True))
        click.echo()

        backend_logger = logging.getLogger("rustprint")
        backend_logger.setLevel(logging.INFO)
        backend_logger.propagate = False
        logging.getLogger("httpx").setLevel(logging.WARNING)
        pipeline._common._logging_configured = True

        q = runner.bus.subscribe()
        ok, message = runner.start(project_config, force=force)
        if not ok:
            runner.bus.unsubscribe(q)
            raise RepositoryError(message)

        console = PhaseStream(stream=real_stdout)
        icons = {"done": ("✓", "green"), "skipped": ("⊘", "yellow"), "error": ("✗", "red")}

        if url:
            console.line(
                click.style("Showing main phases below — open the tracker above for live sub-steps and logs.", fg="bright_black")
            )
            console.line("")

        seen: dict[str, tuple[str, str]] = {}
        active_id: list[str | None] = [None]

        def show_state(stages):
            for s in stages:
                sid = s.get("id")
                status = s.get("status", "pending")
                label = s.get("label", sid)
                detail = s.get("detail") or ""
                if seen.get(sid) == (status, detail):
                    continue
                seen[sid] = (status, detail)
                if status == "active":
                    if active_id[0] != sid:
                        console.set_active(label, detail)
                        active_id[0] = sid
                    else:
                        console.update_detail(detail)
                elif status in icons:
                    icon, color = icons[status]
                    if active_id[0] == sid:
                        console.finish(icon, color, label)
                        active_id[0] = None
                    else:
                        console.line(click.style(icon, fg=color, bold=True) + " " + click.style(label))

        redirector = StdoutRedirector(
            lambda line: runner.bus.publish({"type": "log", "level": "info", "message": line, "ts": time.time()})
        )
        sys.stdout = redirector
        sys.stderr = redirector

        stop_ticker = threading.Event()

        def _animate():
            while not stop_ticker.wait(0.12):
                console.animate()

        ticker = None
        if console.tty:
            ticker = threading.Thread(target=_animate, daemon=True)
            ticker.start()

        error = None
        final_path = None
        try:
            done = False
            while not done:
                event = q.get()
                etype = event.get("type")
                if etype == "state":
                    show_state(event.get("stages", []))
                elif etype == "error":
                    error = event.get("message") or error
                elif etype == "done":
                    error = event.get("error") or error
                    final_path = event.get("final_path")
                    done = True
        finally:
            stop_ticker.set()
            if ticker is not None:
                ticker.join(timeout=1)
            runner.bus.unsubscribe(q)
            sys.stdout = real_stdout
            sys.stderr = real_stderr
            console.close()

        if error and error != "stopped":
            click.secho(f"\nMigration failed: {error}", fg="red", err=True)
            if httpd is not None:
                httpd.shutdown()
            sys.exit(1)
        if error == "stopped":
            click.secho("\nMigration stopped.", fg="yellow")
            if httpd is not None:
                httpd.shutdown()
            return

        click.secho(f"\nMigration completed. Final Rust repo: {final_path}", fg="green", bold=True)
        if httpd is not None and url:
            click.echo("Live tracker still running at " + click.style(url, fg="cyan", bold=True) + " — press Ctrl-C to exit.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                click.echo()
            httpd.shutdown()
    except (ConfigurationError, RepositoryError) as e:
        sys.stdout = real_stdout
        sys.stderr = real_stderr
        if httpd is not None:
            httpd.shutdown()
        click.secho(f"\nError: {e.message}", fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as e:
        sys.stdout = real_stdout
        sys.stderr = real_stderr
        if httpd is not None:
            httpd.shutdown()
        sys.exit(handle_error(e))
