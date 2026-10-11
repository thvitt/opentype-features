import logging
import sys

from fontTools.ttLib import TTLibError
from rich.console import Console
from rich.logging import RichHandler

from opentype_features.cli import app

error_console = Console(stderr=True)


def main() -> None:
    """Run the command line interface."""
    logging.basicConfig(
        format="%(message)s", handlers=[RichHandler(console=error_console)]
    )
    logging.captureWarnings(capture=True)
    try:
        app()
    except TTLibError as error:
        error_console.print(f"[bold red]Error:[/bold red] not a usable font: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
