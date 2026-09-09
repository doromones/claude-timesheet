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


def _write(projects_dir, project, session_id, title, timeline, cwd=None):
    """timeline: list of (datetime, branch_or_None); gitBranch is carried forward."""
    pdir = os.path.join(projects_dir, project)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, f"{session_id}.jsonl"), "w", encoding="utf-8") as fh:
        for i, (e, br) in enumerate(timeline):
            rec = {"sessionId": session_id, "timestamp": e.astimezone(UTC).isoformat().replace("+00:00", "Z")}
            if i == 0 and title:
                rec["customTitle"] = title
            if i == 0 and cwd:
                rec["cwd"] = cwd
            if br is not None:
                rec["gitBranch"] = br
            fh.write(json.dumps(rec) + "\n")


def _write_session(projects_dir, project, session_id, branch, title, events):
    """Convenience: single branch stated on the first event, carried forward."""
    _write(projects_dir, project, session_id, title,
           [(e, branch if i == 0 else None) for i, e in enumerate(events)])


class BranchGroup(unittest.TestCase):
    def test_collapses_key_prefix(self):
        self.assertEqual(ts.branch_group("ABC-123-feature-x"), "ABC-123")

    def test_bare_key(self):
        self.assertEqual(ts.branch_group("ABC-124"), "ABC-124")

    def test_non_key_branch_kept(self):
        self.assertEqual(ts.branch_group("develop"), "develop")
        self.assertEqual(ts.branch_group("feature/login"), "feature/login")

    def test_none(self):
        self.assertIsNone(ts.branch_group(None))


class ActiveSegments(unittest.TestCase):
    def test_cap_drops_long_gap(self):
        cap = 20 * 60
        events = [(dt(2026, 1, 3, 10, 0), "ABC-1"),
                  (dt(2026, 1, 3, 10, 5), "ABC-1"),
                  (dt(2026, 1, 3, 11, 0), "ABC-1")]
        segs = ts.active_segments(events, cap)
        self.assertEqual(len(segs), 1)
        a, b, g = segs[0]
        self.assertEqual((b - a).total_seconds(), 5 * 60)
        self.assertEqual(g, "ABC-1")

    def test_exact_cap_is_kept(self):
        cap = 20 * 60
        events = [(dt(2026, 1, 3, 10, 0), "ABC-1"), (dt(2026, 1, 3, 10, 20), "ABC-1")]
        self.assertEqual(len(ts.active_segments(events, cap)), 1)


class MidnightSplit(unittest.TestCase):
    def test_segment_across_midnight_is_split(self):
        segs = [(dt(2026, 1, 3, 23, 45), dt(2026, 1, 4, 0, 15), "ABC-1")]
        pieces = list(ts.split_at_midnight(segs))
        days = {d for d, _, _, _ in pieces}
        self.assertEqual(days, {dt(2026, 1, 3, 0, 0).date(), dt(2026, 1, 4, 0, 0).date()})
        total = sum((e - s).total_seconds() for _, s, e, _ in pieces)
        self.assertEqual(total, 30 * 60)


class PerEventAttribution(unittest.TestCase):
    def test_time_splits_across_branches_by_active_event(self):
        # 10:00,10:05,10:10 on ABC-1 then 10:15,10:20 on ABC-2 (all 5-min gaps)
        cap = 20 * 60
        events = [(dt(2026, 1, 3, 10, 0), "ABC-1"),
                  (dt(2026, 1, 3, 10, 5), "ABC-1"),
                  (dt(2026, 1, 3, 10, 10), "ABC-1"),
                  (dt(2026, 1, 3, 10, 15), "ABC-2"),
                  (dt(2026, 1, 3, 10, 20), "ABC-2")]
        gs = ts.group_seconds(events, cap, None, None)
        # the 10:10->10:15 gap is attributed to the earlier event's branch (ABC-1)
        self.assertEqual(gs["ABC-1"], 15 * 60)
        self.assertEqual(gs["ABC-2"], 5 * 60)

    def test_session_label_marks_extra_branches(self):
        gs = {"ABC-1": 900.0, "ABC-2": 300.0}
        self.assertEqual(ts.session_label(gs), "ABC-1 +1")
        self.assertEqual(ts.session_label({"ABC-1": 900.0}), "ABC-1")
        self.assertEqual(ts.session_label({}), "—")


class UnionVsSum(unittest.TestCase):
    def test_overlapping_union_less_than_sum(self):
        ivals_a = [(dt(2026, 1, 8, 10, 0), dt(2026, 1, 8, 11, 0))]
        ivals_b = [(dt(2026, 1, 8, 10, 30), dt(2026, 1, 8, 11, 30))]
        self.assertEqual(ts.union_seconds(ivals_a + ivals_b), 90 * 60)

    def test_disjoint_union_equals_sum(self):
        ivals_a = [(dt(2026, 1, 8, 9, 0), dt(2026, 1, 8, 10, 0))]
        ivals_b = [(dt(2026, 1, 8, 11, 0), dt(2026, 1, 8, 12, 0))]
        self.assertEqual(ts.union_seconds(ivals_a + ivals_b), 120 * 60)


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
        self.assertIn("ABC-123", ts.report_by_session(sessions, 1200, None, None))

    def test_date_filter_excludes_out_of_range(self):
        from datetime import date
        args = self._args()
        sessions = ts.load_sessions(self.tmp, args, date(2026, 1, 5), date(2026, 1, 5))
        self.assertEqual(len(sessions), 1)
        self.assertIn("ABC-124", ts.report_by_session(sessions, 1200, None, None))

    def test_reports_run(self):
        args = self._args()
        sessions = ts.load_sessions(self.tmp, args, None, None)
        cap = args.cap * 60
        self.assertIn("ABC-123", ts.report_by_session(sessions, cap, None, None))
        self.assertIn("ABC-124", ts.report_by_branch(sessions, cap, None, None))
        self.assertIn("Active (real)", ts.report_by_day(sessions, cap, None, None))


class ReportShape(unittest.TestCase):
    def test_branch_report_splits_a_switching_session(self):
        tmp = tempfile.mkdtemp()
        _write(tmp, "p", "s1", "Mixed",
               [(dt(2026, 1, 3, 10, 0), "ABC-1"),
                (dt(2026, 1, 3, 10, 5), None),
                (dt(2026, 1, 3, 10, 10), "ABC-2"),
                (dt(2026, 1, 3, 10, 15), None)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        out = ts.report_by_branch(sessions, 1200, None, None)
        self.assertIn("ABC-1", out)
        self.assertIn("ABC-2", out)

    def test_has_header_total_and_legend(self):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "p", "s1", "ABC-1", "t",
                       [dt(2026, 1, 3, 10, 0) + timedelta(minutes=m) for m in range(0, 31, 5)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        out = ts.report_by_session(sessions, 1200, None, None)
        self.assertIn("| Branch | Session | Title | Active | Span | Per day |", out)
        self.assertIn("**Total**", out)
        self.assertIn("Real working time", out)

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


class ActivityBlocks(unittest.TestCase):
    def test_split_on_long_gap(self):
        cap = 20 * 60
        events = [(dt(2026, 1, 3, 10, 0), "ABC-1"), (dt(2026, 1, 3, 10, 5), "ABC-1"),
                  (dt(2026, 1, 3, 10, 10), "ABC-1"),
                  (dt(2026, 1, 3, 11, 0), "ABC-2"), (dt(2026, 1, 3, 11, 5), "ABC-2"),
                  (dt(2026, 1, 3, 11, 10), "ABC-2")]
        blocks = ts.activity_blocks(events, cap)
        self.assertEqual(len(blocks), 2)
        a0, b0, g0 = blocks[0]
        self.assertEqual((b0 - a0).total_seconds(), 600)
        self.assertEqual(g0, "ABC-1")
        a1, b1, g1 = blocks[1]
        self.assertEqual((b1 - a1).total_seconds(), 600)
        self.assertEqual(g1, "ABC-2")

    def test_dominant_group_in_block(self):
        cap = 20 * 60
        events = [(dt(2026, 1, 3, 10, 0), "ABC-1"), (dt(2026, 1, 3, 10, 5), "ABC-1"),
                  (dt(2026, 1, 3, 10, 10), "ABC-2"), (dt(2026, 1, 3, 10, 30), "ABC-2"),
                  (dt(2026, 1, 3, 10, 50), "ABC-2")]
        blocks = ts.activity_blocks(events, cap)
        self.assertEqual(len(blocks), 1)
        a, b, g = blocks[0]
        self.assertEqual((b - a).total_seconds(), 50 * 60)
        self.assertEqual(g, "ABC-2")

    def test_single_event_run_is_not_a_block(self):
        cap = 20 * 60
        events = [(dt(2026, 1, 3, 10, 0), "ABC-1"),
                  (dt(2026, 1, 3, 12, 0), "ABC-1")]  # 2h gap > cap: two lone events
        self.assertEqual(ts.activity_blocks(events, cap), [])

    def test_report_interval_lists_blocks(self):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "p", "s1", "ABC-1", "t",
                       [dt(2026, 1, 3, 10, 0) + timedelta(minutes=m) for m in (0, 5, 10)]
                       + [dt(2026, 1, 3, 12, 0) + timedelta(minutes=m) for m in (0, 5, 10)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        out = ts.report_by_interval(sessions, 1200, None, None)
        self.assertIn("| Time | Duration | Branch | Session |", out)
        self.assertIn("Total activity", out)
        self.assertEqual(out.count("0h10m"), 2)   # two block rows
        self.assertIn("0h20m", out)               # day total / grand total

    def test_report_interval_grouped_by_session(self):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "p", "s1", "ABC-1", "My session",
                       [dt(2026, 1, 3, 10, 0) + timedelta(minutes=m) for m in (0, 5, 10)]
                       + [dt(2026, 1, 3, 12, 0) + timedelta(minutes=m) for m in (0, 5, 10)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        out = ts.report_by_interval(sessions, 1200, None, None, group="session")
        self.assertIn("My session", out)
        self.assertIn("| Time | Duration | Branch |", out)
        self.assertIn("Session total", out)
        self.assertIn("0h20m", out)   # two 10-min blocks

    def test_interval_by_session_sorted_by_first_block(self):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "p", "late0001", "ABC-9", "LateSession",
                       [dt(2026, 1, 3, 11, 0) + timedelta(minutes=m) for m in (0, 5, 10)])
        _write_session(tmp, "p", "erly0001", "ABC-8", "EarlySession",
                       [dt(2026, 1, 3, 9, 0) + timedelta(minutes=m) for m in (0, 5, 10)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        out = ts.report_by_interval(sessions, 1200, None, None, group="session")
        self.assertLess(out.index("EarlySession"), out.index("LateSession"))


class ProjectGrouping(unittest.TestCase):
    def test_groups_by_project_dir(self):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "repoA", "a1", "ABC-1", "tA",
                       [dt(2026, 1, 3, 10, 0) + timedelta(minutes=m) for m in range(0, 31, 5)])
        _write_session(tmp, "repoB", "b1", "ABC-2", "tB",
                       [dt(2026, 1, 4, 10, 0) + timedelta(minutes=m) for m in range(0, 31, 5)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        out = ts.report_by_project(sessions, 1200, None, None)
        self.assertIn("| Project | Sessions | Active | Span | Per day |", out)
        self.assertIn("repoA", out)
        self.assertIn("repoB", out)
        self.assertIn("projects", out)

    def test_project_name_from_cwd_basename(self):
        tmp = tempfile.mkdtemp()
        _write(tmp, "-Users-me-Projects-myapp", "c1", "tC",
               [(dt(2026, 1, 3, 10, 0) + timedelta(minutes=m), "ABC-1") if m == 0
                else (dt(2026, 1, 3, 10, 0) + timedelta(minutes=m), None) for m in range(0, 31, 5)],
               cwd="/Users/me/Projects/myapp")
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        self.assertEqual(sessions[0]["project"], "myapp")
        self.assertIn("myapp", ts.report_by_project(sessions, 1200, None, None))

    def test_project_grouped_by_session(self):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "repoA", "a1", "ABC-1", "Alpha",
                       [dt(2026, 1, 3, 10, 0) + timedelta(minutes=m) for m in range(0, 31, 5)])
        _write_session(tmp, "repoB", "b1", "ABC-2", "Beta",
                       [dt(2026, 1, 3, 10, 0) + timedelta(minutes=m) for m in range(0, 31, 5)])
        args = ts.build_parser().parse_args([])
        args.projects_dir = tmp
        sessions = ts.load_sessions(tmp, args, None, None)
        out = ts.report_by_project(sessions, 1200, None, None, group="session")
        self.assertIn("**repoA**", out)
        self.assertIn("**repoB**", out)
        self.assertIn("Alpha", out)
        self.assertIn("Beta", out)
        self.assertIn("Project total", out)
        self.assertIn("| Branch | Session | Title | Active | Span | Per day |", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
