"""Command line interface of the OpenType feature report."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from cyclopts import App, Parameter
from cyclopts.types import ExistingFile  # noqa: TC002  # Cyclopts evaluates hints
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from opentype_features import (
    MAX_POSITIONING_EXAMPLES,
    SUBTLE_RATIO,
    FontAnalyzer,
    VisualComparator,
    collect_examples,
    render_pdf_report,
)

if TYPE_CHECKING:
    from opentype_features import FeatureExample

logger = logging.getLogger(__name__)
console = Console()

app = App(help="Analyze the OpenType features of a font and write a PDF report.")


@app.default
def report(
    font: ExistingFile,
    /,
    *,
    output: Annotated[Path | None, Parameter(alias="-o")] = None,
    diff: bool = True,
    min_ratio: float = 0.0,
    hide_subtle: Annotated[bool, Parameter(negative=())] = False,
) -> None:
    """
    Analyze the OpenType features of a font and write a PDF report.

    Args:
        font: the .ttf or .otf font file to analyze
        output: path of the PDF report [default: FONT name + "-features.pdf"]
        diff: show the visual difference of each example in the report
        min_ratio: hide examples whose relative visual difference is smaller
        hide_subtle: hide examples whose relative visual difference is below 1%

    """
    if hide_subtle:
        min_ratio = max(min_ratio, SUBTLE_RATIO)
    output = output or Path(f"{font.stem}-features.pdf")
    written = True

    analyzer = FontAnalyzer(font)
    analyzer.analyze()
    with analyzer.modified_font() as actual_font_path:
        comparator = VisualComparator(actual_font_path)
        analyzer.detect_default_features(comparator)
        examples = collect_examples(analyzer, comparator, min_ratio)
        try:
            render_pdf_report(
                analyzer,
                examples,
                output,
                actual_font_path,
                comparator,
                show_diff=diff,
            )
        except Exception:
            logger.exception("Could not write the PDF report %s", output)
            written = False

    console.print(summarize(analyzer, examples, output if written else None))


def summarize(
    analyzer: FontAnalyzer,
    examples_by_tag: dict[str, list[FeatureExample]],
    report_path: Path | None,
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
        shown_inputs = sum(1 + len(e.input_variants) for e in examples)
        table.add_row(
            tag,
            analyzer.get_tag_name(tag),
            _default_status(analyzer, tag),
            str(len(examples)),
            str(found),
            "; ".join(_notes(analyzer, tag, examples, shown_inputs)),
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


def _notes(
    analyzer: FontAnalyzer,
    tag: str,
    examples: list[FeatureExample],
    shown_inputs: int,
) -> list[str]:
    notes = []
    if total := analyzer.positioning_totals.get(tag):
        notes.append(f"{MAX_POSITIONING_EXAMPLES} strongest of {total} positionings")
    if hidden := len(analyzer.feature_examples[tag]) - shown_inputs:
        notes.append(f"{hidden} invisible or too subtle")
    if folded := sum(len(e.input_variants) for e in examples):
        notes.append(f"{folded} similar inputs folded")
    return notes
