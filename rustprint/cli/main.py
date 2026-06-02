import sys
import click

from rustprint import __version__
from rustprint.cli.help import ColorGroup


@click.group(cls=ColorGroup)
@click.version_option(version=__version__, prog_name="RustPrint CLI")
@click.pass_context
def cli(ctx):
    """
    RustPrint: AI-powered C-to-Rust translation and documentation generation.
    """
    ctx.ensure_object(dict)


@cli.command()
def version():
    """Display version information."""
    click.echo(f"RustPrint CLI v{__version__}")
    click.echo("Python-based C-to-Rust translation and documentation generator using AI analysis")


from rustprint.cli.commands.config import config_group
from rustprint.cli.commands.init import init_command
from rustprint.cli.commands.migrate import migrate_command
from rustprint.cli.commands.web import web_command

cli.add_command(config_group)
cli.add_command(init_command, name="init")
cli.add_command(migrate_command, name="migrate")
cli.add_command(web_command, name="web")


def main():
    """Entry point for the CLI."""
    try:
        cli(obj={})
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted by user", err=True)
        sys.exit(130)
    except Exception as e:
        click.secho(f"\n✗ Unexpected error: {e}", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
