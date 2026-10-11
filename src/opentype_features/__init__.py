"""Analyze OpenType features in a font file and generate reports."""

import argparse
import functools
import logging
import math
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import uharfbuzz as hb
from fontFeatures import Chaining, RoutineReference, Substitution
from fontFeatures.ttLib import unparse
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from fpdf import FPDF, XPos, YPos
from PIL import Image, ImageDraw, ImageFont

from opentype_features.feature_names import FEATURE_NAMES, LANGUAGE_NAMES

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from fontFeatures import Routine, Rule

# Private Use Area for rendering unmapped glyphs
# Use Supplementary Private Use Area-A to avoid 0xFFFF boundary
PUA_START = 0xF0000

# PDF Layout Constants
HEADING_HEIGHT = 10
ROW_HEIGHT = 8
PAGE_BOTTOM_MARGIN = 275
ROW_LABEL_WIDTH = 15
CELL_LABEL_HEIGHT = 3

# Visual Difference Thresholds
DIFF_THRESHOLD_NONE = 0.0001  # Technical but no visual difference
DIFF_THRESHOLD_SMALL = 0.1  # Subtle visual difference
SUBTLE_RATIO = 0.01  # Relative difference hidden by --hide-subtle
RENDER_CACHE_SIZE = 1024

# Minimum number of inputs mapping to the same output to be shown as one entry
MIN_GROUP_SIZE = 4

# Applied by script-specific shapers depending on the context, e.g. Arabic joining
SHAPER_CONTEXT_FEATURES = frozenset(
    [
        "isol",
        "init",
        "medi",
        "fina",
        "med2",
        "fin2",
        "fin3",
        "nukt",
        "akhn",
        "rphf",
        "rkrf",
        "pref",
        "blwf",
        "abvf",
        "half",
        "pstf",
        "vatu",
        "cjct",
        "pres",
        "abvs",
        "blws",
        "psts",
        "haln",
        "ljmo",
        "vjmo",
        "tjmo",
    ]
)

# Default features disabled in both renderings so that their effect becomes visible
BASELINE_FEATURES = {"liga": False, "calt": False, "kern": False}


@dataclass(frozen=True)
class FeatureExample:
    """An input text whose rendering an OpenType feature changes."""

    input_glyphs: str
    input_chars: str
    output_glyphs: str
    output_chars: str
    feature_tag: str
    languages: tuple[str, ...] = ()
    diff_total: float = 1.0
    diff_ratio: float = 1.0
    feature_value: int = 1
    # Further substitutions with inputs of the same length and the same output
    input_variants: tuple[FeatureExample, ...] = ()

    @property
    def all_input_chars(self) -> list[str]:
        """Return the input of this substitution and of its folded variants."""
        return [self.input_chars, *(v.input_chars for v in self.input_variants)]

    @property
    def input_alternatives(self) -> list[list[str]] | None:
        """Return the characters per position if the inputs are all combinations."""
        inputs = self.all_input_chars
        positions = [list(dict.fromkeys(chars)) for chars in zip(*inputs, strict=True)]
        if math.prod(len(chars) for chars in positions) == len(inputs):
            return positions
        return None

    @property
    def differs_only_in_case(self) -> bool:
        """Check whether the inputs are all letter case combinations of one text."""
        positions = self.input_alternatives
        return positions is not None and all(
            len({char.casefold() for char in chars}) == 1 for chars in positions
        )

    @property
    def input_pattern(self) -> str:
        """Return the input, with bracketed alternatives for folded variants."""
        positions = self.input_alternatives
        if not self.input_variants or positions is None or self.differs_only_in_case:
            return self.input_chars
        return "".join(
            chars[0] if len(chars) == 1 else f"[{''.join(chars)}]"
            for chars in positions
        )

    @property
    def input_summary(self) -> str:
        """Describe the folded input variants, if any."""
        if not self.input_variants:
            return ""
        summary = f"{len(self.all_input_chars)} inputs"
        if self.differs_only_in_case:
            summary += ", any case"
        return summary


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
        # Scoring and the diff row of the report need the same renderings
        self._masks = functools.lru_cache(maxsize=RENDER_CACHE_SIZE)(self._render_masks)

    def get_difference_score(
        self,
        text: str,
        feature_tag: str,
        languages: tuple[str, ...] = (),
        feature_value: int = 1,
    ) -> tuple[float, float]:
        """
        Calculate difference scores between renderings.

        Returns:
            (total_diff, ratio_diff)
            total_diff: (changed pixels) / (total pixels)
            ratio_diff: (added + removed pixels) / (common pixels)

        """
        # First check if there's any technical difference
        if not self.is_visually_different(text, feature_tag, languages, feature_value):
            return 0.0, 0.0

        arr_off, arr_on = self._masks(text, feature_tag, languages, feature_value)

        common = np.sum(arr_off & arr_on)
        only_off = np.sum(arr_off & ~arr_on)
        only_on = np.sum(~arr_off & arr_on)

        total_pixels = arr_off.size
        diff_total = float((only_off + only_on) / total_pixels)

        # New ratio: (added + removed) / (all non-white pixels)
        # all non-white = only_off + only_on + common
        total_foreground = only_off + only_on + common
        if total_foreground > 0:
            diff_ratio = float((only_off + only_on) / total_foreground)
        else:
            diff_ratio = 0.0

        return diff_total, diff_ratio

    def get_diff_image(
        self,
        text: str,
        feature_tag: str,
        languages: tuple[str, ...] = (),
        feature_value: int = 1,
    ) -> Image.Image:
        """
        Create a visualization of differences between off and on renderings.

        - Common pixels: light gray
        - Pixels only in 'off': red
        - Pixels only in 'on': green
        """
        mask_off, mask_on = self._masks(text, feature_tag, languages, feature_value)
        pixel_max = 255

        # Create RGB image initialized to white
        diff_arr = np.full((*mask_off.shape, 3), pixel_max, dtype=np.uint8)

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

    def _render_masks(
        self,
        text: str,
        feature_tag: str,
        languages: tuple[str, ...],
        feature_value: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return the inked pixels of the off and on renderings."""
        img_off, img_on = self._render_pair(text, feature_tag, languages, feature_value)
        mask_threshold = 128
        return np.array(img_off) > mask_threshold, np.array(img_on) > mask_threshold

    def _render_pair(
        self,
        text: str,
        feature_tag: str,
        languages: tuple[str, ...] = (),
        feature_value: int = 1,
    ) -> tuple[Image.Image, Image.Image]:
        """Render both off and on states aligned to the same baseline and fixed height."""
        # Use a very wide canvas initially
        w_large = self.size * (len(text) + 2) * 2
        h_large = self.size * 4
        baseline_y = self.size * 2

        img_off = Image.new("L", (w_large, h_large), 0)
        img_on = Image.new("L", (w_large, h_large), 0)

        baseline = [f"-{tag}" for tag in BASELINE_FEATURES]
        language = shaping_language(languages)

        # Render Off
        draw_off = ImageDraw.Draw(img_off)
        draw_off.text(
            (self.size, baseline_y),
            text,
            font=self.pil_font,
            fill=255,
            features=[*baseline, f"-{feature_tag}"],
            language=language,
            anchor="ls",
        )

        # Render On
        draw_on = ImageDraw.Draw(img_on)
        draw_on.text(
            (self.size, baseline_y),
            text,
            font=self.pil_font,
            fill=255,
            features=[*baseline, f"{feature_tag}={feature_value}"],
            language=language,
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

    def _shape(
        self, text: str, features: Mapping[str, bool | int], language: str | None
    ) -> hb.Buffer:  # type: ignore
        buf = hb.Buffer()  # type: ignore
        buf.add_str(text)
        buf.guess_segment_properties()
        if language:
            buf.language = language
        hb.shape(self.font, buf, features)  # type: ignore
        return buf

    def is_visually_different(
        self,
        text: str,
        feature_tag: str,
        languages: tuple[str, ...] = (),
        feature_value: int = 1,
    ) -> bool:
        """
        Check if rendering with features is visually different from without.

        Returns:
            True if there's any difference in glyph IDs or positions.

        """
        off_features = {**BASELINE_FEATURES, feature_tag: False}
        on_features = {**BASELINE_FEATURES, feature_tag: feature_value}

        language = shaping_language(languages)
        buf_off = self._shape(text, off_features, language)
        buf_on = self._shape(text, on_features, language)

        return not _same_shaping(buf_off, buf_on)

    def is_applied_by_default(
        self,
        text: str,
        feature_tag: str,
        languages: tuple[str, ...] = (),
        other_tags: Iterable[str] = (),
    ) -> bool:
        """
        Check whether HarfBuzz applies the feature to text without being asked.

        The other_tags are disabled so that they cannot take over the feature's
        work, e.g. liga forming a ligature that clig would form as well.
        """
        language = shaping_language(languages)
        others_disabled = dict.fromkeys(other_tags, False)
        buf_default = self._shape(text, others_disabled, language)
        buf_disabled = self._shape(
            text, {**others_disabled, feature_tag: False}, language
        )
        return not _same_shaping(buf_default, buf_disabled)


def _same_shaping(buf_a: hb.Buffer, buf_b: hb.Buffer) -> bool:  # type: ignore
    """Check whether two shaped buffers contain the same glyphs at the same positions."""
    glyphs_a = [info.codepoint for info in buf_a.glyph_infos]
    glyphs_b = [info.codepoint for info in buf_b.glyph_infos]
    positions_a = [_position_tuple(pos) for pos in buf_a.glyph_positions]
    positions_b = [_position_tuple(pos) for pos in buf_b.glyph_positions]
    return glyphs_a == glyphs_b and positions_a == positions_b


def _position_tuple(pos: hb.GlyphPosition) -> tuple[int, int, int, int]:  # type: ignore
    return pos.x_advance, pos.y_advance, pos.x_offset, pos.y_offset


class FontAnalyzer:
    """Analyzes a font for OpenType features and generates reports."""

    def __init__(self, font_path: Path) -> None:
        """Initialize the FontAnalyzer."""
        self.font_path = font_path
        self.font = TTFont(str(font_path))
        self.reverse_cmap = self._build_reverse_cmap()
        self.pua_map: dict[str, str] = {}
        self.feature_examples: dict[str, list[FeatureExample]] = {}
        self.custom_feature_names: dict[str, str] = {}
        self.variant_names: dict[str, list[str]] = {}
        self.tags_with_alternates: set[str] = set()
        self.default_features: set[str] = set()
        self.contextual_default_features: set[str] = set()
        self._read_feature_params()

    def _build_reverse_cmap(self) -> dict[str, int]:
        cmap = self.font.getBestCmap()
        if cmap is None:
            return {}
        return {v: k for k, v in cmap.items()}

    def _read_feature_params(self) -> None:
        """Extract custom feature and variant names from the GSUB feature parameters."""
        if "GSUB" not in self.font:
            return

        gsub = self.font["GSUB"].table
        if not hasattr(gsub, "FeatureList") or not gsub.FeatureList:
            return

        for feature_record in gsub.FeatureList.FeatureRecord:
            tag = feature_record.FeatureTag
            params = feature_record.Feature.FeatureParams
            if not params:
                continue

            # Stylistic sets use UINameID, character variants FeatUILabelNameID
            label_id = getattr(params, "UINameID", None) or getattr(
                params, "FeatUILabelNameID", None
            )
            if label_id and (name := self._get_name(label_id)):
                self.custom_feature_names[tag] = name

            first_param_id = getattr(params, "FirstParamUILabelNameID", 0)
            if first_param_id:
                self.variant_names[tag] = [
                    self._get_name(first_param_id + i) or ""
                    for i in range(params.NumNamedParameters)
                ]

    def _get_name(self, name_id: int) -> str | None:
        """Look up a string in the name table, preferring English entries."""
        name_table = self.font["name"]
        name_rec = name_table.getName(name_id, 3, 1, 0x409) or name_table.getName(
            name_id, 1, 0, 0
        )

        # Fallback: search all names for this ID
        if not name_rec:
            name_rec = next(
                (record for record in name_table.names if record.nameID == name_id),
                None,
            )

        return name_rec.toUnicode() if name_rec else None

    def detect_default_features(self, comparator: VisualComparator) -> None:
        """
        Determine which features the shaper applies without being asked.

        Features applied to at least half of their substitutions count as on by
        default, others applied to some (e.g. frac around U+2044 FRACTION SLASH)
        or applied by script-specific shapers (e.g. Arabic init/medi/fina) as on
        by default in some contexts.
        """
        self.default_features = set()
        self.contextual_default_features = set()
        for tag, subs in self.feature_examples.items():
            other_tags = self.feature_examples.keys() - {tag}
            applied = sum(
                comparator.is_applied_by_default(
                    sub.input_chars, tag, sub.languages, other_tags
                )
                for sub in subs
            )
            if applied * 2 >= len(subs):
                self.default_features.add(tag)
            elif applied or tag in SHAPER_CONTEXT_FEATURES:
                self.contextual_default_features.add(tag)

    def get_feature_heading(self, tag: str) -> str:
        """Return the tag, its friendly name, and whether it is on by default."""
        heading = f"{tag} ({self.get_tag_name(tag)})"
        if tag in self.default_features:
            heading += ", on by default"
        elif tag in self.contextual_default_features:
            heading += ", on by default in some contexts"
        return heading

    def get_tag_name(self, tag: str) -> str:
        """Return the friendly name for an OpenType feature tag."""
        return self.custom_feature_names.get(tag, FEATURE_NAMES.get(tag, tag))

    def get_variant_label(self, sub: FeatureExample) -> str:
        """Return a label for the alternate an alternate substitution selects."""
        names = self.variant_names.get(sub.feature_tag, [])
        index = sub.feature_value - 1
        if index < len(names) and names[index]:
            return names[index]
        if sub.feature_tag in self.tags_with_alternates:
            return f"#{sub.feature_value}"
        return ""

    def get_cell_label(self, sub: FeatureExample) -> str:
        """Return the small label shown above a substitution in the report."""
        if sub.feature_tag == "locl" and sub.languages:
            label = ",".join(self.get_language_name(lang) for lang in sub.languages)
        else:
            label = self.get_variant_label(sub)
        return ", ".join(part for part in (label, sub.input_summary) if part)

    def get_language_name(self, lang_tag: str) -> str:
        """Return the friendly name for an OpenType language tag."""
        # Handle tags like "latn/TRK"
        tag = lang_tag
        if "/" in lang_tag:
            _script, tag = lang_tag.split("/")
        return LANGUAGE_NAMES.get(tag, tag)

    def get_char(self, glyph_name: str) -> str:
        """Return the character rendering a glyph, assigning a PUA one if unmapped."""
        if glyph_name in self.reverse_cmap:
            return chr(self.reverse_cmap[glyph_name])

        if glyph_name not in self.pua_map:
            self.pua_map[glyph_name] = chr(PUA_START + len(self.pua_map))

        return self.pua_map[glyph_name]

    def get_display_name(self, glyph_name: str) -> str:
        """Generate a display name for a glyph following user rules."""
        if glyph_name.startswith("glyph") and glyph_name[5:].isdigit():
            if glyph_name in self.reverse_cmap:
                codepoint = self.reverse_cmap[glyph_name]
                return f"U+{codepoint:04X}"
            return f"glyph{int(glyph_name[5:])}"
        return glyph_name

    def get_supported_features(self) -> set[str]:
        """Return all feature tags supported by the font."""
        return set(self.feature_examples.keys())

    def get_figure_chars(self) -> str:
        """Return a string of all characters involved in figure substitutions."""
        fig_tags: set[str] = {"lnum", "onum", "pnum", "tnum"}
        # Start with standard digits
        fig_chars: list[str] = list("0123456789")
        extra_chars: set[str] = set()

        for tag in fig_tags:
            if tag in self.feature_examples:
                for sub in self.feature_examples[tag]:
                    for char in sub.input_chars:
                        # Only include if it's not a standard digit and not a PUA character
                        if "0" <= char <= "9":
                            continue
                        if ord(char) < PUA_START:
                            extra_chars.add(char)

        return "".join(fig_chars) + "".join(sorted(extra_chars))

    def analyze(self) -> None:
        """Analyze the font's features using fontFeatures."""
        ff = unparse(self.font)

        # (tag, in_chars, out_chars) -> FeatureExample
        result_map: dict[tuple[str, str, str], FeatureExample] = {}

        for tag, routine_refs in ff.features.items():
            for ref in routine_refs:
                routine = ref.routine
                langs = tuple(
                    sorted(
                        f"{script.strip()}/{lang.strip()}"
                        for script, lang in routine.languages
                    )
                )

                for rule in routine.rules:
                    try:
                        self._process_rule(rule, tag, langs, result_map)
                    except Exception as error:
                        logger.warning("Skipping a rule of feature %s: %r", tag, error)

        self.feature_examples = {}
        for sub in result_map.values():
            if sub.feature_tag not in self.feature_examples:
                self.feature_examples[sub.feature_tag] = []
            self.feature_examples[sub.feature_tag].append(sub)

    def _process_rule(
        self,
        rule: Rule,
        tag: str,
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
    ) -> None:
        if isinstance(rule, Substitution):
            self._process_ff_substitution(rule, tag, langs, result_map)
        elif isinstance(rule, Chaining):
            self._process_ff_chaining(rule, tag, langs, result_map)

    def _process_ff_substitution(
        self,
        rule: Substitution,
        tag: str,
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
    ) -> None:
        """Process a standard fontFeatures FeatureExample rule."""
        if _is_alternate_substitution(rule):
            self._process_ff_alternates(rule, tag, langs, result_map)
            return

        # Ligature components and multiple-substitution classes use their first glyph
        first_glyphs = [glyph_class[0] for glyph_class in rule.input]
        for glyph in rule.input[0] if len(rule.input) == 1 else first_glyphs[:1]:
            self._add_to_result_map(
                tag,
                [glyph, *first_glyphs[1:]],
                _replacement_for(rule, glyph),
                langs,
                result_map,
            )

    def _process_ff_alternates(
        self,
        rule: Substitution,
        tag: str,
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
    ) -> None:
        """Add one substitution per alternate, selected by the feature value."""
        self.tags_with_alternates.add(tag)
        for value, out_glyph in enumerate(rule.replacement[0], start=1):
            self._add_to_result_map(
                tag,
                [rule.input[0][0]],
                [out_glyph],
                langs,
                result_map,
                feature_value=value,
            )

    def _process_ff_chaining(
        self,
        rule: Chaining,
        tag: str,
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
    ) -> None:
        """Process a fontFeatures Chaining rule, shown with its context."""
        precontext = [glyph_class[0] for glyph_class in rule.precontext]
        postcontext = [glyph_class[0] for glyph_class in rule.postcontext]

        for in_seq in _expand_chain_inputs(rule):
            out_seq = _apply_chain_lookups(rule.lookups, in_seq)
            if out_seq != in_seq:
                self._add_to_result_map(
                    tag,
                    precontext + in_seq + postcontext,
                    precontext + out_seq + postcontext,
                    langs,
                    result_map,
                )

    def _add_to_result_map(
        self,
        tag: str,
        in_glyphs: list[str],
        out_glyphs: list[str],
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
        feature_value: int = 1,
    ) -> None:
        """Add or combine a substitution in the result map."""
        in_chars = "".join(self.get_char(g) for g in in_glyphs)
        out_chars = "".join(self.get_char(g) for g in out_glyphs)
        key = (tag, in_chars, out_chars)
        if key not in result_map:
            result_map[key] = FeatureExample(
                input_glyphs=" ".join(self.get_display_name(g) for g in in_glyphs),
                input_chars=in_chars,
                output_glyphs=" ".join(self.get_display_name(g) for g in out_glyphs),
                output_chars=out_chars,
                feature_tag=tag,
                languages=langs,
                feature_value=feature_value,
            )
        else:
            existing = result_map[key]
            new_langs = tuple(sorted(set(existing.languages + langs)))
            result_map[key] = replace(existing, languages=new_langs)

    def create_modified_font(self) -> Path:
        """Create a temporary font file with unmapped glyphs added to the cmap."""
        if not self.pua_map:
            return self.font_path

        font_copy = TTFont(str(self.font_path))
        pua_cmap = {ord(char): name for name, char in self.pua_map.items()}
        full_cmap = (font_copy.getBestCmap() or {}) | pua_cmap

        # The PUA lies beyond the BMP, which only a format 12 subtable can address
        cmap_table = font_copy["cmap"]
        cmap_table.tables = [t for t in cmap_table.tables if t.format != 12]  # noqa: PLR2004
        subtable = CmapSubtable.newSubtable(12)
        subtable.platformID, subtable.platEncID, subtable.language = 3, 10, 0
        subtable.cmap = full_cmap
        cmap_table.tables.append(subtable)

        with tempfile.NamedTemporaryFile(
            suffix=self.font_path.suffix, delete=False
        ) as tmp:
            temp_path = Path(tmp.name)
        font_copy.save(str(temp_path))
        return temp_path


def _render_compact_table(
    pdf: FPDF,
    subs: list[FeatureExample],
    y: float,
    analyzer: FontAnalyzer,
    comparator: VisualComparator,
    show_diff: bool = True,
) -> float:
    """Render mappings in a compact horizontal table."""
    max_width = 190

    i = 0
    while i < len(subs):
        chunk, i = _get_next_sub_chunk(subs, i, max_width - ROW_LABEL_WIDTH)
        cell_labels = [analyzer.get_cell_label(sub) for sub, _width in chunk]
        has_labels = any(cell_labels)

        if y + _chunk_height(show_diff, has_labels) > PAGE_BOTTOM_MARGIN:
            pdf.add_page()
            y = 15

        y = _render_chunk_row(pdf, chunk, "off", y, comparator, show_diff)
        if has_labels:
            y = _render_cell_labels(pdf, chunk, cell_labels, y)
        y = _render_chunk_row(pdf, chunk, "on", y, comparator, show_diff)
        if show_diff:
            y = _render_chunk_row(pdf, chunk, "diff", y, comparator, show_diff)
        y += 4  # Padding between chunks

    return y


def _chunk_height(show_diff: bool, has_labels: bool) -> float:
    """Return the height of one chunk of the compact table, including padding."""
    rows_to_render = 3 if show_diff else 2
    return rows_to_render * ROW_HEIGHT + 4 + (CELL_LABEL_HEIGHT if has_labels else 0)


def _get_next_sub_chunk(
    subs: list[FeatureExample], start_index: int, available_width: float
) -> tuple[list[tuple[FeatureExample, float]], int]:
    """Calculate how many substitutions fit in the current row."""
    chunk = []
    current_row_width: float = 0
    i = start_index
    while i < len(subs):
        sub = subs[i]
        # Estimate width: 10 units base + 5 per extra char
        sub_width = 10 + (max(len(sub.input_pattern), len(sub.output_chars)) - 1) * 5
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
    chunk: list[tuple[FeatureExample, float]],
    mode: Literal["off", "on", "diff"],
    y: float,
    comparator: VisualComparator,
    show_diff: bool,
) -> float:
    """Render a single row (off, on, or diff) for a chunk of substitutions."""
    pdf.set_xy(10, y)
    pdf.set_text_color(120)
    pdf.set_font("Helvetica", "I", 7)
    pdf.cell(ROW_LABEL_WIDTH, ROW_HEIGHT, f"{mode}:", align="L")

    pdf.set_font("TargetFont", "", 10)

    for sub, width in chunk:
        tag = sub.feature_tag
        x_pos = pdf.get_x()
        if mode == "diff":
            # Render diff bitmap ONLY in diff mode
            diff_img = comparator.get_diff_image(
                sub.input_chars, tag, sub.languages, sub.feature_value
            )

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
            if show_diff and sub.diff_total < DIFF_THRESHOLD_SMALL:
                # Subtle difference overlay: off (light gray) + on (red)
                pdf.set_text_color(220)
                pdf.set_text_shaping(use_shaping_engine=False)
                pdf.cell(width, ROW_HEIGHT, sub.input_chars, align="C")

                pdf.set_xy(x_pos, y)
                pdf.set_text_color(200, 50, 50)
                pdf.set_text_shaping(
                    use_shaping_engine=True,
                    features={**BASELINE_FEATURES, tag: sub.feature_value},  # type: ignore[bad-argument-type]
                    language=shaping_language(sub.languages),
                )
                pdf.cell(width, ROW_HEIGHT, sub.input_chars, align="C")
            else:
                # Normal 'on' rendering
                pdf.set_text_color(0)
                pdf.set_text_shaping(
                    use_shaping_engine=True,
                    features={**BASELINE_FEATURES, tag: sub.feature_value},  # type: ignore[bad-argument-type]
                    language=shaping_language(sub.languages),
                )
                pdf.cell(width, ROW_HEIGHT, sub.input_chars, align="C")

            # Score label
            if show_diff:
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
            pdf.cell(width, ROW_HEIGHT, sub.input_pattern, align="C")

    return y + ROW_HEIGHT


def _render_cell_labels(
    pdf: FPDF, chunk: list[tuple[FeatureExample, float]], labels: list[str], y: float
) -> float:
    """Render small labels above the substitutions, e.g. their languages or variant."""
    label_size = 4
    pdf.set_text_color(150)
    x: float = 10 + ROW_LABEL_WIDTH
    for (_sub, width), text in zip(chunk, labels, strict=True):
        pdf.set_font("Helvetica", "I", label_size)
        text_width = pdf.get_string_width(text)
        if text_width > width - 1:
            pdf.set_font_size(label_size * (width - 1) / text_width)
        pdf.set_xy(x, y)
        pdf.cell(width, CELL_LABEL_HEIGHT, text, align="C")
        x += width
    pdf.set_text_color(0)
    return y + CELL_LABEL_HEIGHT


def _render_figures_section(
    pdf: FPDF, analyzer: FontAnalyzer, supported: set[str]
) -> float:
    """Render a combined section for figure feature combinations."""
    pdf.start_section("Figures", level=0)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(0)
    pdf.cell(0, 10, "Figures", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
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


def shaping_language(languages: tuple[str, ...]) -> str | None:
    """
    Return a HarfBuzz language selecting the OpenType language system of a rule.

    Rules that also apply to the default language need no explicit language.
    """
    lang_tags = [entry.rpartition("/")[2] for entry in languages]
    if not lang_tags or "dflt" in lang_tags:
        return None
    return f"x-hbot{lang_tags[0]}"


def is_character_variant(tag: str) -> bool:
    """Check whether a feature tag denotes a character variant (cv01–cv99)."""
    return tag.startswith("cv") and tag[2:].isdigit()


def _is_alternate_substitution(rule: Substitution) -> bool:
    """Check whether a rule offers several alternates for a single glyph."""
    return (
        len(rule.input) == 1
        and len(rule.replacement) == 1
        and len(rule.input[0]) == 1
        and len(rule.replacement[0]) > 1
    )


def _replacement_for(rule: Substitution, first_glyph: str) -> list[str]:
    """Return the glyphs a substitution produces for an input starting with first_glyph."""
    input_class = rule.input[0]
    if (
        len(rule.input) == 1
        and len(rule.replacement) == 1
        and len(rule.replacement[0]) == len(input_class)
    ):
        return [rule.replacement[0][input_class.index(first_glyph)]]
    return [glyph_class[0] for glyph_class in rule.replacement]


def _match_substitution(
    routine: Routine, glyphs: list[str]
) -> tuple[int, list[str]] | None:
    """Return the input length and output of the routine's rule matching glyphs."""
    for rule in routine.rules:
        if not isinstance(rule, Substitution) or len(rule.input) > len(glyphs):
            continue
        window = glyphs[: len(rule.input)]
        if all(g in cls for g, cls in zip(window, rule.input, strict=True)):
            return len(rule.input), _replacement_for(rule, glyphs[0])
    return None


def _expand_chain_inputs(rule: Chaining) -> list[list[str]]:
    """Return input sequences covering each glyph of the first substituted class."""
    first_glyphs = [glyph_class[0] for glyph_class in rule.input]
    position = next((i for i, lookups in enumerate(rule.lookups) if lookups), None)
    if position is None:
        return [first_glyphs]
    return [
        [*first_glyphs[:position], glyph, *first_glyphs[position + 1 :]]
        for glyph in rule.input[position]
    ]


def _apply_chain_lookups(
    lookups: list[list[RoutineReference | Routine] | None], in_seq: list[str]
) -> list[str]:
    """Apply the lookups of a chaining rule to an input sequence."""
    parts = [[glyph] for glyph in in_seq]
    for i, position_lookups in enumerate(lookups):
        for lookup in position_lookups or []:
            if len(parts[i]) != 1:  # consumed by a ligature or multiplied
                break
            routine = lookup.routine if isinstance(lookup, RoutineReference) else lookup
            if routine is None:
                continue
            if match := _match_substitution(routine, [*parts[i], *in_seq[i + 1 :]]):
                length, replacement = match
                parts[i : i + length] = [replacement] + [[]] * (length - 1)
    return [glyph for part in parts for glyph in part]


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
    elif is_character_variant(tag):
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


def group_examples(subs: list[FeatureExample]) -> list[FeatureExample]:
    """Fold many equally long inputs producing the same output into one entry."""
    groups: dict[tuple[str, int, int], list[FeatureExample]] = {}
    for sub in subs:
        key = (sub.output_chars, sub.feature_value, len(sub.input_chars))
        groups.setdefault(key, []).append(sub)

    grouped = []
    for sub in subs:
        group = groups[sub.output_chars, sub.feature_value, len(sub.input_chars)]
        if len(group) < MIN_GROUP_SIZE:
            grouped.append(sub)
        elif sub is group[0]:
            grouped.append(replace(sub, input_variants=tuple(group[1:])))
    return grouped


def score_examples(
    subs: list[FeatureExample], comparator: VisualComparator, min_ratio: float = 0.0
) -> list[FeatureExample]:
    """Return the visibly changing substitutions, annotated with their scores."""
    scored = []
    for sub in subs:
        try:
            diff_total, diff_ratio = comparator.get_difference_score(
                sub.input_chars, sub.feature_tag, sub.languages, sub.feature_value
            )
        except Exception:
            logger.exception(
                "Could not compare %s for feature %s", sub.input_glyphs, sub.feature_tag
            )
            continue
        if diff_total > DIFF_THRESHOLD_NONE and diff_ratio >= min_ratio:
            scored.append(replace(sub, diff_total=diff_total, diff_ratio=diff_ratio))
    return scored


def render_pdf_report(
    analyzer: FontAnalyzer,
    output_path: Path,
    actual_font_path: Path,
    show_diff: bool = True,
    min_ratio: float = 0.0,
) -> None:
    """Generate a PDF report of OpenType features."""
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    pdf.add_page()
    try:
        pdf.add_font("TargetFont", style="", fname=str(actual_font_path))
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(
            0,
            10,
            f"OpenType Feature Report: {analyzer.font_path.name}",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
    except Exception:
        logger.exception("Could not embed %s in the PDF report", actual_font_path)
        return

    pdf.set_font("Helvetica", "", 9)
    for label, tags in [
        ("On by default", analyzer.default_features),
        ("On by default in some contexts", analyzer.contextual_default_features),
    ]:
        if tags:
            pdf.cell(
                0,
                5,
                f"{label}: {', '.join(sorted(tags))}",
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )
    pdf.ln(5)

    supported = analyzer.get_supported_features()
    fig_tags = {"lnum", "onum", "pnum", "tnum"}
    comparator = VisualComparator(actual_font_path)

    if supported & fig_tags:
        y = _render_figures_section(pdf, analyzer, supported)
        pdf.set_y(y)

    remaining_tags = sorted(
        [t for t in analyzer.feature_examples if t not in fig_tags],
        key=get_feature_priority,
    )

    for tag in remaining_tags:
        try:
            _render_feature(pdf, analyzer, comparator, tag, show_diff, min_ratio)
        except Exception:
            logger.exception("Could not render feature %s", tag)

    try:
        pdf.output(str(output_path))
        print(f"\nPDF report saved to: {output_path}")
    except Exception:
        logger.exception("Could not save the PDF report to %s", output_path)


def _render_feature(
    pdf: FPDF,
    analyzer: FontAnalyzer,
    comparator: VisualComparator,
    tag: str,
    show_diff: bool,
    min_ratio: float,
) -> None:
    """Render the section of a single feature."""
    subs = group_examples(
        score_examples(analyzer.feature_examples[tag], comparator, min_ratio)
    )
    if subs:
        friendly_name = analyzer.get_tag_name(tag)

        # Keep the heading together with the first chunk of its table
        first_chunk_height = _chunk_height(show_diff, has_labels=True)
        if pdf.get_y() + HEADING_HEIGHT + first_chunk_height > PAGE_BOTTOM_MARGIN:
            pdf.add_page()

        pdf.start_section(f"{tag}: {friendly_name}", level=0)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(0)
        pdf.cell(
            0,
            HEADING_HEIGHT,
            f"Feature: {analyzer.get_feature_heading(tag)}",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )

        y = _render_compact_table(
            pdf, subs, pdf.get_y(), analyzer, comparator, show_diff=show_diff
        )
        pdf.set_y(y + 5)


def print_console_report(
    analyzer: FontAnalyzer,
    comparator: VisualComparator | None = None,
    min_ratio: float = 0.0,
) -> None:
    """
    Print a summary of OpenType features to the console.

    With a comparator, substitutions without a relative visual difference of at
    least min_ratio are left out.
    """
    for tag in sorted(analyzer.feature_examples.keys()):
        subs = analyzer.feature_examples[tag]
        if comparator:
            subs = score_examples(subs, comparator, min_ratio)
            if not subs:
                continue
        print(f"\nFeature: {analyzer.get_feature_heading(tag)}")
        for sub in group_examples(subs):
            notes = [analyzer.get_variant_label(sub), sub.input_summary]
            if sub.input_variants and sub.input_alternatives is None:
                notes.append(
                    "also: " + "; ".join(v.input_glyphs for v in sub.input_variants)
                )
            note_text = ", ".join(note for note in notes if note)
            print(
                f"  {sub.input_glyphs} ({sub.input_pattern}) -> "
                f"{sub.output_glyphs} ({sub.output_chars})"
                + (f" [{note_text}]" if note_text else "")
            )


def main() -> None:
    """Execute the OpenType feature analysis tool."""
    parser = argparse.ArgumentParser(
        description="Analyze OpenType features in a font file."
    )
    parser.add_argument("font_path", help="Path to the .ttf or .otf font file")
    parser.add_argument("--output", "-o", help="Path to save the PDF report (optional)")
    parser.add_argument(
        "--no-diff",
        action="store_true",
        help="Disable visual difference information in PDF",
    )
    parser.add_argument(
        "--min-ratio",
        type=float,
        default=0.0,
        help="Skip glyph pairs with ratio diff score below this value (default: 0.0)",
    )
    parser.add_argument(
        "--hide-subtle",
        action="store_true",
        help=f"Skip glyph pairs with ratio diff score below {SUBTLE_RATIO}",
    )
    args = parser.parse_args()
    min_ratio = (
        max(args.min_ratio, SUBTLE_RATIO) if args.hide_subtle else args.min_ratio
    )
    logging.basicConfig(format="%(levelname)s: %(message)s")
    logging.captureWarnings(capture=True)

    font_path = Path(args.font_path)
    if not font_path.exists():
        logger.error("File not found: %s", font_path)
        sys.exit(1)

    analyzer = FontAnalyzer(font_path)
    analyzer.analyze()

    if not analyzer.feature_examples:
        print("No OpenType features with substitutions found.")
        return

    actual_font_path = analyzer.create_modified_font()
    try:
        comparator = VisualComparator(actual_font_path)
        analyzer.detect_default_features(comparator)

        print(f"Analyzing font: {font_path}")
        print("-" * 40)
        print_console_report(analyzer, comparator if min_ratio else None, min_ratio)

        if args.output:
            render_pdf_report(
                analyzer,
                Path(args.output),
                actual_font_path,
                show_diff=not args.no_diff,
                min_ratio=min_ratio,
            )
    finally:
        if actual_font_path != font_path and actual_font_path.exists():
            actual_font_path.unlink()


if __name__ == "__main__":
    main()
