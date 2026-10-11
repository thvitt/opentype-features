"""Tab-separated table of the number of examples per feature of several fonts."""

import csv
from typing import TYPE_CHECKING

from opentype_features import get_feature_priority

if TYPE_CHECKING:
    from pathlib import Path

    from opentype_features import FontAnalyzer

FILENAME_COLUMN = "filename"
FONT_NAME_COLUMN = "font name"


def summary_row(analyzer: FontAnalyzer) -> dict[str, str]:
    """Return the file and font name and the number of examples per feature."""
    return {
        FILENAME_COLUMN: analyzer.font_path.name,
        FONT_NAME_COLUMN: analyzer.get_full_name(),
        **{
            tag: str(analyzer.count_examples(tag))
            for tag in analyzer.get_font_feature_tags()
        },
    }


def update_summary_table(path: Path, rows: list[dict[str, str]]) -> None:
    """
    Add the rows to the table at path, replacing the rows of the same files.

    Columns of features that only some fonts have stay empty for the others.
    """
    rows_by_file = {row[FILENAME_COLUMN]: row for row in _read_rows(path)}
    rows_by_file.update((row[FILENAME_COLUMN], row) for row in rows)
    tags = {key for row in rows_by_file.values() for key in row} - {
        FILENAME_COLUMN,
        FONT_NAME_COLUMN,
    }

    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            [
                FILENAME_COLUMN,
                FONT_NAME_COLUMN,
                *sorted(tags, key=get_feature_priority),
            ],
            restval="",
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows_by_file.values())


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, delimiter="\t")
        if FILENAME_COLUMN not in (reader.fieldnames or []):
            msg = f"{path} has no {FILENAME_COLUMN!r} column"
            raise ValueError(msg)
        return [
            {key: value or "" for key, value in row.items() if key is not None}
            for row in reader
        ]
