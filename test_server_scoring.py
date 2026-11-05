import unittest

from server import TestJudgementInput, calculate_single_test_score


class TestSingleSongScoring(unittest.TestCase):
    def test_all_perfect_is_one_million(self):
        result = calculate_single_test_score(
            TestJudgementInput(total_notes=100, perfect=100, good=0, bad=0, miss=0, max_combo=100)
        )
        self.assertEqual(result["accuracy"], 100.0)
        self.assertEqual(result["score"], 1_000_000.0)

    def test_good_keeps_full_combo_bonus(self):
        result = calculate_single_test_score(
            TestJudgementInput(total_notes=100, perfect=90, good=10, bad=0, miss=0, max_combo=100)
        )
        self.assertEqual(result["accuracy"], 96.5)
        self.assertEqual(result["score"], 968_500.0)

    def test_combo_break_uses_max_combo(self):
        result = calculate_single_test_score(
            TestJudgementInput(total_notes=100, perfect=90, good=0, bad=1, miss=9, max_combo=90)
        )
        self.assertEqual(result["score"], 900_000.0)

    def test_bad_and_miss_have_no_judgement_points(self):
        result = calculate_single_test_score(
            TestJudgementInput(total_notes=100, perfect=0, good=0, bad=50, miss=50, max_combo=0)
        )
        self.assertEqual(result["accuracy"], 0.0)
        self.assertEqual(result["score"], 0.0)

    def test_accuracy_and_score_share_the_same_judgements(self):
        result = calculate_single_test_score(
            TestJudgementInput(total_notes=137, perfect=100, good=20, bad=7, miss=10, max_combo=115)
        )
        expected_accuracy = (100 + 0.65 * 20) / 137 * 100
        expected_score = 900_000 * (100 + 0.65 * 20) / 137 + 100_000 * 115 / 137
        self.assertAlmostEqual(result["accuracy"], expected_accuracy)
        self.assertAlmostEqual(result["score"], expected_score)

    def test_invalid_counts_and_zero_notes_are_rejected(self):
        invalid_inputs = [
            TestJudgementInput(total_notes=0, perfect=0, good=0, bad=0, miss=0, max_combo=0),
            TestJudgementInput(total_notes=10, perfect=9, good=0, bad=0, miss=0, max_combo=9),
            TestJudgementInput(total_notes=10, perfect=10, good=0, bad=0, miss=0, max_combo=11),
        ]
        for data in invalid_inputs:
            with self.assertRaises(ValueError):
                calculate_single_test_score(data)


if __name__ == "__main__":
    unittest.main()