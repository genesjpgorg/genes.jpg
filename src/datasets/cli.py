"""``genes-datasets`` command-line interface."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from datasets import build as builder
from datasets import manifest, schema

app = typer.Typer(
    help="genes.jpg dataset toolkit: schema export, validation and inspection of built datasets.",
    no_args_is_help=True,
    add_completion=False,
)

_DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "dataset.schema.json"


@app.command("schema-export")
def schema_export(
    out: Annotated[
        Path, typer.Option("--out", "-o", help="where to write the JSON Schema")
    ] = _DEFAULT_SCHEMA_PATH,
) -> None:
    """Export the JSON Schema of all record types (``datasets.schema.json_schema``)."""
    typer.echo(f"wrote {schema.export_json_schema(out)}")


@app.command()
def validate(
    root: Annotated[Path, typer.Argument(help="dataset root directory")],
    check_hashes: Annotated[
        bool, typer.Option("--check-hashes", help="re-hash image (sha256) and genome (md5) files")
    ] = False,
) -> None:
    """Check tables, references, files and manifest; exit code 1 if problems are found."""
    problems = manifest.validate_dataset(root, check_hashes=check_hashes)
    for problem in problems:
        typer.echo(problem)
    if problems:
        typer.echo(f"{root}: {len(problems)} problem(s) found")
        raise typer.Exit(code=1)
    typer.echo(f"{root}: OK")


@app.command()
def info(root: Annotated[Path, typer.Argument(help="dataset root directory")]) -> None:
    """Print a summary of the dataset manifest (dataset.json)."""
    path = Path(root) / manifest.MANIFEST_FILE
    if not path.is_file():
        typer.echo(f"{path}: not found", err=True)
        raise typer.Exit(code=1)
    try:
        m = manifest.read_manifest(root)
    except ValidationError as exc:
        typer.echo(f"{path}: invalid manifest: {manifest.format_validation_error(exc)}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"{path}: cannot read manifest: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"name: {m.name}")
    typer.echo(f"version: {m.version}")
    typer.echo(f"schema_version: {m.schema_version}")
    typer.echo(f"created_at: {m.created_at.isoformat()}")
    typer.echo(f"created_by: {m.created_by}")
    if m.description:
        typer.echo(f"description: {m.description}")
    typer.echo("tables:")
    for name, table in m.tables.items():
        typer.echo(f"  {name}: {table.path} ({table.n_rows} rows)")
    typer.echo("counts:")
    for key, value in m.counts.items():
        typer.echo(f"  {key}: {value:,}" if key.endswith("bytes") else f"  {key}: {value}")
    if m.sources:
        typer.echo("sources:")
        for source in m.sources:
            revision = f" @ {source.revision}" if source.revision else ""
            typer.echo(f"  - {source.name}{revision} <{source.url}>")
    if m.selection:
        typer.echo("selection:")
        for line in json.dumps(m.selection, indent=2, default=str).splitlines():
            typer.echo(f"  {line}")


@app.command("build")
def build_cmd(
    config: Annotated[Path, typer.Argument(help="build config JSON (see configs/)")],
    root_override: Annotated[
        Path | None,
        typer.Option(
            "--root-override",
            help="write the dataset here instead of config.root (relative to the current "
            "directory)",
        ),
    ] = None,
    progress: Annotated[
        bool, typer.Option("--progress/--no-progress", help="show download progress bars")
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="resolve species, assemblies and candidates; download nothing"
        ),
    ] = False,
) -> None:
    """Build a genome <-> image dataset from a config; exit code 1 if the build fails."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = builder.load_config(config, root_override=root_override)
    except ValidationError as exc:
        typer.echo(f"{config}: invalid config: {manifest.format_validation_error(exc)}", err=True)
        raise typer.Exit(code=2) from None
    except (OSError, TypeError, ValueError) as exc:
        typer.echo(f"{config}: cannot read config: {exc}", err=True)
        raise typer.Exit(code=2) from None
    try:
        report = builder.build(cfg, progress=progress, dry_run=dry_run)
    except builder.BuildError as exc:
        if exc.report is not None:
            typer.echo(builder.format_report(exc.report))
        typer.echo(f"build failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(builder.format_report(report))


if __name__ == "__main__":
    app()
