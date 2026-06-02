"""
Error handling utilities and exit codes for CLI.

Exit Codes:
  0: Success
  1: General error
  2: Configuration error (missing/invalid credentials)
  3: Repository error (not a git repo, no code files)
  4: LLM API error (including rate limits)
  5: File system error (permissions, disk space)
"""

import sys
import click
from typing import Optional


EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_REPOSITORY_ERROR = 3
EXIT_API_ERROR = 4
EXIT_FILESYSTEM_ERROR = 5


class RustPrintError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_GENERAL_ERROR):
        self.message = message
        self.exit_code = exit_code
        super().__init__(self.message)


class ConfigurationError(RustPrintError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_CONFIG_ERROR)


class RepositoryError(RustPrintError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_REPOSITORY_ERROR)


class APIError(RustPrintError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_API_ERROR)


class FileSystemError(RustPrintError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_FILESYSTEM_ERROR)


def handle_error(error: Exception, verbose: bool = False) -> int:
    if isinstance(error, RustPrintError):
        click.secho(f"\nError: {error.message}", fg="red", err=True)
        return error.exit_code
    else:
        click.secho(f"\nUnexpected error: {error}", fg="red", err=True)
        if verbose:
            import traceback
            click.echo(traceback.format_exc(), err=True)
        return EXIT_GENERAL_ERROR


def error_with_suggestion(message: str, suggestion: str, exit_code: int = EXIT_GENERAL_ERROR):
    click.secho(f"\nError: {message}", fg="red", err=True)
    click.echo(f"\n{suggestion}", err=True)
    sys.exit(exit_code)


def warning(message: str):
    click.secho(f"{message}", fg="yellow")


def success(message: str):
    click.secho(f"{message}", fg="green")


def info(message: str):
    click.echo(message)
