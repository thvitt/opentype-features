from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opentype_features import PUA_START, FontAnalyzer, Substitution


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


def test_substitution_is_simple():
    """Test the is_simple property of Substitution."""
    # 1-to-1 mapping
    # Note: input_chars/output_chars represent the character sequences
    sub1 = Substitution("A", "A", "A.alt", "A", "salt")
    assert sub1.is_simple

    # Many-to-1 mapping (ligature)
    sub2 = Substitution("f f i", "ffi", "ffi.liga", "ﬁ", "liga")
    assert not sub2.is_simple

    # 1-to-many mapping (multiple substitution)
    sub3 = Substitution("ffi.liga", "ﬁ", "f f i", "ffi", "ccmp")
    assert not sub3.is_simple

    # PUA mapping (still 1-to-1)
    sub4 = Substitution("glyph00499", "\ue000", "glyph00499.alt", "\ue001", "salt")
    assert sub4.is_simple
