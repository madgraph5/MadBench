from __future__ import annotations

from madbench.results import append_row, select_results_csv


def test_select_results_csv_new_file(tmp_path):
    path, write_header = select_results_csv(tmp_path, ["a", "b"])
    assert path == tmp_path / "results.csv"
    assert write_header is True


def test_select_results_csv_matching_header_appends(tmp_path):
    csv = tmp_path / "results.csv"
    csv.write_text("a,b\n1,2\n")
    path, write_header = select_results_csv(tmp_path, ["a", "b"])
    assert path == csv
    assert write_header is False


def test_select_results_csv_mismatch_rolls_over(tmp_path):
    (tmp_path / "results.csv").write_text("a,b\n1,2\n")
    path, write_header = select_results_csv(tmp_path, ["a", "b", "c"])
    assert path == tmp_path / "results.2.csv"
    assert write_header is True


def test_select_results_csv_rollover_finds_matching(tmp_path):
    (tmp_path / "results.csv").write_text("a,b\n1,2\n")
    (tmp_path / "results.2.csv").write_text("a,b,c\n1,2,3\n")
    path, write_header = select_results_csv(tmp_path, ["a", "b", "c"])
    assert path == tmp_path / "results.2.csv"
    assert write_header is False


def test_append_row_writes_header_first_then_appends(tmp_path):
    csv = tmp_path / "results.csv"
    append_row(csv, ["a", "b"], {"a": 1, "b": 2}, write_header=True)
    append_row(csv, ["a", "b"], {"a": 3, "b": 4}, write_header=False)
    lines = csv.read_text().splitlines()
    assert lines == ["a,b", "1,2", "3,4"]


def test_append_row_quotes_special_chars(tmp_path):
    csv = tmp_path / "results.csv"
    append_row(
        csv,
        ["label", "note"],
        {"label": "x,y", "note": 'has "quotes"'},
        write_header=True,
    )
    text = csv.read_text()
    assert '"x,y"' in text
    assert '"has ""quotes"""' in text


def test_append_row_missing_keys_become_blank(tmp_path):
    csv = tmp_path / "results.csv"
    append_row(csv, ["a", "b", "c"], {"a": 1, "c": 3}, write_header=True)
    lines = csv.read_text().splitlines()
    assert lines[1] == "1,,3"


def test_append_row_ignores_extra_keys(tmp_path):
    csv = tmp_path / "results.csv"
    append_row(csv, ["a"], {"a": 1, "extra": "ignored"}, write_header=True)
    lines = csv.read_text().splitlines()
    assert lines == ["a", "1"]
