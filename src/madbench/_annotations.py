"""Shared handling of run labels and saved messages."""

from __future__ import annotations

import shutil
import unicodedata
from pathlib import Path
from typing import Optional

import yaml


def validate_short_note(value: Optional[str]) -> Optional[str]:
    """Require a non-empty keyword that stays within one path component."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(
        char.isspace() or char in "/\\" or unicodedata.category(char) == "Cc"
        for char in value
    ):
        raise ValueError(
            "--short-note must be a non-empty keyword without whitespace, "
            "path separators, or control characters"
        )
    return value


def resolve_message(note: Optional[str], message: Optional[str]) -> Optional[str]:
    """Keep the original Python ``note`` argument as an alias for message."""
    if note is not None and message is not None:
        raise ValueError("Specify either note or message, not both")
    return message if message is not None else note


def copy_test_definition(
    source: Path, destination: Path, *, message: Optional[str] = None,
) -> None:
    """Snapshot the source YAML, adding a message only to the saved copy."""
    if message is None:
        shutil.copyfile(source, destination)
        return
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    data["message"] = message
    destination.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
