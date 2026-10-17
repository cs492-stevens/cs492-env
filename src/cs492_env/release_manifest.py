"""Generate a website release manifest from Docker BuildKit metadata."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .cli import DIGEST_PATTERN, release_from_document


def image_digest(metadata: Any) -> str:
    """Extract and validate the pushed multi-architecture image digest."""
    if not isinstance(metadata, dict):
        raise ValueError("BuildKit metadata must be a JSON object")
    digest = metadata.get("containerimage.digest")
    if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
        raise ValueError("BuildKit metadata does not contain a valid image digest")
    return digest


def manifest_document(release: str, digest: str) -> dict[str, object]:
    """Construct and validate the stable manifest for one image release."""
    document: dict[str, object] = {
        "schema_version": 1,
        "release": release,
        "image_tag": release,
        "image_digest": digest,
        "minimum_cli_version": __version__,
        "recommended_cli_version": __version__,
        "notice": None,
    }
    release_from_document(document)
    return document


def write_manifest(metadata_path: Path, release: str, output: Path) -> None:
    """Write a validated manifest atomically next to prior releases."""
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read BuildKit metadata: {error}") from error

    document = manifest_document(release, image_digest(metadata))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output.parent,
        delete=False,
    ) as temporary:
        json.dump(document, temporary, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate stable.<release>.json from Docker BuildKit metadata."
    )
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    args = parse_args(arguments)
    try:
        write_manifest(args.metadata, args.release, args.output)
    except ValueError as error:
        raise SystemExit(f"error: {error}") from None
    print(f"Wrote release manifest: {args.output}")


if __name__ == "__main__":
    main()
