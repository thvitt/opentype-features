from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opentype_features import (
    PUA_START,
    FontAnalyzer,
    Substitution,
    get_feature_priority,
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
    sub1 = Substitution("A", "A", "A.alt", "A", "salt")
    assert sub1.has_one_to_one_chars

    # Many-to-1 mapping (ligature)
    sub2 = Substitution("f f i", "ffi", "ffi.liga", "ﬁ", "liga")
    assert not sub2.has_one_to_one_chars

    # 1-to-many mapping (multiple substitution)
    sub3 = Substitution("ffi.liga", "ﬁ", "f f i", "ffi", "ccmp")
    assert not sub3.has_one_to_one_chars

    # PUA mapping (still 1-to-1)
    sub4 = Substitution("glyph00499", "\ue000", "glyph00499.alt", "\ue001", "salt")
    assert sub4.has_one_to_one_chars
