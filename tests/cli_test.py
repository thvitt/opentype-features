from unittest.mock import MagicMock

import pytest
from rich.console import Console

from opentype_features import FontAnalyzer, VisualComparator, collect_examples
from opentype_features.cli import app, report, summarize


@pytest.fixture
def in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _summary_text(analyzer, examples, report_path):
    console = Console(record=True, width=200)
    console.print(summarize(analyzer, examples, report_path))
    return console.export_text()


@pytest.mark.parametrize("options", [[], ["--hide-subtle"], ["--diff"]])
def test_report_writes_pdf_next_to_working_directory(test_font, in_tmp_path, options):
    app([str(test_font), *options], result_action="return_value")

    assert (in_tmp_path / "FeatureTest-features.pdf").stat().st_size > 0


def test_report_writes_to_given_output(test_font, in_tmp_path):
    report(test_font, output=in_tmp_path / "custom.pdf")

    assert (in_tmp_path / "custom.pdf").stat().st_size > 0


def test_report_compares_with_modified_font(test_font, in_tmp_path, monkeypatch):
    used_paths = []

    class RecordingComparator(VisualComparator):
        def __init__(self, font_path):
            used_paths.append(font_path)
            super().__init__(font_path)

    monkeypatch.setattr("opentype_features.cli.VisualComparator", RecordingComparator)

    report(test_font)

    assert len(used_paths) == 1
    assert used_paths[0] != test_font
    assert not used_paths[0].exists()


def test_report_logs_unwritable_output(test_font, in_tmp_path, caplog, capsys):
    report(test_font, output=in_tmp_path / "missing" / "report.pdf")

    assert "Could not write the PDF report" in caplog.text
    assert "No PDF report written" in capsys.readouterr().out


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


@pytest.fixture
def analyzed_with_hidden_smcp(test_font):
    analyzer = FontAnalyzer(test_font)
    analyzer.analyze()
    comparator = MagicMock(spec=VisualComparator)
    comparator.get_difference_score.side_effect = lambda _text, tag, *_: (
        (0.1, 0.005) if tag == "smcp" else (0.1, 0.5)
    )
    return analyzer, collect_examples(analyzer, comparator, min_ratio=0.01)
