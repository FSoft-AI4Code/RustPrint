import sys

import click

from rustprint.cli.config_manager import ConfigManager, parse_toml_scalar
from rustprint.cli.help import ColorGroup
from rustprint.cli.utils.errors import ConfigurationError, handle_error


@click.group(name="config", cls=ColorGroup)
def config_group():
    """Show or update the current project's RustPrint config."""
    pass


@config_group.command(name="show")
def config_show():
    """Print the current project's config file."""
    try:
        manager = ConfigManager()
        click.echo(f"Config: {manager.config_file_path}")
        click.echo()
        click.echo(manager.raw_toml().rstrip())
    except ConfigurationError as e:
        click.secho(f"\nConfiguration error: {e.message}", fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as e:
        sys.exit(handle_error(e))


@config_group.command(name="set")
@click.argument("key")
@click.argument("value", nargs=-1, required=True)
def config_set(key: str, value: tuple[str, ...]):
    """Set one dotted TOML key in the current project's config."""
    try:
        raw_value = " ".join(value)
        parsed_value = parse_toml_scalar(raw_value)
        manager = ConfigManager()
        path = manager.set_value(key, parsed_value)
        click.secho(f"Updated {key} in {path}", fg="green")
    except ConfigurationError as e:
        click.secho(f"\nConfiguration error: {e.message}", fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as e:
        sys.exit(handle_error(e))
