import logging
import sys

from rich.logging import RichHandler

from opentype_features.cli import app, error_console


def main() -> None:
    """Run the command line interface."""
    logging.basicConfig(
        format="%(message)s", handlers=[RichHandler(console=error_console)]
    )
    logging.captureWarnings(capture=True)
    try:
        app()
    except ValueError as error:
        error_console.print(f"[bold red]Error:[/bold red] {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
