from opentype_features import FeatureExample, VisualComparator, score_examples
from opentype_features.pdf_report import (
    CONTENT_WIDTH,
    PAGE_BOTTOM,
    TILE_GAP,
    ReportPDF,
    _layout_rows,
    _render_characters,
    _render_feature,
    render_pdf_report,
)


def _pdf(font_path):
    pdf = ReportPDF("footer")
    pdf.add_font("TargetFont", fname=str(font_path))
    pdf.add_page()
    return pdf


def test_feature_heading_stays_with_its_tiles(analyzed):
    pdf = _pdf(analyzed.font_path)
    pdf.set_y(PAGE_BOTTOM - 20)
    section_pages = []
    pdf.start_section = lambda *_args, **_kwargs: section_pages.append(pdf.page)
    comparator = VisualComparator(analyzed.font_path)
    examples = score_examples(analyzed.feature_examples["smcp"], comparator)

    _render_feature(
        pdf,
        analyzed,
        comparator,
        "smcp",
        examples,
        show_diff=False,
        link=pdf.add_link(),
    )

    assert section_pages == [2]


def test_tiles_wrap_into_rows(analyzed):
    examples = [
        FeatureExample(text, text, text, text, "liga")
        for text in ["fi", "abcdabcd", "a"] * 20
    ]

    rows = _layout_rows(_pdf(analyzed.font_path), analyzed, examples)

    assert len(rows) > 1
    assert [tile.example for row in rows for tile in row] == examples
    for row in rows:
        assert (
            sum(tile.width for tile in row) + TILE_GAP * (len(row) - 1) <= CONTENT_WIDTH
        )


def test_report_with_non_latin_feature_names(analyzed, tmp_path):
    analyzed.custom_feature_names["ss01"] = "Ελληνικά"
    analyzed.custom_feature_names["ss02"] = "日本語\x00\udcff"
    comparator = VisualComparator(analyzed.font_path)
    examples = {
        tag: score_examples(examples, comparator)
        for tag, examples in analyzed.feature_examples.items()
    }

    render_pdf_report(
        analyzed,
        examples,
        tmp_path / "report.pdf",
        analyzed.font_path,
        comparator,
        show_diff=True,
    )

    assert (tmp_path / "report.pdf").stat().st_size > 0


def test_character_table_gets_its_own_section(analyzed, monkeypatch):
    pdf = _pdf(analyzed.font_path)
    sections = []
    monkeypatch.setattr(
        pdf, "start_section", lambda name, **_: sections.append((name, pdf.page))
    )

    _render_characters(pdf, analyzed)

    assert sections == [("Characters", 2)]
