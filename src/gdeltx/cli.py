from typing import Annotated

import typer

from gdeltx import __version__

app = typer.Typer(
    name="gdeltx",
    help="OSINT investigation over the GDELT APIs.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the gdeltx version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None: ...
