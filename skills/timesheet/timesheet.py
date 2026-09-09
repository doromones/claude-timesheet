#!/usr/bin/env python3
"""Report active working time across Claude Code sessions.

Reads the JSONL session transcripts under ~/.claude/projects, derives how much
time was actually spent (idle gaps above a cap are dropped), and prints a
Markdown report grouped per session (default), per branch, or per calendar day.

Every event carries the git branch that was checked out at that moment, so a
session that switched branches has its time split across them: `--by branch`
and `--by day` attribute each interval to the branch active during it, and the
per-session row is labelled by the branch where most of its time was spent.
Branches following a KEY-123-description convention collapse to KEY-123; other
branches (main, develop, feature/x) are kept as-is. A session is not necessarily
tied to any issue.

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
    """Return the session's (timestamp, branch-group) timeline and labels.

    gitBranch is carried forward: an event is tagged with the most recent branch
    seen up to that line, so branch switches within a session are preserved.
    Timestamps are converted to local time so day boundaries match the wall clock.
    """
    events = []
    raw_branches = set()
    current = None
    title = None
    session_id = None
    cwd = None
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
                current = o["gitBranch"]
                raw_branches.add(current)
            if o.get("cwd"):
                cwd = o["cwd"]
            if o.get("customTitle"):
                title = o["customTitle"]
            ts = o.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    continue
                events.append((dt, branch_group(current)))
    events.sort(key=lambda e: e[0])
    if session_id is None:
        session_id = os.path.splitext(os.path.basename(fp))[0]
    return {
        "events": events,
        "raw_branches": raw_branches,
        "title": title,
        "session_id": session_id,
        "cwd": cwd,
        "file": fp,
    }


# --------------------------------------------------------------------------- #
# Time math (events are (datetime, group) pairs)
# --------------------------------------------------------------------------- #
def active_segments(events, cap_sec):
    """Gaps within the idle cap, each tagged with the earlier event's group."""
    out = []
    for i in range(1, len(events)):
        (a, ga), (b, _gb) = events[i - 1], events[i]
        if (b - a).total_seconds() <= cap_sec:
            out.append((a, b, ga))
    return out


def split_at_midnight(segments):
    """Cut each segment on local midnight: (day, start, end, group)."""
    for a, b, g in segments:
        cur = a
        while cur.date() != b.date():
            nxt = datetime.combine(cur.date() + timedelta(days=1), time.min, tzinfo=cur.tzinfo)
            yield (cur.date(), cur, nxt, g)
            cur = nxt
        yield (cur.date(), cur, b, g)


def day_active(events, cap_sec):
    """Session totals: day -> active seconds, and day -> [(start, end)] pieces."""
    seconds = defaultdict(float)
    pieces = defaultdict(list)
    for d, a, b, _g in split_at_midnight(active_segments(events, cap_sec)):
        seconds[d] += (b - a).total_seconds()
        pieces[d].append((a, b))
    return seconds, pieces


def group_day_pieces(events, cap_sec):
    """group -> day -> [(start, end)] active pieces attributed to that group."""
    gdp = defaultdict(lambda: defaultdict(list))
    for d, a, b, g in split_at_midnight(active_segments(events, cap_sec)):
        gdp[g][d].append((a, b))
    return gdp


def group_seconds(events, cap_sec, since, until):
    """group -> active seconds within the date range."""
    by_g = defaultdict(float)
    for d, a, b, g in split_at_midnight(active_segments(events, cap_sec)):
        if in_range(d, since, until):
            by_g[g] += (b - a).total_seconds()
    return by_g


def per_day_span(events):
    """day -> first->last event span in seconds."""
    byday = defaultdict(list)
    for dt, _g in events:
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


def activity_blocks(events, cap_sec):
    """Maximal runs of activity separated by idle gaps over the cap.

    Yields (start, end, group) where the run has at least one within-cap gap and
    group is the branch that dominated the run by active duration.
    """
    blocks = []
    if len(events) < 2:
        return blocks
    run_start, prev_dt, prev_g = events[0][0], events[0][0], events[0][1]
    gdur = defaultdict(float)

    def close():
        if prev_dt > run_start:
            dom = max(gdur, key=gdur.get) if gdur else prev_g
            blocks.append((run_start, prev_dt, dom))

    for dt, g in events[1:]:
        if (dt - prev_dt).total_seconds() <= cap_sec:
            gdur[prev_g] += (dt - prev_dt).total_seconds()
        else:
            close()
            run_start = dt
            gdur = defaultdict(float)
        prev_dt, prev_g = dt, g
    close()
    return blocks


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


def _group_sort_key(g):
    return (g is None, g or "")


def session_label(gsec):
    """Dominant branch of a session, with '+N' when it touched others."""
    active = {g: v for g, v in gsec.items() if v > 0}
    if not active:
        return "—"
    dom = max(active, key=active.get)
    extra = len(active) - 1
    name = dom or "—"
    return f"{name} +{extra}" if extra else name


LEGEND = (
    "_Active = working time (idle gaps over the cap are dropped). "
    "Span = first→last event of the day (includes idle). "
    "Real = whole-selection working time with overlapping sessions merged. "
    "A multi-branch session is labelled by its dominant branch (+N others); its "
    "time is split per branch under `--by branch`._"
)


def _session_row(s, cap_sec, since, until):
    """One session's table row plus its aggregates, or None if out of range."""
    seconds, pieces = day_active(s["events"], cap_sec)
    span = per_day_span(s["events"])
    days = sorted(set(d for d in seconds if in_range(d, since, until))
                  | set(d for d in span if in_range(d, since, until)))
    if not days:
        return None
    s_active = sum(seconds.get(d, 0.0) for d in days)
    s_span = sum(span.get(d, 0.0) for d in days)
    gsec = group_seconds(s["events"], cap_sec, since, until)
    row = (f"| {_cell(session_label(gsec))} | `{s['session_id'][:8]}` | {_cell(s['title'])} | "
           f"{hm(s_active)} | {hm(s_span)} | {_per_day_cell(seconds, days)} |")
    return {"row": row, "active": s_active, "span": s_span,
            "pieces": [p for d in days for p in pieces.get(d, [])],
            "groups": {g for g, v in gsec.items() if v > 0 and g},
            "last": s["events"][-1][0]}


_SESSION_HEADER = ["| Branch | Session | Title | Active | Span | Per day |",
                   "|---|---|---|---|---|---|"]


def report_by_session(sessions, cap_sec, since, until):
    entries = [r for r in (_session_row(s, cap_sec, since, until) for s in sessions) if r]
    entries.sort(key=lambda r: r["last"], reverse=True)
    groups = set()
    all_pieces = []
    total_active = total_span = 0.0
    out = list(_SESSION_HEADER)
    for r in entries:
        out.append(r["row"])
        total_active += r["active"]
        total_span += r["span"]
        groups |= r["groups"]
        all_pieces.extend(r["pieces"])
    out.append(f"| **Total** |  |  | **{hm(total_active)}** | **{hm(total_span)}** | "
               f"**{len(entries)} sessions · {len(groups)} branches** |")
    out += ["", f"**Real working time over the selection (overlaps merged): {hm(union_seconds(all_pieces))}**",
            "", LEGEND]
    return "\n".join(out)


def report_by_branch(sessions, cap_sec, since, until):
    g_day = defaultdict(lambda: defaultdict(list))
    g_sessions = defaultdict(set)
    g_title = {}
    for s in sessions:
        gdp = group_day_pieces(s["events"], cap_sec)
        for g, daymap in gdp.items():
            touched = False
            for d, ps in daymap.items():
                if not in_range(d, since, until):
                    continue
                g_day[g][d].extend(ps)
                touched = True
            if touched:
                g_sessions[g].add(s["session_id"])
                g_title.setdefault(g, s["title"])
    rows = []
    branches = 0
    all_pieces = []
    for g in sorted(g_day, key=_group_sort_key):
        daymap = g_day[g]
        days = sorted(daymap)
        if not days:
            continue
        pieces = [p for d in days for p in daymap[d]]
        b_active = union_seconds(pieces)
        b_span = 0.0
        for d in days:
            ps = daymap[d]
            b_span += (max(e for _, e in ps) - min(a for a, _ in ps)).total_seconds()
        active_by_day = {d: union_seconds(daymap[d]) for d in days}
        branches += 1
        all_pieces.extend(pieces)
        rows.append(f"| {_cell(g)} | {_cell(g_title.get(g))} | {len(g_sessions[g])} | "
                    f"{hm(b_active)} | {hm(b_span)} | {_per_day_cell(active_by_day, days)} |")
    real = union_seconds(all_pieces)
    out = ["| Branch | Title | Sessions | Active | Span | Per day |",
           "|---|---|---|---|---|---|"]
    out.extend(rows)
    out.append(f"| **Total** |  |  |  | **{hm(real)}** | **{branches} branches** |")
    out.append("")
    out.append(f"**Real working time over the selection (overlaps merged): {hm(real)}**")
    out.append("")
    out.append(LEGEND)
    return "\n".join(out)


def _project_by_session(sessions, cap_sec, since, until):
    by_proj = defaultdict(list)
    for s in sessions:
        r = _session_row(s, cap_sec, since, until)
        if r:
            by_proj[s["project"]].append(r)
    out = []
    all_pieces = []
    for proj in sorted(by_proj, key=_group_sort_key):
        rows = sorted(by_proj[proj], key=lambda r: r["last"], reverse=True)
        p_active = sum(r["active"] for r in rows)
        p_pieces = [p for r in rows for p in r["pieces"]]
        all_pieces.extend(p_pieces)
        out.append(f"**{_cell(proj)}** — {len(rows)} sessions · real {hm(union_seconds(p_pieces))}")
        out.append("")
        out.extend(_SESSION_HEADER)
        for r in rows:
            out.append(r["row"])
        out.append(f"| **Project total** |  |  | **{hm(p_active)}** |  |  |")
        out.append("")
    out.append(f"**Real working time over the selection (overlaps merged): {hm(union_seconds(all_pieces))}**")
    out.append("")
    out.append(LEGEND)
    return "\n".join(out)


def report_by_project(sessions, cap_sec, since, until, group="day"):
    if group == "session":
        return _project_by_session(sessions, cap_sec, since, until)
    p_day = defaultdict(lambda: defaultdict(list))
    p_sessions = defaultdict(set)
    for s in sessions:
        _, pieces = day_active(s["events"], cap_sec)
        for d, ps in pieces.items():
            if in_range(d, since, until):
                p_day[s["project"]][d].extend(ps)
                p_sessions[s["project"]].add(s["session_id"])
    rows = []
    all_pieces = []
    for proj in sorted(p_day, key=_group_sort_key):
        daymap = p_day[proj]
        days = sorted(daymap)
        if not days:
            continue
        pieces = [p for d in days for p in daymap[d]]
        span = 0.0
        for d in days:
            ps = daymap[d]
            span += (max(e for _, e in ps) - min(a for a, _ in ps)).total_seconds()
        active_by_day = {d: union_seconds(daymap[d]) for d in days}
        all_pieces.extend(pieces)
        rows.append(f"| {_cell(proj)} | {len(p_sessions[proj])} | {hm(union_seconds(pieces))} | "
                    f"{hm(span)} | {_per_day_cell(active_by_day, days)} |")
    real = union_seconds(all_pieces)
    out = ["| Project | Sessions | Active | Span | Per day |",
           "|---|---|---|---|---|"]
    out.extend(rows)
    out.append(f"| **Total** |  | **{hm(real)}** |  | **{len(rows)} projects** |")
    out.append("")
    out.append(f"**Real working time over the selection (overlaps merged): {hm(real)}**")
    out.append("")
    out.append(LEGEND)
    return "\n".join(out)


def report_by_day(sessions, cap_sec, since, until):
    pieces_by_day = defaultdict(list)
    sum_by_day = defaultdict(float)
    events_by_day = defaultdict(list)
    sessions_by_day = defaultdict(set)
    groups_all = set()
    for s in sessions:
        seconds, pieces = day_active(s["events"], cap_sec)
        for d, ps in pieces.items():
            pieces_by_day[d].extend(ps)
        for d, sec in seconds.items():
            sum_by_day[d] += sec
        for dt, _g in s["events"]:
            events_by_day[dt.date()].append(dt)
            sessions_by_day[dt.date()].add(s["session_id"])
        groups_all.update(g for g, v in group_seconds(s["events"], cap_sec, since, until).items() if v > 0 and g)

    days = sorted(d for d in events_by_day if in_range(d, since, until))
    rows = []
    tot_union = 0.0
    tot_sum = 0.0
    for d in days:
        u = union_seconds(pieces_by_day.get(d, []))
        ssum = sum_by_day.get(d, 0.0)
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


def _session_blocks(s, cap_sec, since, until):
    """In-range activity blocks of one session as sorted (start, end, group)."""
    out = []
    for a, b, g in activity_blocks(s["events"], cap_sec):
        for d, sa, sb, gg in split_at_midnight([(a, b, g)]):
            if in_range(d, since, until):
                out.append((sa, sb, gg))
    out.sort()
    return out


_INTERVAL_LEGEND = ("_A block is a continuous run of work with no idle gap over the cap. "
                    "Parallel sessions overlap in time, so this total sums them "
                    "(use `--by day` for merged wall-clock)._")


def report_by_interval(sessions, cap_sec, since, until, group="day"):
    if group == "session":
        return _interval_by_session(sessions, cap_sec, since, until)
    day_rows = defaultdict(list)
    for s in sessions:
        for sa, sb, gg in _session_blocks(s, cap_sec, since, until):
            day_rows[sa.date()].append((sa, sb, gg, s["title"], s["session_id"]))
    out = []
    total = 0.0
    for d in sorted(day_rows):
        out.append(f"**{d}**")
        out.append("")
        out.append("| Time | Duration | Branch | Session |")
        out.append("|---|---|---|---|")
        day_total = 0.0
        for sa, sb, gg, title, sid in sorted(day_rows[d], key=lambda r: r[0]):
            dur = (sb - sa).total_seconds()
            day_total += dur
            out.append(f"| {sa.strftime('%H:%M')}–{sb.strftime('%H:%M')} | {hm(dur)} | "
                       f"{_cell(gg)} | `{sid[:8]}` {_cell(title)} |")
        out.append(f"| **Day total** | **{hm(day_total)}** |  |  |")
        out.append("")
        total += day_total
    out.append(f"**Total activity over the selection: {hm(total)}**")
    out.append("")
    out.append(_INTERVAL_LEGEND)
    return "\n".join(out)


def _interval_by_session(sessions, cap_sec, since, until):
    entries = []
    for s in sessions:
        blocks = _session_blocks(s, cap_sec, since, until)
        if blocks:
            entries.append((blocks[0][0], s, blocks))
    entries.sort(key=lambda e: e[0])
    out = []
    total = 0.0
    for _start, s, blocks in entries:
        s_total = sum((b - a).total_seconds() for a, b, _ in blocks)
        total += s_total
        label = session_label(group_seconds(s["events"], cap_sec, since, until))
        out.append(f"**{_cell(label)}** · `{s['session_id'][:8]}` · {_cell(s['title'])}")
        out.append("")
        out.append("| Time | Duration | Branch |")
        out.append("|---|---|---|")
        for a, b, g in blocks:
            out.append(f"| {a.strftime('%m-%d %H:%M')}–{b.strftime('%H:%M')} | "
                       f"{hm((b - a).total_seconds())} | {_cell(g)} |")
        out.append(f"| **Session total** | **{hm(s_total)}** |  |")
        out.append("")
    out.append(f"**Total activity over the selection: {hm(total)}**")
    out.append("")
    out.append(_INTERVAL_LEGEND)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_sessions(projects_dir, args, since, until):
    sessions = []
    for proj, fp in iter_session_files(projects_dir, args.project):
        s = parse_session(fp)
        if not s["events"]:
            continue
        s["project"] = os.path.basename(s["cwd"]) if s["cwd"] else proj
        if args.branch and not any(args.branch.lower() in (b or "").lower() for b in s["raw_branches"]):
            continue
        days = {dt.date() for dt, _g in s["events"]}
        if not any(in_range(d, since, until) for d in days):
            continue
        sessions.append(s)
    return sessions


def build_parser():
    p = argparse.ArgumentParser(
        prog="timesheet",
        description="Active working time across Claude Code sessions, broken down by day.",
    )
    p.add_argument("--by", choices=["session", "branch", "project", "day", "interval"], default="session",
                   help="grouping level (default: session); 'interval' lists activity blocks")
    p.add_argument("--group", choices=["day", "session"], default="day",
                   help="--by interval: day timeline (default) or per session; "
                        "--by project: aggregate (default) or list sessions under each project")
    p.add_argument("--branch", help="only sessions that were on a git branch matching this substring")
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
    elif args.by == "project":
        print(report_by_project(sessions, cap_sec, since, until, args.group))
    elif args.by == "interval":
        print(report_by_interval(sessions, cap_sec, since, until, args.group))
    else:
        print(report_by_day(sessions, cap_sec, since, until))
    return 0


if __name__ == "__main__":
    sys.exit(main())
