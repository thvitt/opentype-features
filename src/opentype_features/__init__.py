"""Analyze OpenType features in a font file and generate reports."""

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
from fpdf import FPDF

from opentype_features.feature_names import FEATURE_NAMES

# GSUB Lookup Types
LOOKUP_TYPE_SINGLE = 1
LOOKUP_TYPE_MULTIPLE = 2
LOOKUP_TYPE_ALTERNATE = 3
LOOKUP_TYPE_LIGATURE = 4
LOOKUP_TYPE_EXTENSION = 7

# Private Use Area for rendering unmapped glyphs
PUA_START = 0xE000

# PDF Layout Constants
PAGE_BREAK_THRESHOLD = 250
MIN_SUBS_FOR_MULTI_COLUMN = 5
GUTTER_WIDTH = 10
ROW_HEIGHT = 8
PAGE_BOTTOM_MARGIN = 275
COLUMN_NAME_WIDTH = 32
COLUMN_RENDER_WIDTH = 13
COLUMN_UNIT_WIDTH = COLUMN_NAME_WIDTH + COLUMN_RENDER_WIDTH
TOTAL_UNIT_WIDTH = COLUMN_UNIT_WIDTH * 2


@dataclass(frozen=True)
class Substitution:
    """Represents a single OpenType substitution."""

    input_glyphs: str
    input_chars: str
    output_glyphs: str
    output_chars: str
    feature_tag: str

    @property
    def is_simple(self) -> bool:
        """Check if this is a 1-to-1 mapping."""
        return len(self.input_chars) == 1 and len(self.output_chars) == 1


class FontAnalyzer:
    """Analyzes a font for OpenType features and generates reports."""

    def __init__(self, font_path: Path) -> None:
        """
        Initialize the FontAnalyzer.

        Args:
            font_path: The path to the font file to analyze.

        """
        self.font_path = font_path
        self.font = TTFont(str(font_path))
        self.reverse_cmap = self._build_reverse_cmap()
        self.pua_map: dict[str, str] = {}
        self.feature_substitutions: dict[str, list[Substitution]] = {}

    def _build_reverse_cmap(self) -> dict[str, int]:
        cmap = self.font.getBestCmap()
        if cmap is None:
            return {}
        return {v: k for k, v in cmap.items()}

    def get_tag_name(self, tag: str) -> str:
        """Return the friendly name for an OpenType feature tag."""
        return FEATURE_NAMES.get(tag, tag)

    def get_glyph_info(self, glyph_name: str) -> tuple[str, str]:
        """Return the character and glyph name for a given glyph name."""
        if glyph_name in self.reverse_cmap:
            return chr(self.reverse_cmap[glyph_name]), glyph_name

        if glyph_name not in self.pua_map:
            pua_char = chr(PUA_START + len(self.pua_map))
            self.pua_map[glyph_name] = pua_char

        return self.pua_map[glyph_name], glyph_name

    def get_display_name(self, glyph_name: str) -> str:
        """Generate a display name for a glyph following user rules."""
        if glyph_name.startswith("glyph") and glyph_name[5:].isdigit():
            if glyph_name in self.reverse_cmap:
                codepoint = self.reverse_cmap[glyph_name]
                return f"U+{codepoint:04X}"
            return f"glyph{int(glyph_name[5:])}"
        return glyph_name

    def get_supported_features(self) -> set[str]:
        """Return all feature tags supported by the font (GSUB and GPOS)."""
        tags = set()
        for table_tag in ["GSUB", "GPOS"]:
            if table_tag in self.font:
                table_obj = self.font[table_tag]
                if hasattr(table_obj, "table"):
                    table = table_obj.table
                    if hasattr(table, "FeatureList") and table.FeatureList:
                        for record in table.FeatureList.FeatureRecord:
                            tags.add(record.FeatureTag)
        return tags

    def get_figure_chars(self) -> str:
        """Return a string of all characters involved in figure substitutions."""
        fig_tags: set[str] = {"lnum", "onum", "pnum", "tnum"}
        chars: set[str] = set("0123456789")
        for tag in fig_tags:
            if tag in self.feature_substitutions:
                for sub in self.feature_substitutions[tag]:
                    for char in sub.input_chars:
                        chars.add(char)
        return "".join(sorted(chars))

    def analyze(self) -> None:
        """Analyze the font's GSUB table."""
        if "GSUB" not in self.font:
            return

        gsub = self.font["GSUB"].table
        feature_list = gsub.FeatureList.FeatureRecord
        lookup_list = gsub.LookupList.Lookup

        for feature_record in feature_list:
            tag = feature_record.FeatureTag
            if tag not in self.feature_substitutions:
                self.feature_substitutions[tag] = []

            for lookup_index in feature_record.Feature.LookupListIndex:
                if lookup_index < len(lookup_list):
                    lookup = lookup_list[lookup_index]
                    self.feature_substitutions[tag].extend(
                        self._process_lookup(lookup, tag)
                    )

        # Deduplicate and filter empty features
        cleaned_subs = {}
        for tag, subs in self.feature_substitutions.items():
            unique_subs = []
            seen = set()
            for sub in subs:
                if sub not in seen:
                    unique_subs.append(sub)
                    seen.add(sub)
            if unique_subs:
                cleaned_subs[tag] = unique_subs
        self.feature_substitutions = cleaned_subs

    def _process_lookup(self, lookup: Any, tag: str) -> list[Substitution]:  # noqa: ANN401
        substitutions = []
        if lookup.LookupType == LOOKUP_TYPE_EXTENSION:
            for subtable in lookup.SubTable:
                substitutions.extend(
                    self._process_subtable(
                        subtable.ExtensionLookupType, subtable.ExtSubTable, tag
                    )
                )
        else:
            for subtable in lookup.SubTable:
                substitutions.extend(
                    self._process_subtable(lookup.LookupType, subtable, tag)
                )
        return substitutions

    def _process_subtable(
        self,
        lookup_type: int,
        subtable: Any,  # noqa: ANN401
        tag: str,
    ) -> list[Substitution]:
        if lookup_type == LOOKUP_TYPE_SINGLE:
            return self._process_single_subtable(subtable, tag)
        if lookup_type == LOOKUP_TYPE_MULTIPLE:
            return self._process_multiple_subtable(subtable, tag)
        if lookup_type == LOOKUP_TYPE_LIGATURE:
            return self._process_ligature_subtable(subtable, tag)
        if lookup_type == LOOKUP_TYPE_ALTERNATE:
            return self._process_alternate_subtable(subtable, tag)
        return []

    def _process_single_subtable(
        self,
        subtable: Any,  # noqa: ANN401
        tag: str,
    ) -> list[Substitution]:
        substitutions = []
        if hasattr(subtable, "mapping"):
            for input_glyph, output_glyph in subtable.mapping.items():
                in_char, in_name = self.get_glyph_info(input_glyph)
                out_char, out_name = self.get_glyph_info(output_glyph)
                substitutions.append(
                    Substitution(
                        input_glyphs=self.get_display_name(in_name),
                        input_chars=in_char,
                        output_glyphs=self.get_display_name(out_name),
                        output_chars=out_char,
                        feature_tag=tag,
                    )
                )
        return substitutions

    def _process_multiple_subtable(
        self,
        subtable: Any,  # noqa: ANN401
        tag: str,
    ) -> list[Substitution]:
        substitutions = []
        if hasattr(subtable, "mapping"):
            for input_glyph, output_glyphs in subtable.mapping.items():
                in_char, in_name = self.get_glyph_info(input_glyph)
                out_info = [self.get_glyph_info(g) for g in output_glyphs]
                out_chars = "".join(c for c, n in out_info)
                out_names = " ".join(self.get_display_name(n) for c, n in out_info)
                substitutions.append(
                    Substitution(
                        input_glyphs=self.get_display_name(in_name),
                        input_chars=in_char,
                        output_glyphs=out_names,
                        output_chars=out_chars,
                        feature_tag=tag,
                    )
                )
        return substitutions

    def _process_ligature_subtable(
        self,
        subtable: Any,  # noqa: ANN401
        tag: str,
    ) -> list[Substitution]:
        substitutions = []
        if hasattr(subtable, "ligatures"):
            for first_glyph, ligatures in subtable.ligatures.items():
                for lig in ligatures:
                    components = [first_glyph, *lig.Component]
                    output = lig.LigGlyph
                    comp_info = [self.get_glyph_info(c) for c in components]
                    comp_chars = "".join(c for c, n in comp_info)
                    comp_names = [self.get_display_name(n) for c, n in comp_info]
                    out_char, out_name = self.get_glyph_info(output)
                    substitutions.append(
                        Substitution(
                            input_glyphs=" ".join(comp_names),
                            input_chars=comp_chars,
                            output_glyphs=self.get_display_name(out_name),
                            output_chars=out_char,
                            feature_tag=tag,
                        )
                    )
        return substitutions

    def _process_alternate_subtable(
        self,
        subtable: Any,  # noqa: ANN401
        tag: str,
    ) -> list[Substitution]:
        substitutions = []
        if hasattr(subtable, "alternates"):
            for input_glyph, alternates in subtable.alternates.items():
                in_char, in_name = self.get_glyph_info(input_glyph)
                # Just take the first alternate for simplicity in this report
                out_char, out_name = self.get_glyph_info(alternates[0])
                substitutions.append(
                    Substitution(
                        input_glyphs=self.get_display_name(in_name),
                        input_chars=in_char,
                        output_glyphs=self.get_display_name(out_name),
                        output_chars=out_char,
                        feature_tag=tag,
                    )
                )
        return substitutions

    def create_modified_font(self) -> Path:
        """Create a temporary font file with unmapped glyphs added to the cmap."""
        if not self.pua_map:
            return self.font_path

        font_copy = TTFont(str(self.font_path))
        for table in font_copy["cmap"].tables:
            if table.isUnicode():
                for name, char in self.pua_map.items():
                    table.cmap[ord(char)] = name

        with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as tmp:
            temp_path = Path(tmp.name)
        font_copy.save(str(temp_path))
        return temp_path


def _draw_unit_header(pdf: FPDF, x: float, y: float) -> None:
    """Draw the header for a single substitution unit."""
    pdf.set_xy(x, y)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(120)
    # combined column header ('off' / 'on') for one unit
    pdf.cell(COLUMN_UNIT_WIDTH, 6, "off", align="C")
    pdf.cell(COLUMN_UNIT_WIDTH, 6, "on", align="C")
    pdf.set_draw_color(220)
    pdf.line(x, y + 6, x + TOTAL_UNIT_WIDTH, y + 6)


def _draw_all_headers(pdf: FPDF, y: float, multi: bool) -> float:
    """Draw headers for one or two columns of substitutions."""
    _draw_unit_header(pdf, 10, y)
    if multi:
        _draw_unit_header(pdf, 10 + TOTAL_UNIT_WIDTH + GUTTER_WIDTH, y)
    return y + ROW_HEIGHT


def _render_compact_table(
    pdf: FPDF, subs: list[Substitution], tag: str, y: float
) -> float:
    """Render 1-to-1 mappings in a compact horizontal table."""
    cell_width = 13
    max_width = 190
    label_width = 15
    chars_per_row = int((max_width - label_width) / cell_width)

    for i in range(0, len(subs), chars_per_row):
        if y + 2 * ROW_HEIGHT + 5 > PAGE_BOTTOM_MARGIN:
            pdf.add_page()
            y = 15

        chunk = subs[i : i + chars_per_row]

        # Row 1: Off
        pdf.set_xy(10, y)
        pdf.set_text_color(120)
        pdf.set_font("Helvetica", "I", 7)
        pdf.cell(label_width, ROW_HEIGHT, "off:", align="L")

        pdf.set_text_color(0)
        pdf.set_font("TargetFont", "", 10)
        pdf.set_text_shaping(use_shaping_engine=False)
        for sub in chunk:
            pdf.cell(cell_width, ROW_HEIGHT, sub.input_chars, align="C")

        y += ROW_HEIGHT

        # Row 2: On
        pdf.set_xy(10, y)
        pdf.set_text_color(120)
        pdf.set_font("Helvetica", "I", 7)
        pdf.cell(label_width, ROW_HEIGHT, "on:", align="L")

        pdf.set_text_color(0)
        pdf.set_font("TargetFont", "", 10)
        pdf.set_text_shaping(use_shaping_engine=True, features={tag: True})
        for sub in chunk:
            pdf.cell(cell_width, ROW_HEIGHT, sub.input_chars, align="C")

        y += ROW_HEIGHT + 4  # Padding between chunks

    return y


def _render_figures_section(
    pdf: FPDF, analyzer: FontAnalyzer, supported: set[str], y: float
) -> float:
    """Render a combined section for figure feature combinations."""
    pdf.start_section("Figures", level=0)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(0)
    pdf.cell(0, 10, "Figures", ln=True)
    y = pdf.get_y()

    fig_chars = analyzer.get_figure_chars()

    v_options: list[str | None] = [None]
    if "lnum" in supported:
        v_options.append("lnum")
    if "onum" in supported:
        v_options.append("onum")

    h_options: list[str | None] = [None]
    if "pnum" in supported:
        h_options.append("pnum")
    if "tnum" in supported:
        h_options.append("tnum")

    for v in v_options:
        for h in h_options:
            if y + 10 > PAGE_BOTTOM_MARGIN:
                pdf.add_page()
                y = 15

            features = {}
            parts = []
            if v:
                features[v] = True
                parts.append(analyzer.get_tag_name(v))
            if h:
                features[h] = True
                parts.append(analyzer.get_tag_name(h))

            label = " + ".join(parts) if parts else "Default"

            pdf.set_xy(10, y)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(100)
            pdf.cell(50, 10, label, align="L")

            pdf.set_font("TargetFont", "", 12)
            pdf.set_text_color(0)
            pdf.set_text_shaping(use_shaping_engine=True, features=features)
            pdf.cell(0, 10, fig_chars, align="L")
            y += 10

    return y + 5


def _render_substitution_unit(pdf: FPDF, x: float, y: float, sub: Substitution) -> None:
    """Render a single substitution unit (input, off, on, output)."""
    pdf.set_xy(x, y)
    pdf.set_text_color(0)

    pdf.set_font("Helvetica", "", 7)
    pdf.cell(COLUMN_NAME_WIDTH, ROW_HEIGHT, sub.input_glyphs, align="L")

    pdf.set_font("TargetFont", "", 10)
    pdf.set_text_shaping(use_shaping_engine=False)
    pdf.cell(COLUMN_RENDER_WIDTH, ROW_HEIGHT, sub.input_chars, align="C")

    pdf.set_text_shaping(use_shaping_engine=True, features={sub.feature_tag: True})
    pdf.cell(COLUMN_RENDER_WIDTH, ROW_HEIGHT, sub.input_chars, align="C")

    pdf.set_text_shaping(use_shaping_engine=False)
    pdf.set_font("Helvetica", "", 7)
    pdf.cell(COLUMN_NAME_WIDTH, ROW_HEIGHT, sub.output_glyphs, align="R")


def render_pdf_report(
    analyzer: FontAnalyzer,
    output_path: Path,
    actual_font_path: Path,
) -> None:
    """Generate a PDF report of OpenType features."""
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    pdf.add_page()
    try:
        pdf.add_font("TargetFont", style="", fname=str(actual_font_path))
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, f"OpenType Feature Report: {analyzer.font_path.name}", ln=True)
        pdf.ln(5)
    except Exception as e:
        print(f"Warning: Could not embed font for PDF: {e}")
        return

    supported = analyzer.get_supported_features()
    fig_tags = {"lnum", "onum", "pnum", "tnum"}

    y = pdf.get_y()
    if supported & fig_tags:
        y = _render_figures_section(pdf, analyzer, supported, y)
        pdf.set_y(y)

    remaining_tags = [
        t for t in sorted(analyzer.feature_substitutions.keys()) if t not in fig_tags
    ]

    for tag in remaining_tags:
        subs = analyzer.feature_substitutions[tag]
        friendly_name = analyzer.get_tag_name(tag)

        if pdf.get_y() > PAGE_BREAK_THRESHOLD:
            pdf.add_page()

        pdf.start_section(f"{tag}: {friendly_name}", level=0)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(0)
        pdf.cell(0, 10, f"Feature: {tag} ({friendly_name})", ln=True)

        simple_subs = [s for s in subs if s.is_simple]
        complex_subs = [s for s in subs if not s.is_simple]

        y = pdf.get_y()

        if simple_subs:
            y = _render_compact_table(pdf, simple_subs, tag, y)

        if complex_subs:
            multi = len(complex_subs) > MIN_SUBS_FOR_MULTI_COLUMN
            y = _draw_all_headers(pdf, y, multi)

            for i in range(0, len(complex_subs), 2):
                if y + ROW_HEIGHT > PAGE_BOTTOM_MARGIN:
                    pdf.add_page()
                    y = 15
                    y = _draw_all_headers(pdf, y, multi)

                _render_substitution_unit(pdf, 10, y, complex_subs[i])

                if i + 1 < len(complex_subs):
                    _render_substitution_unit(
                        pdf,
                        10 + TOTAL_UNIT_WIDTH + GUTTER_WIDTH,
                        y,
                        complex_subs[i + 1],
                    )

                y += ROW_HEIGHT

        pdf.set_y(y + 5)

    try:
        pdf.output(str(output_path))
        print(f"\nPDF report saved to: {output_path}")
    except Exception as e:
        print(f"Error saving PDF: {e}")


def print_console_report(analyzer: FontAnalyzer) -> None:
    """Print a summary of OpenType features to the console."""
    for tag in sorted(analyzer.feature_substitutions.keys()):
        subs = analyzer.feature_substitutions[tag]
        friendly_name = analyzer.get_tag_name(tag)
        print(f"\nFeature: {tag} ({friendly_name})")
        for sub in subs:
            print(
                f"  {sub.input_glyphs} ({sub.input_chars}) -> "
                f"{sub.output_glyphs} ({sub.output_chars})"
            )


def main() -> None:
    """Execute the OpenType feature analysis tool."""
    parser = argparse.ArgumentParser(
        description="Analyze OpenType features in a font file."
    )
    parser.add_argument("font_path", help="Path to the .ttf or .otf font file")
    parser.add_argument("--output", "-o", help="Path to save the PDF report (optional)")
    args = parser.parse_args()

    font_path = Path(args.font_path)
    if not font_path.exists():
        print(f"Error: File not found: {font_path}")
        sys.exit(1)

    analyzer = FontAnalyzer(font_path)
    analyzer.analyze()

    if not analyzer.feature_substitutions:
        print("No OpenType features with substitutions found.")
        return

    print(f"Analyzing font: {font_path}")
    print("-" * 40)
    print_console_report(analyzer)

    if args.output:
        actual_font_path = analyzer.create_modified_font()
        try:
            render_pdf_report(analyzer, Path(args.output), actual_font_path)
        finally:
            if actual_font_path != font_path and actual_font_path.exists():
                actual_font_path.unlink()


if __name__ == "__main__":
    main()
