"""Tests for the streak maths. Run: python -m unittest discover -s scripts"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate import streak_stats  # noqa: E402


def calendar(start: str, counts: list[int]) -> dict[str, int]:
    d0 = dt.date.fromisoformat(start)
    return {(d0 + dt.timedelta(days=i)).isoformat(): n for i, n in enumerate(counts)}


class StreakTests(unittest.TestCase):
    def test_late_push_backfills_the_gap(self):
        # The Nov 2026 incident: commits made on Nov 7-8 were pushed on Nov 11.
        # Before the push the grid had a two-day gap, so a streak of 3 was right at that moment;
        # after the push the same days count, and the streak is the full 31 days to Nov 11.
        before = calendar("2026-10-12", [5] * 26 + [0, 0] + [5, 5, 5])  # Oct 12 .. Nov 11
        st = streak_stats(before)
        self.assertEqual((st["current"], str(st["current_start"])), (3, "2026-11-09"))
        self.assertEqual((st["longest"], str(st["longest_end"])), (26, "2026-11-06"))

        after = calendar("2026-10-12", [5] * 31)
        st = streak_stats(after)
        self.assertEqual((st["current"], str(st["current_start"]), str(st["current_end"])),
                         (31, "2026-10-12", "2026-11-11"))

    def test_empty_today_does_not_break_the_streak(self):
        st = streak_stats(calendar("2026-01-01", [1, 1, 1, 0]))
        self.assertEqual((st["current"], str(st["current_end"])), (3, "2026-01-03"))

    def test_two_empty_days_end_the_streak(self):
        st = streak_stats(calendar("2026-01-01", [1, 1, 1, 0, 0]))
        self.assertEqual((st["current"], st["current_start"]), (0, None))
        self.assertEqual(st["longest"], 3)

    def test_longest_prefers_the_most_recent_tie(self):
        st = streak_stats(calendar("2026-01-01", [1, 1, 0, 1, 1]))
        self.assertEqual((st["longest"], str(st["longest_start"])), (2, "2026-01-04"))

    def test_missing_days_count_as_empty(self):
        days = {"2026-01-01": 1, "2026-01-02": 1, "2026-01-05": 1}
        st = streak_stats(days)
        self.assertEqual((st["current"], st["longest"], st["total"]), (1, 2, 3))

    def test_year_total_only_counts_this_year(self):
        st = streak_stats(calendar("2025-12-30", [4, 4, 7, 7]))
        self.assertEqual((st["total"], st["year_total"]), (22, 14))


if __name__ == "__main__":
    unittest.main()
