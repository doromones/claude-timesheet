#!/usr/bin/env python3
"""Report active working time across Claude Code sessions.

Reads the JSONL session transcripts under ~/.claude/projects, derives how much
time was actually spent (idle gaps above a cap are dropped), and prints a
Markdown report grouped per session (default), per branch, or per calendar day.

Sessions are grouped by their git branch. Many branches follow a KEY-123 naming
convention (issue trackers), so an optional collapse folds KEY-123-description
down to KEY-123; branches without that shape (main, develop, feature/x) are kept
as-is. A session is not necessarily tied to any issue.

Output is English; the calling skill translates labels to the chat language.
Pure standard library; Python 3.9+.
"""

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta

DEFAULT_CAP_MIN = 20
DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def branch_group(branch):
    """Collapse a KEY-123-description branch to KEY-123; otherwise keep it.

    Returns the raw branch when it does not start with a KEY-123 prefix, and
    None when there is no branch at all.
    """
    if not branch:
        return None
    m = KEY_RE.match(branch)
    return m.group(0) if m else branch


def iter_session_files(projects_dir, project_filter=None):
    if not os.path.isdir(projects_dir):
        return
    for proj in sorted(os.listdir(projects_dir)):
        ppath = os.path.join(projects_dir, proj)
        if not os.path.isdir(ppath):
            continue
        if project_filter and project_filter.lower() not in proj.lower():
            continue
        for fp in sorted(glob.glob(os.path.join(ppath, "*.jsonl"))):
            yield proj, fp


def parse_session(fp):
    """Extract the timeline and labels from one session transcript.

    Timestamps are converted to the machine's local timezone so that day
    boundaries line up with the wall clock rather than UTC.
    """
    events = []
    branch = None
    title = None
    session_id = None
    with open(fp, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("sessionId"):
                session_id = o["sessionId"]
            if o.get("gitBranch"):
                branch = o["gitBranch"]
            if o.get("customTitle"):
                title = o["customTitle"]
            ts = o.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    continue
                events.append(dt)
    events.sort()
    if session_id is None:
        session_id = os.path.splitext(os.path.basename(fp))[0]
    return {
        "events": events,
        "branch": branch,
        "group": branch_group(branch),
        "title": title,
        "session_id": session_id,
        "file": fp,
    }


# --------------------------------------------------------------------------- #
# Time math
# --------------------------------------------------------------------------- #
def active_intervals(events, cap_sec):
    """Intervals between consecutive events whose gap is within the idle cap."""
    out = []
    for i in range(1, len(events)):
        a, b = events[i - 1], events[i]
        if (b - a).total_seconds() <= cap_sec:
            out.append((a, b))
    return out


def split_at_midnight(intervals):
    """Cut each interval on local midnight, yielding (day, start, end) pieces."""
    for a, b in intervals:
        cur = a
        while cur.date() != b.date():
            nxt = datetime.combine(cur.date() + timedelta(days=1), time.min, tzinfo=cur.tzinfo)
            yield (cur.date(), cur, nxt)
            cur = nxt
        yield (cur.date(), cur, b)


def per_day_active(events, cap_sec):
    """Map each local day to its active seconds and to its active pieces."""
    seconds = defaultdict(float)
    pieces = defaultdict(list)
    for d, s, e in split_at_midnight(active_intervals(events, cap_sec)):
        seconds[d] += (e - s).total_seconds()
        pieces[d].append((s, e))
    return seconds, pieces


def per_day_span(events):
    """Map each local day to first->last event span in seconds."""
    byday = defaultdict(list)
    for dt in events:
        byday[dt.date()].append(dt)
    return {d: (max(v) - min(v)).total_seconds() for d, v in byday.items()}


def merge_intervals(intervals):
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for s, e in ordered[1:]:
        if s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def union_seconds(intervals):
    return sum((e - s).total_seconds() for s, e in merge_intervals(intervals))


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
def in_range(day, since, until):
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def resolve_range(args):
    today = date.today()
    if args.today:
        return today, today
    if args.week:
        return today - timedelta(days=6), today
    since = date.fromisoformat(args.since) if args.since else None
    until = date.fromisoformat(args.until) if args.until else None
    return since, until


# --------------------------------------------------------------------------- #
# Formatting (English Markdown; the skill localizes labels for chat)
# --------------------------------------------------------------------------- #
def hm(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _cell(text):
    return (text or "—").replace("|", "/").replace("\n", " ")


def _per_day_cell(active, days):
    return " · ".join(f"{d.strftime('%m-%d')} {hm(active.get(d, 0.0))}" for d in days) or "—"


LEGEND = (
    "_Active = working time (idle gaps over the cap are dropped). "
    "Span = first→last event of the day (includes idle). "
    "Real = whole-selection working time with overlapping sessions merged._"
)


def report_by_session(sessions, cap_sec, since, until):
    ordered = sorted(sessions, key=lambda s: s["events"][-1], reverse=True)
    rows = []
    total_active = 0.0
    total_span = 0.0
    groups = set()
    all_pieces = []
    for s in ordered:
        active, pieces = per_day_active(s["events"], cap_sec)
        span = per_day_span(s["events"])
        days = sorted(set(d for d in active if in_range(d, since, until))
                      | set(d for d in span if in_range(d, since, until)))
        if not days:
            continue
        s_active = sum(active.get(d, 0.0) for d in days)
        s_span = sum(span.get(d, 0.0) for d in days)
        total_active += s_active
        total_span += s_span
        if s["group"]:
            groups.add(s["group"])
        for d in days:
            all_pieces.extend(pieces.get(d, []))
        rows.append(f"| {_cell(s['group'])} | `{s['session_id'][:8]}` | {_cell(s['title'])} | "
                    f"{hm(s_active)} | {hm(s_span)} | {_per_day_cell(active, days)} |")
    real = union_seconds(all_pieces)
    out = ["| Branch | Session | Title | Active | Span | Per day |",
           "|---|---|---|---|---|---|"]
    out.extend(rows)
    out.append(f"| **Total** |  |  | **{hm(total_active)}** | **{hm(total_span)}** | "
               f"**{len(rows)} sessions · {len(groups)} branches** |")
    out.append("")
    out.append(f"**Real working time over the selection (overlaps merged): {hm(real)}**")
    out.append("")
    out.append(LEGEND)
    return "\n".join(out)


def report_by_branch(sessions, cap_sec, since, until):
    by_group = defaultdict(list)
    for s in sessions:
        by_group[s["group"] or "—"].append(s)
    total_span = 0.0
    branches = 0
    all_pieces = []
    branch_rows = []
    for group in sorted(by_group):
        members = by_group[group]
        pieces_by_day = defaultdict(list)
        events_by_day = defaultdict(list)
        title = None
        for s in members:
            _, pieces = per_day_active(s["events"], cap_sec)
            for d, ps in pieces.items():
                pieces_by_day[d].extend(ps)
            for dt in s["events"]:
                events_by_day[dt.date()].append(dt)
            title = title or s["title"]
        days = sorted(d for d in (set(pieces_by_day) | set(events_by_day)) if in_range(d, since, until))
        if not days:
            continue
        pieces_in_range = []
        for d in days:
            pieces_in_range.extend(pieces_by_day.get(d, []))
        b_active = union_seconds(pieces_in_range)
        b_span = sum((max(events_by_day[d]) - min(events_by_day[d])).total_seconds()
                     for d in days if len(events_by_day.get(d, [])) > 1)
        total_span += b_span
        branches += 1
        all_pieces.extend(pieces_in_range)
        active_by_day = {d: union_seconds(pieces_by_day.get(d, [])) for d in days}
        branch_rows.append(f"| {_cell(group)} | {_cell(title)} | {len(members)} | "
                           f"{hm(b_active)} | {hm(b_span)} | {_per_day_cell(active_by_day, days)} |")
    real = union_seconds(all_pieces)
    out = ["| Branch | Title | Sessions | Active | Span | Per day |",
           "|---|---|---|---|---|---|"]
    out.extend(branch_rows)
    out.append(f"| **Total** |  |  |  | **{hm(total_span)}** | **{branches} branches** |")
    out.append("")
    out.append(f"**Real working time over the selection (overlaps merged): {hm(real)}**")
    out.append("")
    out.append(LEGEND)
    return "\n".join(out)


def report_by_day(sessions, cap_sec, since, until):
    pieces_by_day = defaultdict(list)
    active_sum_by_day = defaultdict(float)
    events_by_day = defaultdict(list)
    sessions_by_day = defaultdict(set)
    groups_all = set()
    for s in sessions:
        active, pieces = per_day_active(s["events"], cap_sec)
        for d, ps in pieces.items():
            pieces_by_day[d].extend(ps)
        for d, sec in active.items():
            active_sum_by_day[d] += sec
        for dt in s["events"]:
            events_by_day[dt.date()].append(dt)
            sessions_by_day[dt.date()].add(s["session_id"])
        if s["group"]:
            groups_all.add(s["group"])

    days = sorted(d for d in events_by_day if in_range(d, since, until))
    rows = []
    tot_union = 0.0
    tot_sum = 0.0
    for d in days:
        u = union_seconds(pieces_by_day.get(d, []))
        ssum = active_sum_by_day.get(d, 0.0)
        ev = events_by_day.get(d, [])
        sp = (max(ev) - min(ev)).total_seconds() if len(ev) > 1 else 0.0
        tot_union += u
        tot_sum += ssum
        rows.append(f"| {d} | {hm(u)} | {hm(ssum)} | {hm(sp)} | {len(sessions_by_day[d])} |")
    out = ["| Date | Active (real) | Active (sum) | Span | Sessions |",
           "|---|---|---|---|---|"]
    out.extend(rows)
    out.append(f"| **Total** | **{hm(tot_union)}** | **{hm(tot_sum)}** | — | **{len(groups_all)} branches** |")
    out.append("")
    out.append("_Active (real) merges sessions that overlapped in time; "
               "Active (sum) adds each session's time (larger when you worked in parallel). "
               "Span = first→last event of the day._")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_sessions(projects_dir, args, since, until):
    sessions = []
    for _proj, fp in iter_session_files(projects_dir, args.project):
        s = parse_session(fp)
        if not s["events"]:
            continue
        if args.branch and args.branch.lower() not in (s["branch"] or "").lower():
            continue
        days = {dt.date() for dt in s["events"]}
        if not any(in_range(d, since, until) for d in days):
            continue
        sessions.append(s)
    return sessions


def build_parser():
    p = argparse.ArgumentParser(
        prog="timesheet",
        description="Active working time across Claude Code sessions, broken down by day.",
    )
    p.add_argument("--by", choices=["session", "branch", "day"], default="session",
                   help="grouping level (default: session)")
    p.add_argument("--branch", help="only sessions whose git branch contains this substring")
    p.add_argument("--project", help="filter by project directory name (substring)")
    p.add_argument("--since", help="from date YYYY-MM-DD (inclusive)")
    p.add_argument("--until", help="to date YYYY-MM-DD (inclusive)")
    p.add_argument("--today", action="store_true", help="only today")
    p.add_argument("--week", action="store_true", help="last 7 days")
    p.add_argument("--cap", type=int, default=DEFAULT_CAP_MIN,
                   help=f"idle cap in minutes (default: {DEFAULT_CAP_MIN})")
    p.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR,
                   help="root of ~/.claude/projects (for tests)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    since, until = resolve_range(args)
    cap_sec = args.cap * 60

    sessions = load_sessions(args.projects_dir, args, since, until)
    if not sessions:
        print("No sessions match these filters.")
        return 0

    rng = f"Period: {since or '…'} → {until or '…'} · " if (since or until) else ""
    print(f"{rng}idle cap {args.cap} min · local time\n")

    if args.by == "session":
        print(report_by_session(sessions, cap_sec, since, until))
    elif args.by == "branch":
        print(report_by_branch(sessions, cap_sec, since, until))
    else:
        print(report_by_day(sessions, cap_sec, since, until))
    return 0


if __name__ == "__main__":
    sys.exit(main())
