"""Analyze OpenType features in a font file and generate reports."""

import contextlib
import functools
import itertools
import logging
import math
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import uharfbuzz as hb
from fontFeatures import (
    Attachment,
    Chaining,
    Positioning,
    RoutineReference,
    Substitution,
)
from fontFeatures.ttLib import unparse
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from PIL import Image, ImageDraw, ImageFont

from opentype_features.feature_names import FEATURE_NAMES, LANGUAGE_NAMES

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from fontFeatures import FontFeatures, Routine, Rule

# Private Use Area for rendering unmapped glyphs
# Use Supplementary Private Use Area-A to avoid 0xFFFF boundary
PUA_START = 0xF0000


# Visual Difference Thresholds
DIFF_THRESHOLD_NONE = 0.0001  # Technical but no visual difference
SUBTLE_RATIO = 0.01  # Relative difference hidden by --hide-subtle
RENDER_CACHE_SIZE = 1024

# Minimum number of inputs mapping to the same output to be shown as one entry
MIN_GROUP_SIZE = 4

FIGURE_TAGS = frozenset({"lnum", "onum", "pnum", "tnum"})

# Positioning features such as kerning have thousands of rules, keep the strongest
MAX_POSITIONING_EXAMPLES = 60

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
    # Further examples with inputs of the same length and the same output
    input_variants: tuple[FeatureExample, ...] = ()
    # Positioning examples move glyphs instead of replacing them
    positioning: bool = False
    # Main value of a positioning adjustment in font units, e.g. the kerning
    adjustment: int = 0
    # Alternates of aalt that no other feature produces and no character encodes
    aalt_only: bool = False

    @property
    def has_unencoded_input(self) -> bool:
        """Check whether the input contains glyphs without a Unicode mapping."""
        return any(ord(char) >= PUA_START for char in self.input_chars)

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
        # Number of positioning examples of features where only the strongest are kept
        self.positioning_totals: dict[str, int] = {}
        self.contextual_default_features: set[str] = set()
        self._read_feature_params()

    def _build_reverse_cmap(self) -> dict[str, int]:
        cmap = self.font.getBestCmap()
        if cmap is None:
            return {}
        # Controls and surrogates cannot be shaped or embedded, use the PUA instead
        return {
            glyph: codepoint
            for codepoint, glyph in cmap.items()
            if unicodedata.category(chr(codepoint)) not in {"Cc", "Cs"}
        }

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
            if label_id and (name := self.get_name(label_id)):
                self.custom_feature_names[tag] = name

            first_param_id = getattr(params, "FirstParamUILabelNameID", 0)
            if first_param_id:
                self.variant_names[tag] = [
                    self.get_name(first_param_id + i) or ""
                    for i in range(params.NumNamedParameters)
                ]

    def get_name(self, name_id: int) -> str | None:
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

        return name_rec.toUnicode(errors="replace") if name_rec else None

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
        """Return the small label shown above an example in the report."""
        if sub.feature_tag == "locl" and sub.languages:
            label = ",".join(self.get_language_name(lang) for lang in sub.languages)
        else:
            label = self.get_variant_label(sub)
        adjustment = f"{sub.adjustment:+d}" if sub.adjustment else ""
        return ", ".join(
            part for part in (label, sub.input_summary, adjustment) if part
        )

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
            self.feature_examples.setdefault(sub.feature_tag, []).append(sub)

        self.positioning_totals = {}
        for tag, examples in self.feature_examples.items():
            positioning = [e for e in examples if e.positioning]
            if len(positioning) > MAX_POSITIONING_EXAMPLES:
                self.positioning_totals[tag] = len(positioning)
                # Prefer what can be typed, then the largest adjustments
                strongest = sorted(
                    positioning,
                    key=lambda e: (e.has_unencoded_input, -abs(e.adjustment)),
                )
                self.feature_examples[tag] = [
                    *(e for e in examples if not e.positioning),
                    *strongest[:MAX_POSITIONING_EXAMPLES],
                ]

        if "aalt" in self.feature_examples:
            self._mark_aalt_only(ff)

    def _mark_aalt_only(self, ff: FontFeatures) -> None:
        """Flag the aalt examples whose alternates are reachable through aalt only."""
        reachable = {
            self.pua_map[glyph]
            for glyph in _substitution_outputs(ff, exclude_tag="aalt")
            if glyph in self.pua_map
        }
        self.feature_examples["aalt"] = [
            replace(
                example,
                aalt_only=all(
                    ord(char) >= PUA_START and char not in reachable
                    for char in example.output_chars
                ),
            )
            for example in self.feature_examples["aalt"]
        ]

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
        elif isinstance(rule, Positioning):
            self._process_ff_positioning(rule, tag, langs, result_map)
        elif isinstance(rule, Attachment):
            self._process_ff_attachment(rule, tag, langs, result_map)

    def _process_ff_substitution(
        self,
        rule: Substitution,
        tag: str,
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
    ) -> None:
        """Process a standard fontFeatures Substitution rule."""
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
            out_seq, adjustment = _apply_chain_lookups(rule.lookups, in_seq)
            if out_seq != in_seq or adjustment is not None:
                self._add_to_result_map(
                    tag,
                    precontext + in_seq + postcontext,
                    precontext + out_seq + postcontext,
                    langs,
                    result_map,
                    positioning=out_seq == in_seq,
                    adjustment=adjustment or 0,
                )

    def _process_ff_positioning(
        self,
        rule: Positioning,
        tag: str,
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
    ) -> None:
        """Process a single or pair positioning rule, e.g. kerning."""
        precontext = [self._representative(c) for c in rule.precontext or []]
        postcontext = [self._representative(c) for c in rule.postcontext or []]
        first_glyphs = [self._representative(c) for c in rule.glyphs]
        for glyph in rule.glyphs[0] if len(rule.glyphs) == 1 else first_glyphs[:1]:
            glyphs = [*precontext, glyph, *first_glyphs[1:], *postcontext]
            self._add_to_result_map(
                tag,
                glyphs,
                glyphs,
                langs,
                result_map,
                positioning=True,
                adjustment=_main_adjustment(rule),
            )

    def _representative(self, glyph_class: list[str]) -> str:
        """Return the glyph of a class with the lowest code point, if any is encoded."""
        return min(
            glyph_class,
            key=lambda g: (g not in self.reverse_cmap, self.reverse_cmap.get(g, 0)),
        )

    def _process_ff_attachment(
        self,
        rule: Attachment,
        tag: str,
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
    ) -> None:
        """Process a mark attachment rule, pairing each mark with one of the bases."""
        encoded_first = sorted(rule.bases, key=lambda g: g not in self.reverse_cmap)
        marks = sorted(rule.marks, key=lambda g: g not in self.reverse_cmap)
        for base, mark in zip(itertools.cycle(encoded_first), marks, strict=False):
            self._add_to_result_map(
                tag, [base, mark], [base, mark], langs, result_map, positioning=True
            )

    def _add_to_result_map(
        self,
        tag: str,
        in_glyphs: list[str],
        out_glyphs: list[str],
        langs: tuple[str, ...],
        result_map: dict[tuple[str, str, str], FeatureExample],
        feature_value: int = 1,
        positioning: bool = False,
        adjustment: int = 0,
    ) -> None:
        """Add or combine an example in the result map."""
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
                positioning=positioning,
                adjustment=adjustment,
            )
        else:
            existing = result_map[key]
            new_langs = tuple(sorted(set(existing.languages + langs)))
            result_map[key] = replace(existing, languages=new_langs)

    @contextlib.contextmanager
    def modified_font(self) -> Iterator[Path]:
        """Provide a font file in which unencoded glyphs are mapped to the PUA."""
        path = self.create_modified_font()
        try:
            yield path
        finally:
            if path != self.font_path:
                path.unlink(missing_ok=True)

    def get_font_feature_tags(self) -> set[str]:
        """Return the tags of all GSUB and GPOS features in the font."""
        return {
            record.FeatureTag
            for table_tag in ("GSUB", "GPOS")
            if table_tag in self.font
            and (feature_list := self.font[table_tag].table.FeatureList)  # type: ignore[missing-attribute]
            for record in feature_list.FeatureRecord
        }

    def count_examples(self, tag: str) -> int:
        """Return the number of examples found, including dropped positioning ones."""
        examples = self.feature_examples.get(tag, [])
        positioning = sum(e.positioning for e in examples)
        return (
            len(examples) - positioning + self.positioning_totals.get(tag, positioning)
        )

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


def _substitution_outputs(ff: FontFeatures, exclude_tag: str) -> set[str]:
    """Return the glyphs that the substitutions of all other features produce."""
    outputs: set[str] = set()
    visited: set[int] = set()

    def visit(routine: Routine | None) -> None:
        if routine is None or id(routine) in visited:
            return
        visited.add(id(routine))
        for rule in routine.rules:
            if isinstance(rule, Substitution):
                outputs.update(glyph for cls in rule.replacement for glyph in cls)
            elif isinstance(rule, Chaining):
                for position_lookups in rule.lookups:
                    for lookup in position_lookups or []:
                        visit(
                            lookup.routine
                            if isinstance(lookup, RoutineReference)
                            else lookup
                        )

    for tag, routine_refs in ff.features.items():
        if tag != exclude_tag:
            for ref in routine_refs:
                visit(ref.routine)
    return outputs


def _main_adjustment(rule: Positioning) -> int:
    """Return the first non-zero value of a positioning rule's value records."""
    fields = ("xAdvance", "xPlacement", "yPlacement", "yAdvance")
    values = (
        getattr(record, field) or 0
        for record in rule.valuerecords
        if record
        for field in fields
    )
    return next((value for value in values if value), 0)


def _match_positioning(routine: Routine, glyphs: list[str]) -> int | None:
    """Return the main adjustment of the routine's positioning rule matching glyphs."""
    for rule in routine.rules:
        if not isinstance(rule, Positioning) or len(rule.glyphs) > len(glyphs):
            continue
        window = glyphs[: len(rule.glyphs)]
        if all(g in cls for g, cls in zip(window, rule.glyphs, strict=True)):
            return _main_adjustment(rule)
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
) -> tuple[list[str], int | None]:
    """
    Apply the lookups of a chaining rule to an input sequence.

    Returns the output glyphs and, if a positioning lookup matched, its adjustment.
    """
    parts = [[glyph] for glyph in in_seq]
    adjustment = None
    for i, position_lookups in enumerate(lookups):
        for lookup in position_lookups or []:
            if len(parts[i]) != 1:  # consumed by a ligature or multiplied
                break
            routine = lookup.routine if isinstance(lookup, RoutineReference) else lookup
            if routine is None:
                continue
            glyphs = [*parts[i], *in_seq[i + 1 :]]
            if match := _match_substitution(routine, glyphs):
                length, replacement = match
                parts[i : i + length] = [replacement] + [[]] * (length - 1)
            elif adjustment is None:
                adjustment = _match_positioning(routine, glyphs)
    return [glyph for part in parts for glyph in part], adjustment


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
    # 90: Access All Alternates, which repeats the other features

    priority = 60  # Default: General

    if tag in {"lnum", "onum", "pnum", "tnum"}:
        priority = 10
    elif tag == "aalt":
        priority = 90
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


def collect_examples(
    analyzer: FontAnalyzer,
    comparator: VisualComparator,
    min_ratio: float = 0.0,
    hide_aalt_only: bool = False,
) -> dict[str, list[FeatureExample]]:
    """Return the visible examples per feature, grouped and in report order."""
    return {
        tag: group_examples(
            score_examples(
                [
                    example
                    for example in analyzer.feature_examples[tag]
                    if not (hide_aalt_only and example.aalt_only)
                ],
                comparator,
                min_ratio,
            )
        )
        for tag in sorted(analyzer.feature_examples, key=get_feature_priority)
    }


def feature_notes(
    analyzer: FontAnalyzer,
    tag: str,
    examples: list[FeatureExample],
    hide_aalt_only: bool = False,
) -> list[str]:
    """Describe which of the feature's examples are not shown individually."""
    notes = []
    if total := analyzer.positioning_totals.get(tag):
        notes.append(f"{MAX_POSITIONING_EXAMPLES} strongest of {total} positionings")
    all_examples = analyzer.feature_examples[tag]
    aalt_only_hidden = sum(e.aalt_only for e in all_examples) if hide_aalt_only else 0
    if aalt_only_hidden:
        notes.append(f"{aalt_only_hidden} only reachable via aalt hidden")
    if aalt_only_shown := sum(
        1 + len(e.input_variants) for e in examples if e.aalt_only
    ):
        notes.append(f"{aalt_only_shown} only reachable via aalt (outlined)")
    shown_inputs = sum(1 + len(e.input_variants) for e in examples)
    if hidden := len(all_examples) - aalt_only_hidden - shown_inputs:
        notes.append(f"{hidden} invisible or too subtle")
    if folded := sum(len(e.input_variants) for e in examples):
        notes.append(f"{folded} similar inputs folded")
    return notes
