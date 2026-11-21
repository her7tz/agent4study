"""Check what the paper layout hint can safely claim about columns."""

import json
import unittest

from paper_table_layout import table_column_uncertainty, table_layout_hint


def _row(*cells):
    return "".join(f"{cell:<12}" for cell in cells).rstrip()


class PaperTableLayoutTests(unittest.TestCase):
    def test_aligned_sparse_rows_keep_empty_columns_and_exact_values(self):
        page = "\n".join([
            "Table 1: Model comparison.",
            "",
            _row("model", "layers", "key", "value", "ppl", "bleu"),
            _row("base", "6", "64", "64", "4.92", "25.8"),
            _row("small key", "", "16", "", "5.16", "25.1"),
            _row("shallower", "2", "", "", "6.11", "23.7"),
            "unclear" + " " * 47 + "3.14",
            "",
            "A separate paragraph follows.",
        ])
        hint = table_layout_hint(page)
        self.assertEqual(hint.splitlines(), [
            "Table 1 (. = blank)",
            "model|layers|key|value|ppl|bleu",
            "base|6|64|64|4.92|25.8",
            "small key|.|16|.|5.16|25.1",
            "shallower|2|.|.|6.11|23.7",
            "Omitted unaligned/over-budget rows: 1",
        ])
        self.assertLessEqual(len(json.dumps(hint)), 1200)

    def test_fused_or_repeated_headers_do_not_assign_language_columns(self):
        body = [_row("first", "20.0", "25.0", "2.00", "1.00", "10"),
                _row("second", "21.0", "24.0", "2.10", "1.10", "11"),
                _row("third", "23.0", "24.0", "2.20", "1.20", "12")]
        for heading in [
            _row("model", "BLEU-DE", "BLEU-FR", "Training costEN-DE", "EN-FR", "rows"),
            _row("model", "EN-DE", "EN-FR", "EN-DE", "EN-FR", "rows"),
        ]:
            with self.subTest(heading=heading):
                self.assertEqual(table_layout_hint("\n".join([
                    "Table 2: Translation results.", heading, *body,
                ])), "")

    def test_numeric_reference_shifted_to_neighbor_headings_is_ambiguous(self):
        page = "\n".join([
            "Table 3: Shifted cells.",
            "      A     B     C     D     E",
            "            1     2     3     4     5",
            "            6     7     8     9     1",
            "            2     3     4     5     6",
        ])
        self.assertEqual(table_layout_hint(page), "")

    def test_between_column_value_keeps_its_row_without_inventing_a_column(self):
        lines = ["Table 4: Two cost columns.", "", "Model   Scores   Costs"]
        for i in range(3):
            lines.append(f"reference-{i}    1.0     2.0" + " " * 10 + "1.1 · 1019" + " " * 5 + "2.2 · 1019")
        lines.extend(["shared     1.0     2.0" + " " * 18 + "3.3  · 1018",
                      "left       1.0     2.0" + " " * 10 + "4.4 · 1018",
                      "right      1.0     2.0" + " " * 25 + "5.5 · 1018", "",
                      "outside    1.0     2.0" + " " * 18 + "6.6 · 1018"])
        hint = table_column_uncertainty("\n".join(lines))
        self.assertEqual(hint.splitlines()[1:], ["shared: 3.3  · 1018"])
        self.assertIn("individual column not established", hint)


if __name__ == "__main__":
    unittest.main()
