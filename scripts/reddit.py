#!/usr/bin/env python3
"""Bookkeeping for the /reddit-daily skill. Stdlib only.

Claude cannot read or post on Reddit (see config.json -> account._posting_path_note),
so this tool never touches the network. It builds the day's search links for Jeet,
works out which phase the account is in, enforces the promo-ratio limits from the
log, and catches near-duplicate comments before they're posted.

    reddit.py pack [--date YYYY-MM-DD]      today's search links
    reddit.py status                        phase, gates, today's headroom
    reddit.py karma --total N [--created YYYY-MM-DD]
    reddit.py dupcheck --text-file PATH     exit 1 on any hyphen/dash outside a URL, or if too similar to a past comment
    reddit.py add --sub S --thread URL --type value|mention|reply|post --text-file PATH
                  [--title T] [--link URL] [--permalink URL] [--score N] [--note N]
    reddit.py mark --id N --status posted|removed [--permalink URL] [--note N]
"""
import argparse
import datetime as dt
import difflib
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
COMMENTS = ROOT / "log" / "comments.jsonl"
ACCOUNT = ROOT / "log" / "account.jsonl"
IST = ZoneInfo("Asia/Kolkata")
DUP_THRESHOLD = 0.6
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now_ist():
    return dt.datetime.now(IST)


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def append_jsonl(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def account_state(today):
    snaps = read_jsonl(ACCOUNT)
    created = CONFIG["account"]["created"]
    karma = None
    karma_date = None
    for s in snaps:
        created = s.get("created") or created
        if s.get("total_karma") is not None:
            karma, karma_date = s["total_karma"], s["date"]
    age = (today - dt.date.fromisoformat(created)).days
    gate = CONFIG["phases"]["warmup"]["exit_gate"]
    established = karma is not None and age >= gate["min_account_age_days"] and karma >= gate["min_total_karma"]
    return {
        "created": created,
        "age_days": age,
        "karma": karma,
        "karma_date": karma_date,
        "phase": "established" if established else "warmup",
        "days_to_gate": max(0, gate["min_account_age_days"] - age),
        "karma_to_gate": None if karma is None else max(0, gate["min_total_karma"] - karma),
    }


def attempted(rows):
    # Removed comments still count toward every limit: a removed mention in a sub is
    # the strongest possible signal not to try again there soon.
    return [r for r in rows if r.get("status") in ("posted", "removed")]


def cmd_status(args):
    now = now_ist()
    today = now.date()
    acct = account_state(today)
    rows = read_jsonl(COMMENTS)
    live = attempted(rows)
    week_ago = today - dt.timedelta(days=7)
    in_week = [r for r in live if dt.date.fromisoformat(r["date"]) > week_ago]
    today_rows = [r for r in live if r["date"] == today.isoformat()]
    last30 = live[-30:]
    mentions30 = sum(1 for r in last30 if r.get("mentions_yuvgro"))

    print(f"Account u/{CONFIG['account']['username']} — created {acct['created']}, {acct['age_days']} days old")
    if acct["karma"] is None:
        print("Karma: UNKNOWN — ask Jeet for the total karma on his profile, then run `karma --total N`")
    else:
        print(f"Karma: {acct['karma']} (as of {acct['karma_date']})")

    ph = CONFIG["phases"][acct["phase"]]
    print(f"\nPHASE: {ph['label']}")
    if acct["phase"] == "warmup":
        k = "?" if acct["karma_to_gate"] is None else acct["karma_to_gate"]
        print(f"  Gate to phase 2: {acct['days_to_gate']} more day(s) AND {k} more karma. No YuvGro mentions, no yuvgro.com links.")
        print("  Links:", "none (week 1)" if acct["age_days"] < 7 else "neutral sources only")

    spd = CONFIG["session"]["comments_per_day"]
    print(f"\nToday ({today}, IST): {len(today_rows)} posted — target {spd['target']}, max {spd['max']}")
    in_window = "22:00" <= now.strftime("%H:%M") <= "23:30"
    print(f"Session window 22:00–23:30 IST: {'IN window' if in_window else 'outside window (' + now.strftime('%H:%M') + ') — fine, note it in the report'}")
    print(f"Last 7 days: {len(in_week)} posted")

    per_sub = {}
    for r in in_week:
        per_sub[r["sub"]] = per_sub.get(r["sub"], 0) + 1
    cap = ph["max_comments_per_sub_per_week"]
    if per_sub:
        print(f"Per-sub this week (cap {cap}): " + ", ".join(f"r/{s} {n}{' FULL' if n >= cap else ''}" for s, n in sorted(per_sub.items())))

    print(f"\nMention ratio (last {len(last30)} posted): {mentions30}/{len(last30)}")
    if acct["phase"] == "established":
        p = CONFIG["phases"]["established"]
        # Judged against the comments actually posted, so a thin log (< 10) allows no mention at all.
        allowed = int(p["yuvgro_mention_ratio_max"] * len(last30))
        mentions_today = sum(1 for r in today_rows if r.get("mentions_yuvgro"))
        links7 = sum(1 for r in in_week if r.get("yuvgro_link"))
        reasons = []
        if mentions30 + 1 > allowed:
            reasons.append(f"ratio: {mentions30} mention(s) in last {len(last30)}, allowed {allowed}")
        if mentions_today >= p["max_mentions_per_day"]:
            reasons.append("already mentioned YuvGro today")
        print(f"Can mention YuvGro now: {'NO — ' + '; '.join(reasons) if reasons else 'yes (one, on a mention-eligible thread)'}")
        print(f"yuvgro.com links in last 7 days: {links7}/{p['max_yuvgro_links_per_7_days']}"
              f"{' — no more links this week' if links7 >= p['max_yuvgro_links_per_7_days'] else ''}")
        recent = {}
        cutoff = today - dt.timedelta(days=p["min_days_between_mentions_in_same_sub"])
        for r in live:
            if r.get("mentions_yuvgro") and dt.date.fromisoformat(r["date"]) > cutoff:
                recent[r["sub"]] = r["date"]
        if recent:
            print("Subs on mention cooldown: " + ", ".join(f"r/{s} (last {d})" for s, d in sorted(recent.items())))
        posts_week = sum(1 for r in in_week if r["type"] == "post")
        print(f"Original posts this week: {posts_week}/{p['original_posts']['max_per_week']}")
    else:
        print("Can mention YuvGro now: NO — phase 1")

    removed = [r for r in rows if r.get("status") == "removed"]
    if removed:
        print(f"\nRemoved so far: {len(removed)} — " + ", ".join(f"#{r['id']} r/{r['sub']}" for r in removed[-5:]))


def cmd_pack(args):
    day = dt.date.fromisoformat(args.date) if args.date else now_ist().date()
    acct = account_state(day)
    topic_key = CONFIG["topic_rotation_by_weekday"][WEEKDAYS[day.weekday()]]
    topic = CONFIG["topics"][topic_key]
    tpl = CONFIG["search_url_templates"]
    warm = acct["phase"] == "warmup"

    subs = topic["subs"]
    if warm:
        ok = CONFIG["phases"]["warmup"]["prefer_subs"]
        subs = [s for s in subs if s in ok]
        # Backfill so the pack is never empty on a day whose topic subs are phase-2 only.
        for s in ["smallbusiness", "SEO", "Entrepreneur"]:
            if len(subs) >= 3:
                break
            if s not in subs:
                subs.append(s)
    subs = subs[:4]

    # Rotate the queries by ISO week so the same three don't come up every Monday.
    q = topic["queries"]
    start = day.isocalendar()[1] % len(q)
    queries = (q[start:] + q[:start])[:3]

    print(f"Search pack — {day} ({WEEKDAYS[day.weekday()]}) · topic: {topic_key} · {CONFIG['phases'][acct['phase']]['label']}\n")
    print("Newest posts (skim titles, open anything < 24h with a real question):")
    for s in subs:
        print(f"  r/{s}: {tpl['sub_new'].format(sub=s)}")
    print("\nSitewide searches, last 24h:")
    for query in queries:
        print(f"  {query}: {tpl['sitewide_search'].format(q=quote_plus(query))}")
    if not warm:
        g = queries[0].replace('"', "")
        print("\nEvergreen GEO thread (Google, last 6 months, not archived):")
        print(f"  {g}: {tpl['google_evergreen'].format(q=quote_plus(g))}")
    print("\nPaste back up to 10 promising threads: URL + title + post text (+ top comments if they matter).")


def cmd_karma(args):
    row = {"date": now_ist().date().isoformat(), "total_karma": args.total}
    if args.created:
        row["created"] = args.created
    append_jsonl(ACCOUNT, row)
    print(f"Recorded karma {args.total}" + (f", created {args.created}" if args.created else ""))


def similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower().split(), b.lower().split()).ratio()


HYPHENS = re.compile(r"[-‐-―−]")
URL = re.compile(r"https?://\S+")


def hyphen_hits(text):
    # Jeet's rule: no hyphens or dashes anywhere except inside URLs, where they're part of the address.
    stripped = URL.sub("", text)
    return [stripped[max(0, m.start() - 15):m.end() + 15].replace("\n", " ") for m in HYPHENS.finditer(stripped)]


def cmd_dupcheck(args):
    text = Path(args.text_file).read_text().strip()
    hits = hyphen_hits(text)
    if hits:
        print(f"dupcheck: {len(hits)} hyphen/dash found (not allowed outside URLs) — rewrite:")
        for h in hits:
            print(f"  …{h}…")
        return 1
    best = (-1.0, None)
    for r in read_jsonl(COMMENTS):
        s = similarity(text, r.get("text", ""))
        if s > best[0]:
            best = (s, r)
    if best[1] is None:
        print("dupcheck: no prior comments — OK")
        return 0
    s, r = best
    verdict = "TOO SIMILAR — rewrite" if s >= DUP_THRESHOLD else "OK"
    print(f"dupcheck: closest is #{r['id']} (r/{r['sub']}, {r['date']}) at {s:.2f} — {verdict}")
    return 1 if s >= DUP_THRESHOLD else 0


def cmd_add(args):
    rows = read_jsonl(COMMENTS)
    text = Path(args.text_file).read_text().strip()
    link = args.link or ""
    if "yuvgro.com" in link:
        yuvgro_link = link
    elif "yuvgro.com" in text.lower():
        yuvgro_link = "in text"
    else:
        yuvgro_link = None
    row = {
        "id": (rows[-1]["id"] + 1) if rows else 1,
        "date": now_ist().date().isoformat(),
        "ts": now_ist().isoformat(timespec="minutes"),
        "sub": args.sub.removeprefix("r/"),
        "thread_url": args.thread,
        "thread_title": args.title or "",
        "type": args.type,
        "mentions_yuvgro": args.type == "mention" or "yuvgro" in text.lower(),
        "yuvgro_link": yuvgro_link,
        "text": text,
        "permalink": args.permalink or "",
        "status": "posted",
        "fit_score": args.score,
        "note": args.note or "",
    }
    if account_state(now_ist().date())["phase"] == "warmup" and row["mentions_yuvgro"]:
        sys.exit("REFUSED: phase 1 comment mentions YuvGro. Remove the mention or wait for the gate.")
    append_jsonl(COMMENTS, row)
    print(f"Logged #{row['id']} r/{row['sub']} [{row['type']}]{' · YuvGro mention' if row['mentions_yuvgro'] else ''}")


def cmd_mark(args):
    rows = read_jsonl(COMMENTS)
    for r in rows:
        if r["id"] == args.id:
            r["status"] = args.status
            if args.permalink:
                r["permalink"] = args.permalink
            if args.note:
                r["note"] = (r.get("note", "") + " | " + args.note).strip(" |")
            write_jsonl(COMMENTS, rows)
            print(f"#{args.id} -> {args.status}")
            return
    sys.exit(f"No comment #{args.id}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("pack")
    p.add_argument("--date")
    p = sub.add_parser("karma")
    p.add_argument("--total", type=int, required=True)
    p.add_argument("--created")
    p = sub.add_parser("dupcheck")
    p.add_argument("--text-file", required=True)
    p = sub.add_parser("add")
    p.add_argument("--sub", required=True)
    p.add_argument("--thread", required=True)
    p.add_argument("--type", required=True, choices=["value", "mention", "reply", "post"])
    p.add_argument("--text-file", required=True)
    p.add_argument("--title")
    p.add_argument("--link")
    p.add_argument("--permalink")
    p.add_argument("--score", type=int)
    p.add_argument("--note")
    p = sub.add_parser("mark")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--status", required=True, choices=["posted", "removed"])
    p.add_argument("--permalink")
    p.add_argument("--note")
    args = ap.parse_args()
    fn = {"status": cmd_status, "pack": cmd_pack, "karma": cmd_karma, "dupcheck": cmd_dupcheck,
          "add": cmd_add, "mark": cmd_mark}[args.cmd]
    sys.exit(fn(args) or 0)


if __name__ == "__main__":
    main()
