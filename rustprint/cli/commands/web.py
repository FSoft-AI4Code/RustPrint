import click

from rustprint.cli.help import ColorCommand
from rustprint.web.server import run_server


@click.command(name="web", cls=ColorCommand)
@click.option("--host", default="127.0.0.1", help="Host to bind the server to.")
@click.option("--port", default=5000, type=int, help="Port to bind the server to.")
def web_command(host: str, port: int):
    """Launch the RustPrint web UI."""
    run_server(host=host, port=port)
