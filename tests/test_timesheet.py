#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "timesheet"))
import timesheet as ts  # noqa: E402

UTC = timezone.utc


def dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


class BranchGroup(unittest.TestCase):
    def test_collapses_key_prefix(self):
        self.assertEqual(ts.branch_group("ABC-123-feature-x"), "ABC-123")

    def test_bare_key(self):
        self.assertEqual(ts.branch_group("ABC-124"), "ABC-124")

    def test_non_key_branch_kept(self):
        self.assertEqual(ts.branch_group("develop"), "develop")
        self.assertEqual(ts.branch_group("feature/login"), "feature/login")

    def test_key_not_at_start_kept_raw(self):
        self.assertEqual(ts.branch_group("hotfix/ABC-1-x"), "hotfix/ABC-1-x")

    def test_none(self):
        self.assertIsNone(ts.branch_group(None))


class ActiveInterval(unittest.TestCase):
    def test_cap_drops_long_gap(self):
        cap = 20 * 60
        events = [dt(2026, 1, 3, 10, 0), dt(2026, 1, 3, 10, 5), dt(2026, 1, 3, 11, 0)]
        ivals = ts.active_intervals(events, cap)
        # 10:00->10:05 counts (5m), 10:05->11:00 is 55m > cap, dropped
        self.assertEqual(len(ivals), 1)
        total = sum((b - a).total_seconds() for a, b in ivals)
        self.assertEqual(total, 5 * 60)

    def test_exact_cap_is_kept(self):
        cap = 20 * 60
        events = [dt(2026, 1, 3, 10, 0), dt(2026, 1, 3, 10, 20)]
        self.assertEqual(len(ts.active_intervals(events, cap)), 1)


class MidnightSplit(unittest.TestCase):
    def test_interval_across_midnight_is_split(self):
        ivals = [(dt(2026, 1, 3, 23, 45), dt(2026, 1, 4, 0, 15))]
        pieces = list(ts.split_at_midnight(ivals))
        days = {d for d, _, _ in pieces}
        self.assertEqual(days, {dt(2026, 1, 3, 0, 0).date(), dt(2026, 1, 4, 0, 0).date()})
        total = sum((e - s).total_seconds() for _, s, e in pieces)
        self.assertEqual(total, 30 * 60)


class UnionVsSum(unittest.TestCase):
    def test_overlapping_sessions_union_less_than_sum(self):
        cap = 20 * 60
        a_events = [dt(2026, 1, 8, 10, 0) + timedelta(minutes=m) for m in range(0, 61, 5)]
        b_events = [dt(2026, 1, 8, 10, 30) + timedelta(minutes=m) for m in range(0, 61, 5)]
        pa = ts.active_intervals(a_events, cap)
        pb = ts.active_intervals(b_events, cap)
        union = ts.union_seconds(pa + pb)
        ssum = sum((e - s).total_seconds() for e_list in (pa, pb) for s, e in e_list)
        self.assertEqual(union, 90 * 60)   # 10:00..11:30 merged
        self.assertEqual(ssum, 120 * 60)   # 60 + 60
        self.assertLess(union, ssum)

    def test_disjoint_sessions_union_equals_sum(self):
        ivals_a = [(dt(2026, 1, 8, 9, 0), dt(2026, 1, 8, 10, 0))]
        ivals_b = [(dt(2026, 1, 8, 11, 0), dt(2026, 1, 8, 12, 0))]
        self.assertEqual(ts.union_seconds(ivals_a + ivals_b), 120 * 60)


def _write_session(projects_dir, project, session_id, branch, title, events):
    pdir = os.path.join(projects_dir, project)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, f"{session_id}.jsonl"), "w", encoding="utf-8") as fh:
        for i, e in enumerate(events):
            rec = {"sessionId": session_id, "timestamp": e.astimezone(UTC).isoformat().replace("+00:00", "Z")}
            if i == 0:
                rec["gitBranch"] = branch
                rec["customTitle"] = title
            fh.write(json.dumps(rec) + "\n")


class EndToEndFilters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        base = dt(2026, 1, 3, 10, 0)
        _write_session(self.tmp, "projA", "aaaa1111", "ABC-123-feature-x",
                       "Feature X", [base + timedelta(minutes=m) for m in range(0, 61, 5)])
        _write_session(self.tmp, "projA", "bbbb2222", "ABC-124",
                       "Feature Y", [dt(2026, 1, 5, 9, 0) + timedelta(minutes=m) for m in range(0, 31, 5)])

    def _args(self, **over):
        ns = ts.build_parser().parse_args([])
        ns.projects_dir = self.tmp
        for k, v in over.items():
            setattr(ns, k, v)
        return ns

    def test_branch_filter(self):
        args = self._args(branch="ABC-123")
        sessions = ts.load_sessions(self.tmp, args, None, None)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["group"], "ABC-123")

    def test_date_filter_excludes_out_of_range(self):
        from datetime import date
        args = self._args()
        sessions = ts.load_sessions(self.tmp, args, date(2026, 1, 5), date(2026, 1, 5))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["group"], "ABC-124")

    def test_reports_run(self):
        args = self._args()
        sessions = ts.load_sessions(self.tmp, args, None, None)
        cap = args.cap * 60
        self.assertIn("ABC-123", ts.report_by_session(sessions, cap, None, None))
        self.assertIn("ABC-124", ts.report_by_branch(sessions, cap, None, None))
        self.assertIn("Active (real)", ts.report_by_day(sessions, cap, None, None))


class ReportShape(unittest.TestCase):
    def _sessions(self, tmp):
        _write_session(tmp, "p", "s1", "ABC-1", "t",
                       [dt(2026, 1, 3, 10, 0) + timedelta(minutes=m) for m in range(0, 31, 5)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        return ts.load_sessions(tmp, args, None, None)

    def test_has_header_total_and_legend(self):
        sessions = self._sessions(tempfile.mkdtemp())
        out = ts.report_by_session(sessions, 1200, None, None)
        self.assertIn("| Branch | Session | Title | Active | Span | Per day |", out)
        self.assertIn("**Total**", out)
        self.assertIn("Real working time", out)
        self.assertIn("Active =", out)

    def test_grand_total_merges_parallel_sessions(self):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "p", "s1", "ABC-1", "a",
                       [dt(2026, 1, 3, 10, 0) + timedelta(minutes=m) for m in range(0, 61, 5)])
        _write_session(tmp, "p", "s2", "ABC-2", "b",
                       [dt(2026, 1, 3, 10, 30) + timedelta(minutes=m) for m in range(0, 61, 5)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        out = ts.report_by_session(sessions, 1200, None, None)
        self.assertIn("Real working time over the selection (overlaps merged): 1h30m", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
