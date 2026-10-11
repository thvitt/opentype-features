import csv
import shutil
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from opentype_features import FontAnalyzer, VisualComparator, collect_examples
from opentype_features.cli import SUMMARY_TABLE_NAME, app, report, summarize


@pytest.fixture
def in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def two_fonts(test_font, tmp_path):
    other = tmp_path / "fonts" / "Other.ttf"
    other.parent.mkdir()
    shutil.copy(test_font, other)
    return [test_font, other]


def _read_tsv(path):
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file, delimiter="\t"))


def _summary_text(analyzer, examples, report_path, aalt_only=False):
    console = Console(record=True, width=200)
    console.print(summarize(analyzer, examples, report_path, aalt_only))
    return console.export_text()


@pytest.mark.parametrize(
    "options", [[], ["--hide-subtle"], ["--diff"], ["--aalt-only"], ["--characters"]]
)
def test_report_writes_pdf_next_to_working_directory(test_font, in_tmp_path, options):
    app([str(test_font), *options], result_action="return_value")

    assert (in_tmp_path / "FeatureTest.pdf").stat().st_size > 0


def test_report_writes_to_given_output(test_font, in_tmp_path):
    report([test_font], output=in_tmp_path / "custom.pdf")

    assert (in_tmp_path / "custom.pdf").stat().st_size > 0


def test_report_writes_into_given_directory(test_font, in_tmp_path):
    (in_tmp_path / "reports").mkdir()

    report([test_font], output=in_tmp_path / "reports")

    assert (in_tmp_path / "reports" / "FeatureTest.pdf").stat().st_size > 0
    assert (in_tmp_path / "reports" / SUMMARY_TABLE_NAME).exists()


def test_report_writes_a_pdf_per_font(two_fonts, in_tmp_path):
    exit_code = report(two_fonts, output=in_tmp_path / "new" / "reports")

    reports = in_tmp_path / "new" / "reports"
    assert exit_code == 0
    assert sorted(p.name for p in reports.iterdir()) == [
        "FeatureTest.pdf",
        "Other.pdf",
        SUMMARY_TABLE_NAME,
    ]


def test_summary_table_has_a_row_per_font(two_fonts, in_tmp_path):
    report(two_fonts)

    rows = _read_tsv(in_tmp_path / SUMMARY_TABLE_NAME)
    assert [row["filename"] for row in rows] == ["FeatureTest.ttf", "Other.ttf"]
    assert rows[0]["font name"] == "FeatureTest Regular"
    assert rows[0]["salt"] == "2"
    assert rows[0]["kern"] == "2"
    assert list(rows[0])[-1] == "aalt"


def test_summary_table_is_updated(test_font, in_tmp_path):
    table = in_tmp_path / "table.tsv"
    table.write_text(
        "filename\tfont name\tswsh\tsalt\n"
        "Old.otf\tOld Regular\t7\t1\n"
        "FeatureTest.ttf\tStale\t3\t9\n",
        encoding="utf-8",
    )

    report([test_font], tsv=table)

    rows = _read_tsv(table)
    assert [row["filename"] for row in rows] == ["Old.otf", "FeatureTest.ttf"]
    assert rows[0]["swsh"] == "7"
    assert rows[0]["liga"] == ""
    assert rows[1]["font name"] == "FeatureTest Regular"
    assert rows[1]["swsh"] == ""
    assert rows[1]["salt"] == "2"


def test_summary_table_without_filename_column(test_font, in_tmp_path):
    table = in_tmp_path / "table.tsv"
    table.write_text("name\tliga\n", encoding="utf-8")

    with pytest.raises(ValueError, match="filename"):
        report([test_font], tsv=table)


def test_fonts_with_the_same_name_are_rejected(test_font, in_tmp_path):
    copy = in_tmp_path / "copy" / test_font.name
    copy.parent.mkdir()
    shutil.copy(test_font, copy)

    with pytest.raises(ValueError, match=r"FeatureTest\.pdf"):
        report([test_font, copy])


def test_unusable_font_does_not_stop_the_others(test_font, in_tmp_path, caplog):
    broken = in_tmp_path / "Broken.ttf"
    broken.write_bytes(b"not a font")

    exit_code = report([broken, test_font])

    assert exit_code == 1
    assert "Broken.ttf is not a usable font" in caplog.text
    assert (in_tmp_path / "FeatureTest.pdf").exists()
    rows = _read_tsv(in_tmp_path / SUMMARY_TABLE_NAME)
    assert [row["filename"] for row in rows] == ["FeatureTest.ttf"]


def test_report_compares_with_modified_font(test_font, in_tmp_path, monkeypatch):
    used_paths = []

    class RecordingComparator(VisualComparator):
        def __init__(self, font_path):
            used_paths.append(font_path)
            super().__init__(font_path)

    monkeypatch.setattr("opentype_features.cli.VisualComparator", RecordingComparator)

    report([test_font])

    assert len(used_paths) == 1
    assert used_paths[0] != test_font
    assert not used_paths[0].exists()


def test_report_logs_unwritable_output(
    test_font, in_tmp_path, monkeypatch, caplog, capsys
):
    def fail(*_args, **_kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr("opentype_features.cli.render_pdf_report", fail)

    report([test_font])

    assert "Could not write the PDF report" in caplog.text
    assert "No PDF report written" in capsys.readouterr().err


def test_summary_lists_findings(test_font, in_tmp_path):
    analyzer = FontAnalyzer(test_font)
    analyzer.analyze()
    comparator = VisualComparator(test_font)
    analyzer.detect_default_features(comparator)
    examples = collect_examples(analyzer, comparator)

    text = _summary_text(analyzer, examples, in_tmp_path / "report.pdf")

    liga_row = next(line for line in text.splitlines() if " liga " in line)
    assert "Standard Ligatures" in liga_row
    assert " on " in liga_row
    assert "3 similar inputs folded" in text  # ss02
    assert "report.pdf" in text


def test_summary_notes_hidden_examples(analyzed_with_hidden_smcp):
    analyzer, examples = analyzed_with_hidden_smcp

    text = _summary_text(analyzer, examples, None)

    smcp_row = next(line for line in text.splitlines() if " smcp " in line)
    assert "1 invisible or too subtle" in smcp_row
    assert "No PDF report written" in text


def test_summary_notes_aalt_only_alternates(test_font, in_tmp_path):
    analyzer = FontAnalyzer(test_font)
    analyzer.analyze()
    comparator = VisualComparator(test_font)

    shown = _summary_text(analyzer, collect_examples(analyzer, comparator), None)
    hidden = _summary_text(
        analyzer,
        collect_examples(analyzer, comparator, aalt_only=True),
        None,
        aalt_only=True,
    )

    aalt_row = next(line for line in hidden.splitlines() if " aalt " in line)
    assert "1 aalt only (outlined)" in shown
    assert "also in other features" in aalt_row
    assert "invisible" not in aalt_row


@pytest.fixture
def analyzed_with_hidden_smcp(test_font):
    analyzer = FontAnalyzer(test_font)
    analyzer.analyze()
    comparator = MagicMock(spec=VisualComparator)
    comparator.get_difference_score.side_effect = lambda _text, tag, *_: (
        (0.1, 0.005) if tag == "smcp" else (0.1, 0.5)
    )
    return analyzer, collect_examples(analyzer, comparator, min_ratio=0.01)
