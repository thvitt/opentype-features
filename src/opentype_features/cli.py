"""Command line interface of the OpenType feature report."""

import functools
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from cyclopts import App, Parameter
from cyclopts.types import ExistingFile  # noqa: TC002  # Cyclopts evaluates hints
from fontTools.ttLib import TTLibError
from rich.console import Console, Group
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from opentype_features import (
    SUBTLE_RATIO,
    FontAnalyzer,
    VisualComparator,
    collect_examples,
    feature_notes,
)
from opentype_features.pdf_report import render_pdf_report
from opentype_features.summary_table import summary_row, update_summary_table

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.progress import (
        TaskID,
    )

    from opentype_features import FeatureExample

SUMMARY_TABLE_NAME = "opentype-features.tsv"

logger = logging.getLogger(__name__)
console = Console()
error_console = Console(stderr=True)

app = App(help="Analyze the OpenType features of fonts and write PDF reports.")


@dataclass(frozen=True)
class ReportOptions:
    """What the reports show."""

    show_diff: bool
    min_ratio: float
    aalt_only: bool


@app.default
def report(
    fonts: list[ExistingFile],
    /,
    *,
    output: Annotated[Path | None, Parameter(alias="-o")] = None,
    tsv: Path | None = None,
    diff: bool = False,
    min_ratio: float = 0.0,
    hide_subtle: Annotated[bool, Parameter(negative=())] = False,
    aalt_only: Annotated[bool, Parameter(negative=())] = False,
) -> int:
    """
    Analyze the OpenType features of fonts and write a PDF report for each.

    Args:
        fonts: the .ttf or .otf font files to analyze
        output: path of the PDF report, or the directory of the reports of several fonts [default: the current directory, reports named like the fonts]
        tsv: table of the number of examples per feature and font, updated if it exists [default: opentype-features.tsv next to the reports]
        diff: add a picture of the pixel difference to each example
        min_ratio: hide examples whose relative visual difference is smaller
        hide_subtle: hide examples whose relative visual difference is below 1%
        aalt_only: show only those alternates of aalt that no other feature reaches

    Returns:
        1 if a font could not be analyzed, else 0

    """
    if hide_subtle:
        min_ratio = max(min_ratio, SUBTLE_RATIO)
    options = ReportOptions(diff, min_ratio, aalt_only)
    pdf_paths = report_paths(fonts, output)
    tsv = tsv or pdf_paths[0].parent / SUMMARY_TABLE_NAME
    failed = False

    with _progress() as progress:
        all_fonts = progress.add_task("All fonts", total=len(fonts))
        for font, pdf_path in zip(fonts, pdf_paths, strict=True):
            try:
                analyzer = _report_font(font, pdf_path, options, progress)
            except TTLibError as error:
                logger.error("%s is not a usable font: %s", font, error)  # noqa: TRY400
                failed = True
            except Exception:
                logger.exception("Could not analyze %s", font)
                failed = True
            else:
                update_summary_table(tsv, [summary_row(analyzer)])
            finally:
                progress.advance(all_fonts)

    console.print(Text.assemble("Summary table: ", (str(tsv), "bold green")))
    return int(failed)


def report_paths(fonts: list[Path], output: Path | None) -> list[Path]:
    """Return the paths of the fonts' PDF reports, creating their directory."""
    if output and len(fonts) == 1 and not output.is_dir():
        output.parent.mkdir(parents=True, exist_ok=True)
        return [output]
    directory = output or Path()
    directory.mkdir(parents=True, exist_ok=True)
    names = [font.with_suffix(".pdf").name for font in fonts]
    if duplicates := sorted(
        name for name, count in Counter(names).items() if count > 1
    ):
        msg = f"Several fonts would be reported as {', '.join(duplicates)}"
        raise ValueError(msg)
    return [directory / name for name in names]


def _progress() -> Progress:
    """Return transient progress bars, shown only on a terminal."""
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=error_console,
        transient=True,
        disable=not error_console.is_terminal,
        redirect_stdout=console.is_terminal,
    )


def _report_font(
    font: Path, pdf_path: Path, options: ReportOptions, progress: Progress
) -> FontAnalyzer:
    """Analyze a font, write its PDF report and print its summary."""
    task = progress.add_task(f"{font.name}: analyzing", total=None)
    try:
        analyzer = FontAnalyzer(font)
        analyzer.analyze()
        with analyzer.modified_font() as actual_font_path:
            comparator = VisualComparator(actual_font_path)
            examples = _analyze_examples(analyzer, comparator, options, progress, task)
            written = _write_pdf(
                analyzer,
                examples,
                pdf_path,
                actual_font_path,
                comparator,
                options,
                functools.partial(progress.advance, task),
            )
    finally:
        progress.remove_task(task)

    console.print(
        summarize(analyzer, examples, pdf_path if written else None, options.aalt_only)
    )
    return analyzer


def _analyze_examples(
    analyzer: FontAnalyzer,
    comparator: VisualComparator,
    options: ReportOptions,
    progress: Progress,
    task: TaskID,
) -> dict[str, list[FeatureExample]]:
    """Detect the default features and collect the examples worth showing."""
    name = analyzer.font_path.name
    example_count = sum(map(len, analyzer.feature_examples.values()))
    advance = functools.partial(progress.advance, task)

    progress.update(
        task,
        description=f"{name}: detecting default features",
        total=2 * example_count + len(analyzer.feature_examples),
    )
    analyzer.detect_default_features(comparator, advance)
    progress.update(
        task, description=f"{name}: comparing renderings", completed=example_count
    )
    examples = collect_examples(
        analyzer, comparator, options.min_ratio, options.aalt_only, advance
    )
    progress.update(
        task, description=f"{name}: writing PDF", completed=2 * example_count
    )
    return examples


def _write_pdf(
    analyzer: FontAnalyzer,
    examples: dict[str, list[FeatureExample]],
    pdf_path: Path,
    actual_font_path: Path,
    comparator: VisualComparator,
    options: ReportOptions,
    on_feature: Callable[[], object],
) -> bool:
    """Write the PDF report, returning whether that succeeded."""
    try:
        render_pdf_report(
            analyzer,
            examples,
            pdf_path,
            actual_font_path,
            comparator,
            show_diff=options.show_diff,
            aalt_only=options.aalt_only,
            on_feature=on_feature,
        )
    except Exception:
        logger.exception("Could not write the PDF report %s", pdf_path)
        return False
    return True


def summarize(
    analyzer: FontAnalyzer,
    examples_by_tag: dict[str, list[FeatureExample]],
    report_path: Path | None,
    aalt_only: bool = False,
) -> Group:
    """Summarize the findings per feature."""
    table = Table(title=f"OpenType features of {analyzer.font_path.name}")
    table.add_column("Tag", style="bold cyan")
    table.add_column("Feature")
    table.add_column("Default")
    table.add_column("Shown", justify="right")
    table.add_column("Found", justify="right")
    table.add_column("Notes", style="dim")

    for tag, examples in examples_by_tag.items():
        found = analyzer.count_examples(tag)
        table.add_row(
            tag,
            analyzer.get_tag_name(tag),
            _default_status(analyzer, tag),
            str(len(examples)),
            str(found),
            "; ".join(feature_notes(analyzer, tag, examples, aalt_only)),
            style=None if examples else "dim",
        )

    parts: list[Table | Text] = [table]
    if unanalyzed := analyzer.get_font_feature_tags() - examples_by_tag.keys():
        parts.append(
            Text(f"Features without analyzable rules: {', '.join(sorted(unanalyzed))}")
        )
    if report_path:
        parts.append(Text.assemble("PDF report: ", (str(report_path), "bold green")))
    else:
        parts.append(Text("No PDF report written.", style="bold red"))
    return Group(*parts)


def _default_status(analyzer: FontAnalyzer, tag: str) -> str:
    if tag in analyzer.default_features:
        return "on"
    if tag in analyzer.contextual_default_features:
        return "in context"
    return ""
