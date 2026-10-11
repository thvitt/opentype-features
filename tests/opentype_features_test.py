from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fontFeatures import Substitution as FFSubstitution

from opentype_features import (
    PUA_START,
    FontAnalyzer,
    Substitution,
    VisualComparator,
    get_feature_priority,
    is_character_variant,
    score_substitutions,
)


@pytest.fixture
def analyzer(monkeypatch):
    """Fixture to provide a FontAnalyzer with a mocked init."""
    # Mock TTFont to prevent it from trying to read a file
    monkeypatch.setattr("opentype_features.TTFont", MagicMock())
    # Mock _build_reverse_cmap so it doesn't fail on init
    monkeypatch.setattr(
        "opentype_features.FontAnalyzer._build_reverse_cmap", lambda self: {}
    )

    a = FontAnalyzer(Path("dummy.ttf"))
    return a


def test_get_display_name_with_specific_name(analyzer):
    """Test that a specific name is returned as-is."""
    assert analyzer.get_display_name("Aogonek") == "Aogonek"
    assert analyzer.get_display_name("ordfeminine") == "ordfeminine"
    assert analyzer.get_display_name("f_f_i.liga") == "f_f_i.liga"


def test_get_display_name_generic_with_unicode(analyzer):
    """Test that generic names with a Unicode mapping return U+XXXX."""
    analyzer.reverse_cmap = {"glyph00065": 0x0041, "glyph00123": 0x012A}
    assert analyzer.get_display_name("glyph00065") == "U+0041"
    assert analyzer.get_display_name("glyph00123") == "U+012A"


def test_get_display_name_generic_without_unicode(analyzer):
    """Test that generic names without Unicode mapping return normalized glyph number."""
    analyzer.reverse_cmap = {}
    assert analyzer.get_display_name("glyph00499") == "glyph499"
    assert analyzer.get_display_name("glyph00005") == "glyph5"
    assert analyzer.get_display_name("glyph1234") == "glyph1234"


def test_get_glyph_info_with_unicode(analyzer):
    """Test get_glyph_info for a glyph present in reverse_cmap."""
    analyzer.reverse_cmap = {"Aogonek": 0x0104}
    char, name = analyzer.get_glyph_info("Aogonek")
    assert char == chr(0x0104)
    assert name == "Aogonek"
    assert not analyzer.pua_map  # PUA map should not be updated


def test_get_glyph_info_without_unicode(analyzer):
    """Test get_glyph_info for a glyph without Unicode mapping, using PUA."""
    analyzer.reverse_cmap = {}

    char1, name1 = analyzer.get_glyph_info("glyph00499")
    assert char1 == chr(PUA_START)
    assert name1 == "glyph00499"
    assert analyzer.pua_map["glyph00499"] == chr(PUA_START)

    char2, name2 = analyzer.get_glyph_info("glyph00500")
    assert char2 == chr(PUA_START + 1)
    assert name2 == "glyph00500"
    assert analyzer.pua_map["glyph00500"] == chr(PUA_START + 1)

    # Second call for the same glyph should return the same PUA character
    char3, name3 = analyzer.get_glyph_info("glyph00499")
    assert char3 == chr(PUA_START)
    assert name3 == "glyph00499"


def test_get_tag_name(analyzer):
    """Test mapping of feature tags to friendly names."""
    # Assuming "liga" is in FEATURE_NAMES
    assert analyzer.get_tag_name("liga") == "Standard Ligatures"
    # Fallback to tag itself
    assert analyzer.get_tag_name("unknown_tag") == "unknown_tag"


def test_get_feature_priority():
    """Test feature sorting priority."""
    # Figures
    p_fig = 10
    assert get_feature_priority("lnum")[0] == p_fig

    # Ligatures
    p_liga = 20
    p_other_liga = 21
    assert get_feature_priority("liga")[0] == p_liga
    assert get_feature_priority("dlig")[0] == p_other_liga

    # Stylistic Alternates
    p_salt = 30
    assert get_feature_priority("salt")[0] == p_salt

    # Stylistic Sets
    p_ss = 40
    assert get_feature_priority("ss01")[0] == p_ss
    assert get_feature_priority("ss20")[0] == p_ss

    # Character Variants
    p_cv = 50
    assert get_feature_priority("cv01")[0] == p_cv

    # General
    p_gen = 60
    assert get_feature_priority("ccmp")[0] == p_gen

    # Small/Shifted
    p_small = 70
    assert get_feature_priority("smcp")[0] == p_small
    assert get_feature_priority("frac")[0] == p_small

    # Relative ordering within group by tag name
    assert get_feature_priority("dlig") == (p_other_liga, "dlig")
    assert get_feature_priority("clig") == (p_other_liga, "clig")
    # sorted((21, "dlig"), (21, "clig")) will put clig first


def test_get_figure_chars(analyzer):
    """Test collection of characters for the figures section."""
    analyzer.feature_substitutions = {
        "lnum": [Substitution("a", "a", "A", "A", "lnum")],
        "onum": [
            Substitution("0", "0", "0.old", "0", "onum"),
            Substitution("pua", chr(PUA_START), "x", "x", "onum"),
            Substitution("%", "%", "%.alt", "%", "onum"),
        ],
        "other": [Substitution("x", "x", "X", "X", "other")],
    }
    # Should be "0123456789" + sorted("%", "a")
    chars = analyzer.get_figure_chars()
    assert chars.startswith("0123456789")
    assert chars == "0123456789%a"
    assert "x" not in chars
    assert chr(PUA_START) not in chars


def test_substitution_has_one_to_one_chars():
    """Test the has_one_to_one_chars property of Substitution."""
    # 1-to-1 mapping
    # Note: input_chars/output_chars represent the character sequences
    sub1 = Substitution("A", "A", "A.alt", "A", "salt", diff_total=1.0, diff_ratio=1.0)
    assert sub1.has_one_to_one_chars

    # Many-to-1 mapping (ligature)
    sub2 = Substitution(
        "f f i", "ffi", "ffi.liga", "ﬁ", "liga", diff_total=1.0, diff_ratio=1.0
    )
    assert not sub2.has_one_to_one_chars

    # 1-to-many mapping (multiple substitution)
    sub3 = Substitution(
        "ffi.liga", "ﬁ", "f f i", "ffi", "ccmp", diff_total=1.0, diff_ratio=1.0
    )
    assert not sub3.has_one_to_one_chars

    # PUA mapping (still 1-to-1)
    sub4 = Substitution(
        "glyph00499",
        "\ue000",
        "glyph00499.alt",
        "\ue001",
        "salt",
        diff_total=1.0,
        diff_ratio=1.0,
    )
    assert sub4.has_one_to_one_chars


@pytest.fixture
def analyzed(test_font):
    """A FontAnalyzer that has analyzed the synthetic test font."""
    a = FontAnalyzer(test_font)
    a.analyze()
    return a


def _scored(analyzer, tag, font_path=None):
    comparator = VisualComparator(font_path or analyzer.font_path)
    return score_substitutions(analyzer.feature_substitutions[tag], comparator)


def test_visual_comparator(test_font):
    vc = VisualComparator(test_font)

    assert vc.is_visually_different("a", "smcp")
    total_diff, ratio_diff = vc.get_difference_score("a", "smcp")
    diff_threshold = 0.01
    assert total_diff > diff_threshold
    assert ratio_diff > diff_threshold

    assert not vc.is_visually_different("b", "smcp")
    assert vc.get_difference_score("b", "smcp") == (0.0, 0.0)


@pytest.mark.parametrize("tag", ["liga", "calt"])
def test_default_features_are_scored_as_different(analyzed, tag):
    assert _scored(analyzed, tag)


def test_is_character_variant():
    assert is_character_variant("cv01")
    assert is_character_variant("cv99")
    assert not is_character_variant("ss01")
    assert not is_character_variant("cvxx")


def _alternate_rule(in_glyph, out_glyphs):
    rule = MagicMock(spec=FFSubstitution)
    rule.input = [[in_glyph]]
    rule.replacement = [out_glyphs]
    return rule


def test_character_variant_alternates_get_feature_values(analyzer):
    analyzer.reverse_cmap = {"a": ord("a")}
    result_map = {}

    analyzer._process_ff_substitution(
        _alternate_rule("a", ["a.alt1", "a.alt2"]), "cv01", (), result_map
    )

    subs = sorted(result_map.values(), key=lambda s: s.feature_value)
    assert [(s.output_glyphs, s.feature_value) for s in subs] == [
        ("a.alt1", 1),
        ("a.alt2", 2),
    ]


def test_variant_label_prefers_font_names(analyzer):
    analyzer.variant_names = {"cv01": ["Round", ""]}
    analyzer.tags_with_alternates = {"cv01"}

    def label(value):
        return analyzer.get_variant_label(
            Substitution("a", "a", "a.alt", "x", "cv01", feature_value=value)
        )

    assert label(1) == "Round"
    assert label(2) == "#2"
    assert label(3) == "#3"


def test_variant_label_empty_without_alternates(analyzer):
    plain = Substitution("a", "a", "a.alt", "x", "cv02")
    assert analyzer.get_variant_label(plain) == ""
    assert analyzer.get_variant_label(Substitution("a", "a", "A", "A", "smcp")) == ""
