import sys
from pathlib import Path

import click

from rustprint import __version__
from rustprint.cli.config_manager import ConfigManager
from rustprint.cli.help import ColorCommand
from rustprint.cli.utils.errors import ConfigurationError, handle_error


def _banner() -> None:
    click.echo()
    click.echo(
        "  "
        + click.style("👋 RustPrint", fg="bright_magenta", bold=True)
        + " "
        + click.style(f"v{__version__}", fg="bright_black")
    )
    click.secho("  Interactive setup for C → Rust migration", fg="bright_black")
    click.echo()


def _section(title: str) -> None:
    click.echo()
    click.secho(f"── {title}", fg="yellow", bold=True)


def _ask(text: str, **kwargs):
    return click.prompt(click.style(text, fg="cyan"), **kwargs)


def _confirm(text: str, **kwargs) -> bool:
    return click.confirm(click.style(text, fg="cyan"), **kwargs)


@click.command(name="init", cls=ColorCommand)
@click.argument("source_repo", required=False, type=click.Path(file_okay=False, dir_okay=True))
@click.option("--yes", "overwrite_yes", is_flag=True, help="Overwrite an existing config without asking.")
@click.option("--force", "overwrite_force", is_flag=True, help="Overwrite an existing config without asking.")
def init_command(source_repo: str | None, overwrite_yes: bool, overwrite_force: bool):
    """Create a project-specific RustPrint config."""
    try:
        _banner()

        _section("Source")
        initial_source = Path(source_repo or Path.cwd()).expanduser().resolve()
        source_input = _ask("Source C repository", default=str(initial_source), show_default=True)
        source_path = Path(source_input).expanduser().resolve()
        if not source_path.is_dir():
            raise ConfigurationError(f"Source repository does not exist: {source_path}")

        manager = ConfigManager(project_path=source_path)
        overwrite = overwrite_yes or overwrite_force

        if manager.config_exists() and not overwrite:
            prompt = click.style(f"Config already exists at {manager.config_file_path}. Overwrite?", fg="yellow")
            if not click.confirm(prompt, default=False):
                click.secho("Init cancelled.", fg="bright_black")
                return
            overwrite = True

        data = manager.create_default_project_config(source_path)

        _section("Model & API")
        data["model"]["name"] = _ask("Model name", default=data["model"]["name"])
        data["model"]["provider"] = _ask("Model provider", default=data["model"]["provider"])
        data["api"]["api_key"] = _ask("API key", default=data["api"]["api_key"], hide_input=True)
        data["api"]["base_url"] = _ask("API base URL", default=data["api"]["base_url"])

        _section("Output")
        data["output"]["base_dir"] = _ask("Output base directory", default=data["output"]["base_dir"])
        data["output"]["cache"] = _ask("Cache directory", default=data["output"]["cache"])
        data["run"]["force"] = _confirm(
            "Force a clean run (ignore cached results and start from scratch)?",
            default=data["run"]["force"],
        )

        _section("Git")
        data["git"]["branch_enabled"] = _confirm(
            "Create or switch to a Git branch during migration?",
            default=data["git"]["branch_enabled"],
        )
        if data["git"]["branch_enabled"]:
            data["git"]["branch_name"] = _ask("Git branch name", default=data["git"]["branch_name"])
            data["git"]["commit"] = _confirm(
                "Commit generated changes automatically?",
                default=data["git"]["commit"],
            )

        _section("Requirement refinement")
        data["requirement_refinement"]["enabled"] = _confirm(
            "Enable requirement refinement?",
            default=data["requirement_refinement"]["enabled"],
        )
        if data["requirement_refinement"]["enabled"]:
            data["requirement_refinement"]["rounds"] = _ask(
                "Requirement refinement rounds",
                default=data["requirement_refinement"]["rounds"],
                type=int,
            )

        _section("Execution refinement")
        data["execution_refinement"]["enabled"] = _confirm(
            "Enable execution-aware refinement?",
            default=data["execution_refinement"]["enabled"],
        )
        if data["execution_refinement"]["enabled"]:
            data["execution_refinement"]["rounds"] = _ask(
                "Execution-aware refinement rounds",
                default=data["execution_refinement"]["rounds"],
                type=int,
            )
            data["execution_refinement"]["translate_tests"] = _confirm(
                "Translate source tests?",
                default=data["execution_refinement"]["translate_tests"],
            )

        path = manager.write_project_config(data, overwrite=overwrite)
        click.echo()
        click.secho(f"✓ Created RustPrint config: {path}", fg="green", bold=True)
        click.echo("  Run: " + click.style("rustprint migrate", fg="cyan", bold=True))
    except ConfigurationError as e:
        click.secho(f"\nConfiguration error: {e.message}", fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as e:
        sys.exit(handle_error(e))
