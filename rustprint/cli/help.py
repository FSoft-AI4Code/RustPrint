import click

from rustprint import __version__


class RustPrintFormatter(click.HelpFormatter):
    def write_usage(self, prog, args="", prefix=None):
        if prefix is None:
            prefix = "Usage: "
        super().write_usage(
            click.style(prog, fg="bright_magenta", bold=True),
            args,
            prefix=click.style(prefix, fg="yellow", bold=True),
        )

    def write_heading(self, heading):
        super().write_heading(click.style(heading, fg="yellow", bold=True))

    def write_dl(self, rows, col_max=30, col_spacing=2):
        colored = [(click.style(term, fg="cyan", bold=True), definition) for term, definition in rows]
        super().write_dl(colored, col_max=col_max, col_spacing=col_spacing)


def banner() -> str:
    return (
        "  "
        + click.style("👋 RustPrint", fg="bright_magenta", bold=True)
        + " "
        + click.style(f"v{__version__}", fg="bright_black")
        + "\n"
        + click.style("  AI-powered C → Rust migration", fg="bright_black")
    )


class ColorCommand(click.Command):
    def get_help(self, ctx):
        ctx.formatter_class = RustPrintFormatter
        return super().get_help(ctx)


class ColorGroup(click.Group):
    command_class = ColorCommand

    def get_help(self, ctx):
        ctx.formatter_class = RustPrintFormatter
        return super().get_help(ctx)

    def format_help(self, ctx, formatter):
        formatter.write(banner() + "\n\n")
        super().format_help(ctx, formatter)


ColorGroup.group_class = ColorGroup
