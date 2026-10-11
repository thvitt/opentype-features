from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fontFeatures import Substitution as FFSubstitution
from fontTools.ttLib import TTFont

from opentype_features import (
    PUA_START,
    FontAnalyzer,
    Substitution,
    VisualComparator,
    get_feature_priority,
    is_character_variant,
    main,
    print_console_report,
    render_pdf_report,
    score_substitutions,
    shaping_language,
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


def test_get_char_with_unicode(analyzer):
    """Test get_char for a glyph present in reverse_cmap."""
    analyzer.reverse_cmap = {"Aogonek": 0x0104}
    assert analyzer.get_char("Aogonek") == chr(0x0104)
    assert not analyzer.pua_map  # PUA map should not be updated


def test_get_char_without_unicode(analyzer):
    """Test get_char for a glyph without Unicode mapping, using PUA."""
    analyzer.reverse_cmap = {}

    assert analyzer.get_char("glyph00499") == chr(PUA_START)
    assert analyzer.pua_map["glyph00499"] == chr(PUA_START)

    assert analyzer.get_char("glyph00500") == chr(PUA_START + 1)
    assert analyzer.pua_map["glyph00500"] == chr(PUA_START + 1)

    # Second call for the same glyph should return the same PUA character
    assert analyzer.get_char("glyph00499") == chr(PUA_START)


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


def test_localized_forms_are_scored_as_different(analyzed):
    (sub,) = _scored(analyzed, "locl")
    assert sub.languages == ("latn/TRK",)
    assert analyzed.get_cell_label(sub) == "Turkish"


def test_shaping_language():
    assert shaping_language(()) is None
    assert shaping_language(("DFLT/dflt", "latn/TRK")) is None
    assert shaping_language(("latn/CAT", "latn/TRK")) == "x-hbotCAT"


def test_modified_font_maps_unencoded_glyphs(analyzed):
    modified = analyzed.create_modified_font()
    try:
        cmap = TTFont(modified).getBestCmap() or {}
        assert cmap[ord(analyzed.pua_map["a.sc"])] == "a.sc"
        assert cmap[ord("a")] == "a"
        assert _scored(analyzed, "ss01", modified)
    finally:
        modified.unlink()


def test_pdf_report_compares_with_modified_font(analyzed, tmp_path, monkeypatch):
    used_paths = []

    class RecordingComparator(VisualComparator):
        def __init__(self, font_path):
            used_paths.append(font_path)
            super().__init__(font_path)

    monkeypatch.setattr("opentype_features.VisualComparator", RecordingComparator)
    modified = analyzed.create_modified_font()
    try:
        render_pdf_report(analyzed, tmp_path / "report.pdf", modified)
    finally:
        modified.unlink()

    assert used_paths == [modified]
    assert (tmp_path / "report.pdf").stat().st_size > 0


def _glyph_mappings(analyzer, tag):
    return sorted(
        (s.input_glyphs, s.output_glyphs) for s in analyzer.feature_substitutions[tag]
    )


def test_chaining_expands_input_classes(analyzed):
    assert _glyph_mappings(analyzed, "calt") == [
        ("b a", "b.alt a"),
        ("c a", "c.alt a"),
    ]


def test_chaining_applies_nested_ligatures(analyzed):
    assert _glyph_mappings(analyzed, "clig") == [("f i a", "f_i a")]


def test_chaining_tries_all_lookups_at_each_position(analyzed):
    assert _glyph_mappings(analyzed, "dlig") == [("b c", "b.alt c.alt")]


def test_class_substitution_is_expanded(analyzer):
    analyzer.reverse_cmap = {"a": ord("a"), "b": ord("b")}
    rule = MagicMock(spec=FFSubstitution)
    rule.input = [["a", "b"]]
    rule.replacement = [["a.sc", "b.sc"]]
    result_map = {}

    analyzer._process_ff_substitution(rule, "smcp", (), result_map)

    assert sorted((s.input_glyphs, s.output_glyphs) for s in result_map.values()) == [
        ("a", "a.sc"),
        ("b", "b.sc"),
    ]


def test_pdf_report_logs_unusable_font(analyzed, tmp_path, caplog):
    broken_font = tmp_path / "broken.ttf"
    broken_font.write_bytes(b"not a font")

    render_pdf_report(analyzed, tmp_path / "report.pdf", broken_font)

    assert "Could not embed" in caplog.text
    assert not (tmp_path / "report.pdf").exists()


def test_pdf_report_logs_unwritable_output(analyzed, tmp_path, caplog):
    render_pdf_report(analyzed, tmp_path / "missing" / "report.pdf", analyzed.font_path)

    assert "Could not save" in caplog.text


def test_scoring_skips_failing_comparisons(analyzed, caplog):
    comparator = MagicMock(spec=VisualComparator)
    comparator.get_difference_score.side_effect = [RuntimeError("boom"), (0.5, 0.5)]
    subs = analyzed.feature_substitutions["calt"]

    scored = score_substitutions(subs, comparator)

    assert [s.input_glyphs for s in scored] == [subs[1].input_glyphs]
    assert "Could not compare" in caplog.text


def test_analysis_skips_failing_rules(analyzer, monkeypatch, caplog):
    def fail(*_args):
        raise ValueError("unsupported")

    monkeypatch.setattr(analyzer, "_process_rule", fail)
    routine = MagicMock(languages=[("latn", "dflt")], rules=[object()])
    font_features = MagicMock(features={"liga": [MagicMock(routine=routine)]})
    monkeypatch.setattr("opentype_features.unparse", lambda _font: font_features)

    analyzer.analyze()

    assert analyzer.feature_substitutions == {}
    assert "Skipping a rule of feature liga" in caplog.text


def test_pdf_report_without_diff(analyzed, tmp_path, recwarn):
    render_pdf_report(analyzed, tmp_path / "report.pdf", analyzed.font_path, False)

    assert (tmp_path / "report.pdf").stat().st_size > 0
    assert not [w for w in recwarn if issubclass(w.category, DeprecationWarning)]


def test_diff_image_reuses_scoring_renderings(test_font, monkeypatch):
    vc = VisualComparator(test_font)
    vc.get_difference_score("a", "smcp")
    monkeypatch.setattr(vc, "_render_pair", MagicMock(side_effect=AssertionError))

    image = vc.get_diff_image("a", "smcp")

    assert image.mode == "RGB"
    assert image.getpixel((0, 0)) == (255, 255, 255)


def test_detect_default_features(analyzed):
    analyzed.detect_default_features(VisualComparator(analyzed.font_path))

    assert analyzed.default_features == {"calt", "clig", "liga", "locl"}
    assert analyzed.contextual_default_features == set()
    assert analyzed.get_feature_heading("liga") == (
        "liga (Standard Ligatures), on by default"
    )
    assert analyzed.get_feature_heading("smcp") == "smcp (Small Capitals)"


def test_feature_heading_for_contextual_defaults(analyzer):
    analyzer.contextual_default_features = {"frac"}
    assert analyzer.get_feature_heading("frac") == (
        "frac (Fractions), on by default in some contexts"
    )


def test_console_report_hides_small_ratios(analyzed, capsys):
    comparator = MagicMock(spec=VisualComparator)
    comparator.get_difference_score.side_effect = lambda text, tag, *_: (
        (0.1, 0.005) if tag == "smcp" else (0.1, 0.5)
    )

    print_console_report(analyzed, comparator, min_ratio=0.01)

    output = capsys.readouterr().out
    assert "Feature: liga" in output
    assert "Feature: smcp" not in output


@pytest.mark.parametrize("option", [[], ["--hide-subtle"]])
def test_main_writes_reports(test_font, tmp_path, monkeypatch, capsys, option):
    report = tmp_path / "report.pdf"
    monkeypatch.setattr(
        "sys.argv", ["opentype-features", str(test_font), "-o", str(report), *option]
    )

    main()

    assert (
        "Feature: liga (Standard Ligatures), on by default" in capsys.readouterr().out
    )
    assert report.stat().st_size > 0
