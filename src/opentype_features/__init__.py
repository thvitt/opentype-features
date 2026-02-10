"""Analyze OpenType features in a font file and generate reports."""

import argparse
import sys
import tempfile
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


def get_tag_name(tag: str) -> str:
    """Return the friendly name for an OpenType feature tag."""
    return FEATURE_NAMES.get(tag, tag)


def get_glyph_info(
    glyph_name: str, reverse_cmap: dict[str, int], pua_map: dict[str, str]
) -> tuple[str, str]:
    """Return the character and glyph name for a given glyph name."""
    if glyph_name in reverse_cmap:
        return chr(reverse_cmap[glyph_name]), glyph_name

    # If not in cmap, use a PUA character for rendering
    if glyph_name not in pua_map:
        pua_char = chr(PUA_START + len(pua_map))
        pua_map[glyph_name] = pua_char

    return pua_map[glyph_name], glyph_name


def get_display_name(glyph_name: str, reverse_cmap: dict[str, int]) -> str:
    """Generate a display name for a glyph following user rules."""
    # If fontTools generated a generic name (e.g., glyph00499)
    if glyph_name.startswith("glyph") and glyph_name[5:].isdigit():
        if glyph_name in reverse_cmap:
            codepoint = reverse_cmap[glyph_name]
            return f"U+{codepoint:04X}"
        # No name, no unicode -> glyph number (normalized)
        return f"glyph{int(glyph_name[5:])}"
    # It has a specific name
    return glyph_name


def _process_single_subtable(
    subtable: Any,  # noqa: ANN401
    tag: str,
    reverse_cmap: dict[str, int],
    pua_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Process a single substitution subtable."""
    substitutions = []
    if hasattr(subtable, "mapping"):
        for input_glyph, output_glyph in subtable.mapping.items():
            in_char, in_name = get_glyph_info(input_glyph, reverse_cmap, pua_map)
            out_char, out_name = get_glyph_info(output_glyph, reverse_cmap, pua_map)

            substitutions.append(
                {
                    "in_chars": in_char,
                    "in_glyphs": get_display_name(in_name, reverse_cmap),
                    "out_chars": out_char,
                    "out_glyphs": get_display_name(out_name, reverse_cmap),
                    "tag": tag,
                }
            )
    return substitutions


def _process_multiple_subtable(
    subtable: Any,  # noqa: ANN401
    tag: str,
    reverse_cmap: dict[str, int],
    pua_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Process a multiple substitution subtable."""
    substitutions = []
    if hasattr(subtable, "mapping"):
        for input_glyph, output_glyphs in subtable.mapping.items():
            in_char, in_name = get_glyph_info(input_glyph, reverse_cmap, pua_map)
            out_info = [
                get_glyph_info(g, reverse_cmap, pua_map) for g in output_glyphs
            ]
            out_chars = "".join(c for c, n in out_info)
            out_names = " ".join(get_display_name(n, reverse_cmap) for c, n in out_info)
            substitutions.append(
                {
                    "in_chars": in_char,
                    "in_glyphs": get_display_name(in_name, reverse_cmap),
                    "out_chars": out_chars,
                    "out_glyphs": out_names,
                    "tag": tag,
                }
            )
    return substitutions


def _process_ligature_subtable(
    subtable: Any,  # noqa: ANN401
    tag: str,
    reverse_cmap: dict[str, int],
    pua_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Process a ligature substitution subtable."""
    substitutions = []
    if hasattr(subtable, "ligatures"):
        for first_glyph, ligatures in subtable.ligatures.items():
            for lig in ligatures:
                components = [first_glyph, *lig.Component]
                output = lig.LigGlyph
                comp_info = [
                    get_glyph_info(c, reverse_cmap, pua_map) for c in components
                ]

                comp_chars = "".join(c for c, n in comp_info)
                comp_names = [get_display_name(n, reverse_cmap) for c, n in comp_info]
                out_char, out_name = get_glyph_info(output, reverse_cmap, pua_map)

                substitutions.append(
                    {
                        "in_chars": comp_chars,
                        "in_glyphs": " ".join(comp_names),
                        "out_chars": out_char,
                        "out_glyphs": get_display_name(out_name, reverse_cmap),
                        "tag": tag,
                    }
                )
    return substitutions


def _process_alternate_subtable(
    subtable: Any,  # noqa: ANN401
    tag: str,
    reverse_cmap: dict[str, int],
    pua_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Process an alternate substitution subtable."""
    substitutions = []
    if hasattr(subtable, "alternates"):
        for input_glyph, alternates in subtable.alternates.items():
            in_char, in_name = get_glyph_info(input_glyph, reverse_cmap, pua_map)
            out_char, out_name = get_glyph_info(alternates[0], reverse_cmap, pua_map)
            substitutions.append(
                {
                    "in_chars": in_char,
                    "in_glyphs": get_display_name(in_name, reverse_cmap),
                    "out_chars": out_char,
                    "out_glyphs": get_display_name(out_name, reverse_cmap),
                    "tag": tag,
                }
            )
    return substitutions


def process_subtable(
    lookup_type: int,
    subtable: Any,  # noqa: ANN401
    tag: str,
    reverse_cmap: dict[str, int],
    pua_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Extract substitutions from a GSUB subtable."""
    if lookup_type == LOOKUP_TYPE_SINGLE:
        return _process_single_subtable(subtable, tag, reverse_cmap, pua_map)
    if lookup_type == LOOKUP_TYPE_MULTIPLE:
        return _process_multiple_subtable(subtable, tag, reverse_cmap, pua_map)
    if lookup_type == LOOKUP_TYPE_LIGATURE:
        return _process_ligature_subtable(subtable, tag, reverse_cmap, pua_map)
    if lookup_type == LOOKUP_TYPE_ALTERNATE:
        return _process_alternate_subtable(subtable, tag, reverse_cmap, pua_map)
    return []


def process_lookup(
    lookup: Any,  # noqa: ANN401
    tag: str,
    reverse_cmap: dict[str, int],
    pua_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Extract substitutions from a GSUB lookup."""
    substitutions = []
    if lookup.LookupType == LOOKUP_TYPE_EXTENSION:
        for subtable in lookup.SubTable:
            ext_lookup_type = subtable.ExtensionLookupType
            actual_subtable = subtable.ExtSubTable
            substitutions.extend(
                process_subtable(
                    ext_lookup_type, actual_subtable, tag, reverse_cmap, pua_map
                )
            )
    else:
        for subtable in lookup.SubTable:
            substitutions.extend(
                process_subtable(lookup.LookupType, subtable, tag, reverse_cmap, pua_map)
            )
    return substitutions


def create_modified_font(font_path: Path, pua_map: dict[str, str]) -> Path:
    """Create a temporary font file with unmapped glyphs added to the cmap."""
    font = TTFont(str(font_path))
    # Add mappings to all Unicode cmaps found
    for table in font["cmap"].tables:
        if table.isUnicode():
            for name, char in pua_map.items():
                table.cmap[ord(char)] = name

    # Save to a temporary file
    with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as tmp:
        temp_path = Path(tmp.name)
    font.save(str(temp_path))
    return temp_path


def render_pdf_report(
    font_path: Path,
    feature_substitutions: dict[str, list[dict[str, Any]]],
    output_path: Path,
    actual_font_path: Path,
) -> None:
    """Generate a PDF report of OpenType features."""
    pdf = FPDF()
    pdf.add_page()
    try:
        pdf.add_font("TargetFont", style="", fname=str(actual_font_path))
        pdf.set_font("TargetFont", "", 16)
        pdf.cell(0, 10, f"OpenType Feature Report: {font_path.name}", ln=True)
        pdf.ln(5)
    except Exception as e:
        print(f"Warning: Could not embed font for PDF: {e}")
        return

    for tag in sorted(feature_substitutions.keys()):
        all_substitutions = feature_substitutions[tag]
        friendly_name = get_tag_name(tag)

        # Deduplicate substitutions
        unique_subs = []
        seen = set()
        for s in all_substitutions:
            s_tuple = (s["in_glyphs"], s["out_glyphs"], s["tag"])
            if s_tuple not in seen:
                unique_subs.append(s)
                seen.add(s_tuple)
        all_substitutions = unique_subs

        pdf.set_font("TargetFont", "", 12)
        pdf.cell(0, 10, f"Feature: {tag} ({friendly_name})", ln=True)
        pdf.set_font("TargetFont", "", 10)

        if all_substitutions:
            w = [45, 35, 35, 45]
            pdf.cell(w[0], 8, "Source Glyphs", border=1)
            pdf.cell(w[1], 8, "OFF (Render)", border=1)
            pdf.cell(w[2], 8, "ON (Render)", border=1)
            pdf.cell(w[3], 8, "Target Glyph(s)", border=1)
            pdf.ln()

            for s in all_substitutions:
                try:
                    pdf.cell(w[0], 10, s["in_glyphs"], border=1)
                    pdf.set_text_shaping(use_shaping_engine=False)
                    pdf.cell(w[1], 10, s["in_chars"], border=1)
                    pdf.set_text_shaping(use_shaping_engine=True, features={s["tag"]: True})
                    pdf.cell(w[2], 10, s["in_chars"], border=1)
                    pdf.set_text_shaping(use_shaping_engine=False)
                    pdf.cell(w[3], 10, s["out_glyphs"], border=1)
                    pdf.ln()
                except Exception as e:
                    print(f"Error rendering row: {e}")
            pdf.ln(5)
        else:
            pdf.cell(0, 10, "No simple substitutions found for this feature.", ln=True)
            pdf.ln()

    try:
        pdf.output(str(output_path))
        print(f"\nPDF report saved to: {output_path}")
    except Exception as e:
        print(f"Error saving PDF: {e}")


def print_console_report(feature_substitutions: dict[str, list[dict[str, Any]]]) -> None:
    """Print a summary of OpenType features to the console."""
    for tag in sorted(feature_substitutions.keys()):
        all_substitutions = feature_substitutions[tag]
        friendly_name = get_tag_name(tag)

        unique_subs = []
        seen = set()
        for s in all_substitutions:
            s_tuple = (s["in_glyphs"], s["out_glyphs"], s["tag"])
            if s_tuple not in seen:
                unique_subs.append(s)
                seen.add(s_tuple)

        if unique_subs:
            print(f"\nFeature: {tag} ({friendly_name})")
            for s in unique_subs:
                print(
                    f"  {s['in_glyphs']} ({s['in_chars']}) -> "
                    f"{s['out_glyphs']} ({s['out_chars']})"
                )


def main() -> None:
    """Execute the OpenType feature analysis tool."""
    parser = argparse.ArgumentParser(description="Analyze OpenType features in a font file.")
    parser.add_argument("font_path", help="Path to the .ttf or .otf font file")
    parser.add_argument("--output", "-o", help="Path to save the PDF report (optional)")
    args = parser.parse_args()

    font_path = Path(args.font_path)
    if not font_path.exists():
        print(f"Error: File not found: {font_path}")
        sys.exit(1)

    try:
        font = TTFont(str(font_path))
    except Exception as e:
        print(f"Error loading font: {e}")
        sys.exit(1)

    if "GSUB" not in font:
        print("No GSUB table found. This font might not have OpenType features.")
        return

    gsub = font["GSUB"].table
    feature_list = gsub.FeatureList.FeatureRecord
    lookup_list = gsub.LookupList.Lookup

    cmap = font.getBestCmap()
    reverse_cmap = {v: k for k, v in cmap.items()}
    pua_map: dict[str, str] = {}

    print(f"Analyzing font: {font_path}")
    print("-" * 40)

    feature_substitutions: dict[str, list[dict[str, Any]]] = {}
    for feature_record in feature_list:
        tag = feature_record.FeatureTag
        if tag not in feature_substitutions:
            feature_substitutions[tag] = []

        for lookup_index in feature_record.Feature.LookupListIndex:
            if lookup_index < len(lookup_list):
                lookup = lookup_list[lookup_index]
                feature_substitutions[tag].extend(
                    process_lookup(lookup, tag, reverse_cmap, pua_map)
                )

    print_console_report(feature_substitutions)

    if args.output:
        actual_font_path = font_path
        if pua_map:
            actual_font_path = create_modified_font(font_path, pua_map)

        try:
            render_pdf_report(
                font_path, feature_substitutions, Path(args.output), actual_font_path
            )
        finally:
            if actual_font_path != font_path and actual_font_path.exists():
                actual_font_path.unlink()


if __name__ == "__main__":
    main()
