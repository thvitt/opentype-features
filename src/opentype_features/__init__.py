"""Analyze OpenType features in a font file and generate reports."""

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

from opentype_features.feature_names import FEATURE_NAMES, LANGUAGE_NAMES

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
ROW_HEIGHT = 8
PAGE_BOTTOM_MARGIN = 275
COLUMN_RENDER_WIDTH = 15

# Visual Difference Thresholds
DIFF_THRESHOLD_NONE = 0.0001  # Technical but no visual difference
DIFF_THRESHOLD_SMALL = 0.1  # Subtle visual difference


@dataclass(frozen=True)
class Substitution:
    """Represents a single OpenType substitution."""

    input_glyphs: str
    input_chars: str
    output_glyphs: str
    output_chars: str
    feature_tag: str
    languages: tuple[str, ...] = ()
    diff_total: float = 1.0
    diff_ratio: float = 1.0

    @property
    def has_one_to_one_chars(self) -> bool:
        """Check if this is a 1-to-1 character mapping."""
        return len(self.input_chars) == 1 and len(self.output_chars) == 1


class VisualComparator:
    """Compares visual renderings of characters with and without features."""

    def __init__(self, font_path: Path) -> None:
        """Initialize with font data."""
        self.font_path = font_path
        with font_path.open("rb") as f:
            self.font_data = f.read()
        self.face = hb.Face(self.font_data)  # type: ignore
        self.font = hb.Font(self.face)  # type: ignore
        self.size = 128
        self.pil_font = ImageFont.truetype(str(font_path), self.size)

    def get_difference_score(
        self, text: str, feature_tag: str, languages: tuple[str, ...] = ()
    ) -> tuple[float, float]:
        """
        Calculate difference scores between renderings.

        Returns:
            (total_diff, ratio_diff)
            total_diff: (changed pixels) / (total pixels)
            ratio_diff: (added + removed pixels) / (common pixels)

        """
        # First check if there's any technical difference
        if not self.is_visually_different(text, feature_tag, languages):
            return 0.0, 0.0

        # Then measure pixel difference for small vs large
        img_off, img_on = self._render_pair(text, feature_tag, languages)

        # Use numpy for pixel-wise difference
        mask_threshold = 128
        arr_off = np.array(img_off) > mask_threshold
        arr_on = np.array(img_on) > mask_threshold

        common = np.sum(arr_off & arr_on)
        only_off = np.sum(arr_off & ~arr_on)
        only_on = np.sum(~arr_off & arr_on)

        total_pixels = arr_off.size
        diff_total = float((only_off + only_on) / total_pixels)

        # Avoid division by zero
        if common > 0:
            diff_ratio = float((only_off + only_on) / common)
        else:
            diff_ratio = 1.0 if (only_off + only_on) > 0 else 0.0

        return diff_total, diff_ratio

    def get_diff_image(
        self, text: str, feature_tag: str, languages: tuple[str, ...] = ()
    ) -> Image.Image:
        """
        Create a visualization of differences between off and on renderings.

        - Common pixels: light gray
        - Pixels only in 'off': red
        - Pixels only in 'on': green
        """
        img_off, img_on = self._render_pair(text, feature_tag, languages)

        w, h = img_off.size

        # Threshold to binary masks for logic
        mask_threshold = 128
        pixel_max = 255

        mask_off = np.array(img_off) > mask_threshold
        mask_on = np.array(img_on) > mask_threshold

        # Create RGB image initialized to white
        diff_arr = np.full((h, w, 3), pixel_max, dtype=np.uint8)

        # Common pixels (light gray)
        common = mask_off & mask_on
        diff_arr[common] = [200, 200, 200]

        # Pixels only in 'off' (red)
        only_off = mask_off & ~mask_on
        diff_arr[only_off] = [pixel_max, 0, 0]

        # Pixels only in 'on' (green)
        only_on = ~mask_off & mask_on
        diff_arr[only_on] = [0, 200, 0]

        return Image.fromarray(diff_arr)

    def _render_pair(
        self, text: str, feature_tag: str, languages: tuple[str, ...] = ()
    ) -> tuple[Image.Image, Image.Image]:
        """Render both off and on states aligned to the same baseline and fixed height."""
        # Use a very wide canvas initially
        w_large = self.size * (len(text) + 2) * 2
        h_large = self.size * 4
        baseline_y = self.size * 2

        img_off = Image.new("L", (w_large, h_large), 0)
        img_on = Image.new("L", (w_large, h_large), 0)

        # Render Off
        draw_off = ImageDraw.Draw(img_off)
        draw_off.text(
            (self.size, baseline_y), text, font=self.pil_font, fill=255, anchor="ls"
        )

        # Render On
        draw_on = ImageDraw.Draw(img_on)
        draw_on.text(
            (self.size, baseline_y),
            text,
            font=self.pil_font,
            fill=255,
            features=[feature_tag],
            anchor="ls",
        )

        # Get metrics for vertical cropping
        ascent, descent = self.pil_font.getmetrics()
        # Crop to a fixed height around the baseline
        # Add 10% padding
        padding = int(self.size * 0.1)
        y_start = baseline_y - ascent - padding
        y_end = baseline_y + descent + padding

        # Find horizontal bounds
        bbox_off = img_off.getbbox()
        bbox_on = img_on.getbbox()

        if bbox_off is None and bbox_on is None:
            return (
                Image.new("L", (1, y_end - y_start), 0),
                Image.new("L", (1, y_end - y_start), 0),
            )

        # Ensure we have both boxes for calculations
        final_bbox_off = bbox_off
        if final_bbox_off is None:
            final_bbox_off = bbox_on

        final_bbox_on = bbox_on
        if final_bbox_on is None:
            final_bbox_on = bbox_off

        # final_bbox_off and final_bbox_on are now guaranteed not None
        # because of the initial check
        assert final_bbox_off is not None
        assert final_bbox_on is not None

        x_start = min(final_bbox_off[0], final_bbox_on[0]) - padding
        x_end = max(final_bbox_off[2], final_bbox_on[2]) + padding

        common_bbox = (x_start, y_start, x_end, y_end)

        return img_off.crop(common_bbox), img_on.crop(common_bbox)

    def is_visually_different(
        self, text: str, feature_tag: str, languages: tuple[str, ...] = ()
    ) -> bool:
        """
        Check if rendering with features is visually different from without.

        Returns:
            True if there's any difference in glyph IDs or positions.

        """
        # Explicitly disable common defaults in 'off' state to ensure we see the change
        off_features = {"liga": False, "calt": False, "kern": False}
        on_features = {**off_features, feature_tag: True}

        buf_off = hb.Buffer()  # type: ignore
        buf_off.add_str(text)
        buf_off.guess_segment_properties()
        # Set language if available (use the first one for comparison)
        if languages:
            _script, lang = (
                languages[0].split("/")
                if "/" in languages[0]
                else ("latn", languages[0])
            )
            buf_off.language = lang

        hb.shape(self.font, buf_off, off_features)  # type: ignore

        buf_on = hb.Buffer()  # type: ignore
        buf_on.add_str(text)
        buf_on.guess_segment_properties()
        if languages:
            _script, lang = (
                languages[0].split("/")
                if "/" in languages[0]
                else ("latn", languages[0])
            )
            buf_on.language = lang

        hb.shape(self.font, buf_on, on_features)  # type: ignore

        info_off = buf_off.glyph_infos
        info_on = buf_on.glyph_infos

        if len(info_off) != len(info_on):
            return True

        for i in range(len(info_off)):
            if info_off[i].codepoint != info_on[i].codepoint:
                return True

        pos_off = buf_off.glyph_positions
        pos_on = buf_on.glyph_positions
        for i in range(len(pos_off)):
            if (
                pos_off[i].x_advance != pos_on[i].x_advance
                or pos_off[i].x_offset != pos_on[i].x_offset
                or pos_off[i].y_advance != pos_on[i].y_advance
                or pos_off[i].y_offset != pos_on[i].y_offset
            ):
                return True

        return False


class FontAnalyzer:
    """Analyzes a font for OpenType features and generates reports."""

    def __init__(self, font_path: Path) -> None:
        """Initialize the FontAnalyzer."""
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

    def get_language_name(self, lang_tag: str) -> str:
        """Return the friendly name for an OpenType language tag."""
        # Handle tags like "latn/TRK"
        tag = lang_tag
        if "/" in lang_tag:
            _script, tag = lang_tag.split("/")
        return LANGUAGE_NAMES.get(tag, tag)

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
        # Start with standard digits
        fig_chars: list[str] = list("0123456789")
        extra_chars: set[str] = set()

        for tag in fig_tags:
            if tag in self.feature_substitutions:
                for sub in self.feature_substitutions[tag]:
                    for char in sub.input_chars:
                        # Only include if it's not a standard digit and not a PUA character
                        if "0" <= char <= "9":
                            continue
                        if ord(char) < PUA_START:
                            extra_chars.add(char)

        return "".join(fig_chars) + "".join(sorted(extra_chars))

    def _get_feature_lookup_contexts(
        self,
        script_list: list[Any],
        feature_list: list[Any],
    ) -> dict[tuple[str, int], set[str]]:
        """Map (feature_tag, lookup_index) to set of (script, lang) contexts."""
        contexts: dict[tuple[str, int], set[str]] = {}
        for script_record in script_list:
            script_tag = script_record.ScriptTag
            script = script_record.Script

            lang_systems = []
            if script.DefaultLangSys:
                lang_systems.append(("dflt", script.DefaultLangSys))
            for lang_sys_record in script.LangSysRecord:
                lang_systems.append(
                    (lang_sys_record.LangSysTag, lang_sys_record.LangSys)
                )

            for lang_tag, lang_sys in lang_systems:
                for feature_index in lang_sys.FeatureIndex:
                    feature_record = feature_list[feature_index]
                    tag = feature_record.FeatureTag
                    for lookup_index in feature_record.Feature.LookupListIndex:
                        key = (tag, lookup_index)
                        if key not in contexts:
                            contexts[key] = set()
                        contexts[key].add(f"{script_tag}/{lang_tag}")
        return contexts

    def analyze(self) -> None:
        """Analyze the font's GSUB table."""
        if "GSUB" not in self.font:
            return

        gsub = self.font["GSUB"].table
        lookup_list = gsub.LookupList.Lookup

        # (feature_tag, lookup_index) -> set of (script, lang)
        feature_lookup_contexts = self._get_feature_lookup_contexts(
            gsub.ScriptList.ScriptRecord, gsub.FeatureList.FeatureRecord
        )

        # Collect and combine substitutions
        # (tag, in_chars, out_chars) -> Substitution
        result_map: dict[tuple[str, str, str], Substitution] = {}

        for (tag, lookup_index), contexts in feature_lookup_contexts.items():
            if lookup_index < len(lookup_list):
                lookup = lookup_list[lookup_index]
                lookup_subs = self._process_lookup(lookup, tag)
                languages = tuple(sorted(contexts))
                for sub in lookup_subs:
                    key = (sub.feature_tag, sub.input_chars, sub.output_chars)
                    if key not in result_map:
                        result_map[key] = Substitution(
                            input_glyphs=sub.input_glyphs,
                            input_chars=sub.input_chars,
                            output_glyphs=sub.output_glyphs,
                            output_chars=sub.output_chars,
                            feature_tag=sub.feature_tag,
                            languages=languages,
                        )
                    else:
                        existing = result_map[key]
                        new_langs = tuple(sorted(set(existing.languages + languages)))
                        result_map[key] = Substitution(
                            input_glyphs=existing.input_glyphs,
                            input_chars=existing.input_chars,
                            output_glyphs=existing.output_glyphs,
                            output_chars=existing.output_chars,
                            feature_tag=existing.feature_tag,
                            languages=new_langs,
                        )

        # Populate self.feature_substitutions
        self.feature_substitutions = {}
        for sub in result_map.values():
            if sub.feature_tag not in self.feature_substitutions:
                self.feature_substitutions[sub.feature_tag] = []
            self.feature_substitutions[sub.feature_tag].append(sub)

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


def _render_compact_table(
    pdf: FPDF,
    subs: list[Substitution],
    tag: str,
    y: float,
    analyzer: FontAnalyzer,
    comparator: VisualComparator,
) -> float:
    """Render mappings in a compact horizontal table."""
    max_width = 190
    label_width = 15

    i = 0
    while i < len(subs):
        chunk, i = _get_next_sub_chunk(subs, i, max_width - label_width)
        if not chunk:
            break

        # We now have 3 rows: off, on, diff
        # Check if we need a page break
        rows_to_render = 3
        total_chunk_height = rows_to_render * ROW_HEIGHT + 5

        # Extra height for labels if needed
        if any(s.feature_tag == "locl" and s.languages for s, w in chunk):
            total_chunk_height += 3

        if y + total_chunk_height > PAGE_BOTTOM_MARGIN:
            pdf.add_page()
            y = 15

        y = _render_chunk_row(
            pdf,
            chunk,
            "off",
            False,
            tag,
            y,
            label_width,
            analyzer,
            comparator,
            mode="off",
        )
        y = _render_chunk_row(
            pdf, chunk, "on", True, tag, y, label_width, analyzer, comparator, mode="on"
        )
        y = _render_chunk_row(
            pdf,
            chunk,
            "diff",
            True,
            tag,
            y,
            label_width,
            analyzer,
            comparator,
            mode="diff",
        )
        y += 4  # Padding between chunks

    return y


def _get_next_sub_chunk(
    subs: list[Substitution], start_index: int, available_width: float
) -> tuple[list[tuple[Substitution, float]], int]:
    """Calculate how many substitutions fit in the current row."""
    chunk = []
    current_row_width: float = 0
    i = start_index
    while i < len(subs):
        sub = subs[i]
        # Estimate width: 10 units base + 5 per extra char
        sub_width = 10 + (max(len(sub.input_chars), len(sub.output_chars)) - 1) * 5
        if current_row_width + sub_width > available_width:
            if not chunk:  # Single very long sub
                chunk.append((sub, sub_width))
                i += 1
            break
        chunk.append((sub, sub_width))
        current_row_width += sub_width
        i += 1
    return chunk, i


def _render_chunk_row(
    pdf: FPDF,
    chunk: list[tuple[Substitution, float]],
    label: str,
    shaping: bool,
    tag: str,
    y: float,
    label_width: float,
    analyzer: FontAnalyzer,
    comparator: VisualComparator,
    mode: str = "off",
) -> float:
    """Render a single row (off, on, or diff) for a chunk of substitutions."""
    pdf.set_xy(10, y)
    pdf.set_text_color(120)
    pdf.set_font("Helvetica", "I", 7)
    pdf.cell(label_width, ROW_HEIGHT, f"{label}:", align="L")

    pdf.set_font("TargetFont", "", 10)

    for sub, width in chunk:
        x_pos = pdf.get_x()
        if mode == "diff":
            # Render diff bitmap ONLY in diff mode
            diff_img = comparator.get_diff_image(sub.input_chars, tag, sub.languages)

            # Constant resolution: scale based on fixed pixel height
            # Font height in pixels: ascent + descent + 20% padding
            ascent, descent = comparator.pil_font.getmetrics()
            total_px_h = ascent + descent + int(comparator.size * 0.2)

            # Target height in PDF (mm)
            target_mm_h = ROW_HEIGHT - 2
            scale = target_mm_h / total_px_h

            img_h = target_mm_h
            img_w = diff_img.width * scale

            # Constrain width if it exceeds cell width
            if img_w > width - 2:
                img_w = width - 2
                img_h = (diff_img.height / diff_img.width) * img_w

            pdf.image(
                diff_img, x=x_pos + (width - img_w) / 2, y=y + 1, w=img_w, h=img_h
            )
            # Advance X position
            pdf.set_xy(x_pos + width, y)
        elif mode == "on":
            if sub.diff_total < DIFF_THRESHOLD_SMALL:
                # Subtle difference overlay: off (light gray) + on (red)
                pdf.set_text_color(220)
                pdf.set_text_shaping(use_shaping_engine=False)
                pdf.cell(width, ROW_HEIGHT, sub.input_chars, align="C")

                pdf.set_xy(x_pos, y)
                pdf.set_text_color(200, 50, 50)
                pdf.set_text_shaping(use_shaping_engine=True, features={tag: True})
                pdf.cell(width, ROW_HEIGHT, sub.input_chars, align="C")
            else:
                # Normal 'on' rendering
                pdf.set_text_color(0)
                pdf.set_text_shaping(use_shaping_engine=True, features={tag: True})
                pdf.cell(width, ROW_HEIGHT, sub.input_chars, align="C")

            # Score label
            current_y = pdf.get_y()
            pdf.set_xy(x_pos, current_y + ROW_HEIGHT - 2)
            pdf.set_font("Helvetica", "I", 3)
            pdf.set_text_color(180)
            score_text = f"t:{sub.diff_total:.1%} r:{sub.diff_ratio:.1%}"
            pdf.cell(width, 2, score_text, align="C")
            pdf.set_xy(x_pos + width, current_y)
            pdf.set_font("TargetFont", "", 10)
            pdf.set_text_color(0)

        else:
            # Mode 'off': Normal rendering
            pdf.set_text_color(0)
            pdf.set_text_shaping(use_shaping_engine=False)
            pdf.cell(width, ROW_HEIGHT, sub.input_chars, align="C")

        if mode == "on" and sub.feature_tag == "locl" and sub.languages:
            _draw_locl_labels(pdf, x_pos, y, width, sub.languages, analyzer)

    return y + ROW_HEIGHT


def _draw_locl_labels(
    pdf: FPDF,
    x: float,
    y: float,
    width: float,
    languages: tuple[str, ...],
    analyzer: FontAnalyzer,
) -> None:
    """Draw small language labels below localized substitutions."""
    pdf.set_xy(x, y + ROW_HEIGHT - 2)
    pdf.set_font("Helvetica", "I", 4)
    pdf.set_text_color(150)
    langs = ",".join(analyzer.get_language_name(lang) for lang in languages)
    pdf.cell(width, 2, langs, align="C")
    # Position will be restored by the caller if needed, but we mostly care about text flow
    pdf.set_xy(x + width, y)
    pdf.set_font("TargetFont", "", 10)
    pdf.set_text_color(0)


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


def get_feature_priority(tag: str) -> tuple[int, str]:
    """Return a sorting priority for feature tags."""
    # Priority groups:
    # 10: Figures
    # 20: Standard Ligatures
    # 21: Other Ligatures
    # 30: Stylistic Alternates
    # 40: Stylistic Sets
    # 50: Character Variants
    # 60: General/Remaining
    # 70: Smaller/Shifted

    priority = 60  # Default: General

    if tag in {"lnum", "onum", "pnum", "tnum"}:
        priority = 10
    elif tag == "liga":
        priority = 20
    elif tag in {"clig", "dlig", "hlig", "rlig"}:
        priority = 21
    elif tag == "salt":
        priority = 30
    elif tag.startswith("ss") and tag[2:].isdigit():
        priority = 40
    elif tag.startswith("cv") and tag[2:].isdigit():
        priority = 50
    elif tag in {
        "smcp",
        "c2sc",
        "pcap",
        "c2pc",
        "unic",
        "ordn",
        "numr",
        "dnom",
        "frac",
        "afrc",
        "sinf",
        "sups",
        "subs",
    }:
        priority = 70

    return (priority, tag)


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
    comparator = VisualComparator(analyzer.font_path)

    y = pdf.get_y()
    if supported & fig_tags:
        y = _render_figures_section(pdf, analyzer, supported, y)
        pdf.set_y(y)

    remaining_tags = sorted(
        [t for t in analyzer.feature_substitutions if t not in fig_tags],
        key=get_feature_priority,
    )

    for tag in remaining_tags:
        # Calculate differences and filter
        scored_subs = []
        for s in analyzer.feature_substitutions[tag]:
            diff_total, diff_ratio = comparator.get_difference_score(
                s.input_chars, tag, s.languages
            )
            if diff_total > DIFF_THRESHOLD_NONE:
                scored_subs.append(
                    Substitution(
                        input_glyphs=s.input_glyphs,
                        input_chars=s.input_chars,
                        output_glyphs=s.output_glyphs,
                        output_chars=s.output_chars,
                        feature_tag=s.feature_tag,
                        languages=s.languages,
                        diff_total=diff_total,
                        diff_ratio=diff_ratio,
                    )
                )

        if not scored_subs:
            continue

        subs = scored_subs
        friendly_name = analyzer.get_tag_name(tag)

        if pdf.get_y() > PAGE_BREAK_THRESHOLD:
            pdf.add_page()

        pdf.start_section(f"{tag}: {friendly_name}", level=0)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(0)
        pdf.cell(0, 10, f"Feature: {tag} ({friendly_name})", ln=True)

        y = pdf.get_y()
        y = _render_compact_table(pdf, subs, tag, y, analyzer, comparator)
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
