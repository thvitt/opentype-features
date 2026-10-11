"""PDF report of the OpenType features of a font."""

import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fpdf import FPDF

from opentype_features import (
    BASELINE_FEATURES,
    FIGURE_TAGS,
    feature_notes,
    shaping_language,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from opentype_features import (
        FeatureExample,
        FontAnalyzer,
        VisualComparator,
    )

logger = logging.getLogger(__name__)

# Layout in mm, font sizes in pt
MARGIN = 12
CONTENT_WIDTH = 210 - 2 * MARGIN
PAGE_BOTTOM = 297 - 14
HEADING_HEIGHT = 9
TILE_GAP = 1.5
TILE_PADDING = 1.2
MIN_TILE_WIDTH = 9
MAX_LABEL_WIDTH = 40
GLYPH_SIZE = 13
GLYPH_LINE = 6.5
LABEL_SIZE = 5
LABEL_LINE = 2.6
DIFF_HEIGHT = 6
FIGURE_LABEL_WIDTH = 62
CHARS_PER_ROW = 16
CHAR_LABEL_WIDTH = 14
CHAR_CELL_HEIGHT = 9.5
CHAR_CODE_SIZE = 4

# Unicode fonts for everything but the examples
FONT_DIR = Path(__file__).parent / "fonts"
UI_FONT = "DejaVuSans"
TAG_FONT = "DejaVuSansMono"

# Relative visual difference below which a change is highlighted
SUBTLE_HIGHLIGHT_RATIO = 0.1

# Colors as RGB tuples
TEXT = (33, 33, 33)
MUTED = (130, 130, 130)
INPUT = (150, 150, 150)
TILE = (244, 244, 244)
PILL = (228, 228, 228)
SUBTLE_CHANGE = (198, 40, 40)
AALT_ONLY = (106, 27, 154)
DEFAULT_ON = ((46, 125, 50), (232, 245, 233))
DEFAULT_IN_CONTEXT = ((191, 87, 0), (255, 243, 224))


class ReportPDF(FPDF):
    """An A4 document with the font name and page number in the footer."""

    def __init__(self, footer_text: str) -> None:
        """Initialize the document."""
        super().__init__(format="A4")
        self.footer_text = footer_text
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(False)
        self.add_font(UI_FONT, style="", fname=FONT_DIR / "DejaVuSans.ttf")
        self.add_font(UI_FONT, style="B", fname=FONT_DIR / "DejaVuSans-Bold.ttf")
        self.add_font(TAG_FONT, style="B", fname=FONT_DIR / "DejaVuSansMono-Bold.ttf")

    def set_ui_font(self, style: Literal["", "B"], size: int) -> None:
        """Select the font for headings, labels and notes, without shaping."""
        self.set_font(UI_FONT, style, size)
        self.set_text_shaping(use_shaping_engine=False)

    def footer(self) -> None:
        """Draw the footer of the current page."""
        self.set_xy(MARGIN, -10)
        self.set_ui_font("", 7)
        self.set_text_color(*MUTED)
        self.cell(CONTENT_WIDTH / 2, 4, _printable(self.footer_text))
        self.cell(CONTENT_WIDTH / 2, 4, str(self.page_no()), align="R")


@dataclass
class Tile:
    """An example as laid out in the report."""

    example: FeatureExample
    label: str
    width: float


def render_pdf_report(
    analyzer: FontAnalyzer,
    examples_by_tag: dict[str, list[FeatureExample]],
    output_path: Path,
    actual_font_path: Path,
    comparator: VisualComparator,
    show_diff: bool = False,
    aalt_only: bool = False,
    show_characters: bool = False,
    on_feature: Callable[[], object] | None = None,
) -> None:
    """Write a PDF report of the examples of each feature."""
    name = analyzer.get_full_name()
    pdf = ReportPDF(f"{name} · OpenType features")
    pdf.add_font("TargetFont", style="", fname=str(actual_font_path))
    # Names in scripts DejaVu lacks are most likely covered by the font itself
    pdf.set_fallback_fonts(["TargetFont"], exact_match=False)
    pdf.add_page()

    shown_tags = [tag for tag, examples in examples_by_tag.items() if examples]
    links = {tag: pdf.add_link() for tag in shown_tags}
    figures_link = pdf.add_link()
    for tag in FIGURE_TAGS & set(shown_tags):
        links[tag] = figures_link

    _render_title(pdf, analyzer, name)
    _render_overview(pdf, analyzer, examples_by_tag, shown_tags, links)

    supported = analyzer.get_supported_features()
    if supported & FIGURE_TAGS:
        _render_figures(pdf, analyzer, supported, figures_link)

    for tag in shown_tags:
        if tag not in FIGURE_TAGS:
            try:
                _render_feature(
                    pdf,
                    analyzer,
                    comparator,
                    tag,
                    examples_by_tag[tag],
                    show_diff,
                    links[tag],
                    aalt_only,
                )
            except Exception:
                logger.exception("Could not render feature %s", tag)
        if on_feature:
            on_feature()

    if show_characters:
        _render_characters(pdf, analyzer)

    pdf.output(str(output_path))


def _render_title(pdf: ReportPDF, analyzer: FontAnalyzer, name: str) -> None:
    pdf.set_text_color(*TEXT)
    pdf.set_ui_font("B", 18)
    pdf.cell(0, 9, _fit(pdf, name, CONTENT_WIDTH), new_x="LMARGIN", new_y="NEXT")

    details = [analyzer.font_path.name, f"{len(analyzer.font.getGlyphOrder())} glyphs"]
    if version := analyzer.get_name(5):
        details.insert(1, version)
    pdf.set_ui_font("", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(
        0,
        5,
        _fit(pdf, " · ".join(details), CONTENT_WIDTH),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.multi_cell(
        0,
        3.5,
        "Each example shows its input without the feature (gray) and with it "
        "(black); red marks subtle changes. Badges mark features that HarfBuzz "
        "applies by default, everywhere or in some contexts only. Outlined "
        "alternates are reachable through Access All Alternates (aalt) only.",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(3)


def _render_overview(
    pdf: ReportPDF,
    analyzer: FontAnalyzer,
    examples_by_tag: dict[str, list[FeatureExample]],
    shown_tags: list[str],
    links: dict[str, int],
) -> None:
    """Render a two-column list of the features, linked to their sections."""
    column_width = (CONTENT_WIDTH - 6) / 2
    row_height = 4.4
    rows = (len(shown_tags) + 1) // 2
    top = pdf.get_y()
    for index, tag in enumerate(shown_tags):
        x = MARGIN + (index // rows) * (column_width + 6)
        y = top + (index % rows) * row_height
        pdf.set_xy(x, y)
        pdf.set_font(TAG_FONT, "B", 8)
        pdf.set_text_color(*TEXT)
        pdf.cell(11, row_height, _printable(tag), link=links[tag])

        pdf.set_ui_font("", 8)
        name = _fit(pdf, analyzer.get_tag_name(tag), column_width - 42)
        pdf.cell(column_width - 42, row_height, name, link=links[tag])

        badge = _default_badge(analyzer, tag)
        if badge:
            _render_badge(pdf, *badge, x=x + column_width - 31, y=y + 0.7)

        pdf.set_xy(x + column_width - 10, y)
        pdf.set_ui_font("", 7)
        pdf.set_text_color(*MUTED)
        pdf.cell(10, row_height, str(len(examples_by_tag[tag])), align="R")
    pdf.set_y(top + rows * row_height + 4)


def _render_figures(
    pdf: ReportPDF, analyzer: FontAnalyzer, supported: set[str], link: int
) -> None:
    """Render the digits in all combinations of the figure features."""
    vertical = [None, *(tag for tag in ("lnum", "onum") if tag in supported)]
    horizontal = [None, *(tag for tag in ("pnum", "tnum") if tag in supported)]
    combinations = [(v, h) for v in vertical for h in horizontal]
    row_height = 7

    _render_heading(pdf, "Figures", link, len(combinations) * row_height)
    pdf.start_section("Figures", level=0)
    _render_tag_pill(pdf, "figures")
    pdf.set_ui_font("B", 11)
    pdf.set_text_color(*TEXT)
    pdf.cell(0, HEADING_HEIGHT - 2, "Figures", new_x="LMARGIN", new_y="NEXT")

    digits = analyzer.get_figure_chars()
    for combination in combinations:
        tags = [tag for tag in combination if tag]
        if pdf.get_y() + row_height > PAGE_BOTTOM:
            pdf.add_page()
        pdf.set_x(MARGIN)
        pdf.set_ui_font("", 8)
        pdf.set_text_color(*MUTED)
        label = " + ".join(analyzer.get_tag_name(tag) for tag in tags) or "Default"
        pdf.cell(
            FIGURE_LABEL_WIDTH, row_height, _fit(pdf, label, FIGURE_LABEL_WIDTH - 2)
        )
        pdf.set_font("TargetFont", "", GLYPH_SIZE)
        pdf.set_text_color(*TEXT)
        pdf.set_text_shaping(
            use_shaping_engine=True, features=dict.fromkeys(tags, True)
        )
        pdf.cell(0, row_height, digits, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)


def _render_characters(pdf: ReportPDF, analyzer: FontAnalyzer) -> None:
    """Render a code chart of the characters the font maps, 16 to a row."""
    codepoints = sorted((analyzer.font.getBestCmap() or {}).keys())
    rows: dict[int, set[int]] = {}
    for codepoint in codepoints:
        rows.setdefault(codepoint // CHARS_PER_ROW, set()).add(codepoint)

    pdf.add_page()
    pdf.start_section("Characters", level=0)
    _render_tag_pill(pdf, "chars")
    pdf.set_ui_font("B", 11)
    pdf.set_text_color(*TEXT)
    pdf.cell(40, HEADING_HEIGHT - 2, "Characters")
    pdf.set_ui_font("", 7)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, HEADING_HEIGHT - 2, f"{len(codepoints)} encoded characters", align="R")
    pdf.set_y(pdf.get_y() + HEADING_HEIGHT)

    cell_width = (CONTENT_WIDTH - CHAR_LABEL_WIDTH) / CHARS_PER_ROW
    for row, row_codepoints in rows.items():
        if pdf.get_y() + CHAR_CELL_HEIGHT > PAGE_BOTTOM:
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_xy(MARGIN, y)
        pdf.set_font(TAG_FONT, "B", 6)
        pdf.set_text_color(*MUTED)
        pdf.cell(CHAR_LABEL_WIDTH, CHAR_CELL_HEIGHT, f"{row:03X}x")
        for column in range(CHARS_PER_ROW):
            codepoint = row * CHARS_PER_ROW + column
            if codepoint in row_codepoints:
                x = MARGIN + CHAR_LABEL_WIDTH + column * cell_width
                _render_character(pdf, codepoint, x, y, cell_width)
        pdf.set_y(y + CHAR_CELL_HEIGHT + TILE_GAP / 2)


def _render_character(
    pdf: ReportPDF, codepoint: int, x: float, y: float, width: float
) -> None:
    """Render a character in the font above its code point."""
    tile_width = width - TILE_GAP / 2
    pdf.set_fill_color(*TILE)
    pdf.rect(
        x,
        y,
        tile_width,
        CHAR_CELL_HEIGHT,
        style="F",
        round_corners=True,
        corner_radius=1,
    )
    char = chr(codepoint)
    if unicodedata.category(char) not in {"Cc", "Cs"}:
        pdf.set_font("TargetFont", "", GLYPH_SIZE)
        pdf.set_text_shaping(use_shaping_engine=False)
        available = tile_width - 2 * TILE_PADDING
        if (glyph_width := pdf.get_string_width(char)) > available:
            pdf.set_font_size(GLYPH_SIZE * available / glyph_width)
        pdf.set_text_color(*TEXT)
        pdf.set_xy(x, y + TILE_PADDING / 2)
        pdf.cell(tile_width, GLYPH_LINE, char, align="C")
    pdf.set_ui_font("", CHAR_CODE_SIZE)
    pdf.set_text_color(*MUTED)
    pdf.set_xy(x, y + CHAR_CELL_HEIGHT - LABEL_LINE)
    pdf.cell(tile_width, LABEL_LINE - 0.3, f"{codepoint:04X}", align="C")


def _render_feature(
    pdf: ReportPDF,
    analyzer: FontAnalyzer,
    comparator: VisualComparator,
    tag: str,
    examples: list[FeatureExample],
    show_diff: bool,
    link: int,
    aalt_only: bool = False,
) -> None:
    """Render the heading and example tiles of a single feature."""
    rows = _layout_rows(pdf, analyzer, examples)
    first_row_height = _row_height(rows[0], show_diff) if rows else 0
    _render_heading(pdf, tag, link, first_row_height)

    friendly_name = _printable(analyzer.get_tag_name(tag))
    pdf.start_section(_printable(f"{tag}: {friendly_name}"), level=0)
    _render_tag_pill(pdf, tag)
    pdf.set_ui_font("B", 11)
    pdf.set_text_color(*TEXT)
    name = _fit(pdf, friendly_name, CONTENT_WIDTH / 2)
    pdf.cell(pdf.get_string_width(name) + 2, HEADING_HEIGHT - 2, name)
    if badge := _default_badge(analyzer, tag):
        _render_badge(pdf, *badge, x=pdf.get_x(), y=pdf.get_y() + 2)
    if notes := feature_notes(analyzer, tag, examples, aalt_only):
        pdf.set_ui_font("", 7)
        pdf.set_text_color(*MUTED)
        heading_end = pdf.get_x() + 3
        width = MARGIN + CONTENT_WIDTH - heading_end
        pdf.set_x(heading_end)
        pdf.cell(
            width, HEADING_HEIGHT - 2, _fit(pdf, " · ".join(notes), width), align="R"
        )
    pdf.set_y(pdf.get_y() + HEADING_HEIGHT - 2)

    y = pdf.get_y()
    for row in rows:
        height = _row_height(row, show_diff)
        if y + height > PAGE_BOTTOM:
            pdf.add_page()
            y = MARGIN
        x: float = MARGIN
        has_labels = any(tile.label for tile in row)
        for tile in row:
            _render_tile(pdf, comparator, tile, x, y, height, has_labels, show_diff)
            x += tile.width + TILE_GAP
        y += height + TILE_GAP
    pdf.set_y(y + 4)


def _render_heading(pdf: FPDF, tag: str, link: int, first_row_height: float) -> None:
    """Start a section on a new page unless its heading and first row fit."""
    if pdf.get_y() + HEADING_HEIGHT + first_row_height > PAGE_BOTTOM:
        pdf.add_page()
    pdf.set_link(link, page=pdf.page, y=pdf.get_y())


def _render_tag_pill(pdf: FPDF, tag: str) -> None:
    tag = _printable(tag)
    pdf.set_font(TAG_FONT, "B", 9)
    width = pdf.get_string_width(tag) + 3
    pdf.set_fill_color(*PILL)
    pdf.rect(MARGIN, pdf.get_y() + 1.3, width, 4.6, style="F", round_corners=True)
    pdf.set_text_color(*TEXT)
    pdf.set_x(MARGIN)
    pdf.cell(width, HEADING_HEIGHT - 2, tag, align="C")
    pdf.set_x(MARGIN + width + 2)


def _default_badge(
    analyzer: FontAnalyzer, tag: str
) -> tuple[str, tuple[tuple[int, int, int], tuple[int, int, int]]] | None:
    if tag in analyzer.default_features:
        return "on by default", DEFAULT_ON
    if tag in analyzer.contextual_default_features:
        return "in context", DEFAULT_IN_CONTEXT
    return None


def _render_badge(
    pdf: ReportPDF,
    text: str,
    colors: tuple[tuple[int, int, int], tuple[int, int, int]],
    x: float,
    y: float,
) -> None:
    foreground, background = colors
    pdf.set_ui_font("B", 6)
    width = pdf.get_string_width(text) + 2.4
    pdf.set_fill_color(*background)
    pdf.rect(x, y, width, 3, style="F", round_corners=True)
    pdf.set_text_color(*foreground)
    pdf.set_xy(x, y)
    pdf.cell(width, 3, text, align="C")


def _layout_rows(
    pdf: ReportPDF, analyzer: FontAnalyzer, examples: list[FeatureExample]
) -> list[list[Tile]]:
    """Measure the examples' tiles and distribute them over rows."""
    rows: list[list[Tile]] = [[]]
    row_width = 0.0
    for example in examples:
        label = _printable(analyzer.get_cell_label(example))
        pdf.set_font("TargetFont", "", GLYPH_SIZE)
        pdf.set_text_shaping(use_shaping_engine=False)
        glyph_width = max(
            pdf.get_string_width(example.input_pattern),
            pdf.get_string_width(example.output_chars),
        )
        pdf.set_ui_font("", LABEL_SIZE)
        label_width = min(pdf.get_string_width(label), MAX_LABEL_WIDTH)
        width = min(max(glyph_width, label_width) + 2 * TILE_PADDING + 1, CONTENT_WIDTH)
        tile = Tile(example, label, max(width, MIN_TILE_WIDTH))

        if rows[-1] and row_width + tile.width > CONTENT_WIDTH:
            rows.append([])
            row_width = 0.0
        rows[-1].append(tile)
        row_width += tile.width + TILE_GAP
    return [row for row in rows if row]


def _row_height(row: list[Tile], show_diff: bool) -> float:
    label_height = LABEL_LINE if any(tile.label for tile in row) else 0
    diff_height = DIFF_HEIGHT + 0.5 if show_diff else 0
    return 2 * TILE_PADDING + label_height + 2 * GLYPH_LINE + diff_height


def _render_tile(
    pdf: ReportPDF,
    comparator: VisualComparator,
    tile: Tile,
    x: float,
    y: float,
    height: float,
    has_labels: bool,
    show_diff: bool,
) -> None:
    """Render an example: its label, its input, and the input shaped with the feature."""
    example = tile.example
    pdf.set_fill_color(*TILE)
    pdf.rect(x, y, tile.width, height, style="F", round_corners=True, corner_radius=1)
    if example.aalt_only:
        pdf.set_draw_color(*AALT_ONLY)
        pdf.set_line_width(0.35)
        pdf.rect(x, y, tile.width, height, round_corners=True, corner_radius=1)
    line_y = y + TILE_PADDING

    if has_labels:
        if tile.label:
            pdf.set_ui_font("", LABEL_SIZE)
            text_width = pdf.get_string_width(tile.label)
            available = tile.width - 2 * TILE_PADDING
            if text_width > available:
                pdf.set_font_size(LABEL_SIZE * available / text_width)
            pdf.set_text_color(*MUTED)
            pdf.set_xy(x, line_y)
            pdf.cell(tile.width, LABEL_LINE, tile.label, align="C")
        line_y += LABEL_LINE

    pdf.set_font("TargetFont", "", GLYPH_SIZE)
    pdf.set_text_shaping(use_shaping_engine=False)
    pdf.set_text_color(*INPUT)
    pdf.set_xy(x, line_y)
    pdf.cell(tile.width, GLYPH_LINE, example.input_pattern, align="C")
    line_y += GLYPH_LINE

    if example.diff_ratio < SUBTLE_HIGHLIGHT_RATIO:
        # Overlay subtle changes on the unchanged input
        pdf.set_text_color(*PILL)
        pdf.set_xy(x, line_y)
        pdf.cell(tile.width, GLYPH_LINE, example.input_chars, align="C")
        pdf.set_text_color(*SUBTLE_CHANGE)
    else:
        pdf.set_text_color(*TEXT)
    pdf.set_text_shaping(
        use_shaping_engine=True,
        features={**BASELINE_FEATURES, example.feature_tag: example.feature_value},  # type: ignore[bad-argument-type]
        language=shaping_language(example.languages),
    )
    pdf.set_xy(x, line_y)
    pdf.cell(tile.width, GLYPH_LINE, example.input_chars, align="C")
    line_y += GLYPH_LINE

    if show_diff:
        _render_diff(pdf, comparator, example, x, line_y + 0.5, tile.width)


def _render_diff(
    pdf: FPDF,
    comparator: VisualComparator,
    example: FeatureExample,
    x: float,
    y: float,
    width: float,
) -> None:
    diff_image = comparator.get_diff_image(
        example.input_chars,
        example.feature_tag,
        example.languages,
        example.feature_value,
    )
    image_height = DIFF_HEIGHT
    image_width = diff_image.width * image_height / diff_image.height
    if image_width > width - 2 * TILE_PADDING:
        image_width = width - 2 * TILE_PADDING
        image_height = diff_image.height * image_width / diff_image.width
    pdf.image(
        diff_image, x=x + (width - image_width) / 2, y=y, w=image_width, h=image_height
    )


def _fit(pdf: FPDF, text: str, width: float) -> str:
    """Shorten text with an ellipsis until it fits into width."""
    text = _printable(text)
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + "…") > width:
        text = text[:-1]
    return text + "…"


def _printable(text: str) -> str:
    """Replace control characters and lone surrogates, e.g. from broken name tables."""
    return "".join(
        " "
        if unicodedata.category(char) == "Cc"
        else "\ufffd"
        if unicodedata.category(char) == "Cs"
        else char
        for char in text
    )
