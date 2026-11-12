#!/usr/bin/env python3
"""Build the SVG cards for the GitHub profile README.

Runs in GitHub Actions (.github/workflows/profile-cards.yml) and writes a light
and a dark variant of every card into the output directory, which the workflow
publishes to the `output` branch. The README points at those files through
raw.githubusercontent.com, which caches for about five minutes. The hosted
card services it replaces cached for 24 hours, so a late push of older
commits left a stale streak on the profile for up to a day.

Every data source keeps its last good result in data.json. A failing API never
blanks a card; it keeps the previous numbers, and the contributions card shows
when its data was fetched.

    python scripts/generate.py --out dist            # fetch + render
    python scripts/generate.py --out dist --offline  # render from dist/data.json
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import math
import os
import sys
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools import subset
from fontTools.ttLib import TTFont

LOGIN = "OtabekJurabekov"
CF_HANDLE = "OtabekJurabekov"
LC_USER = "OtabekJurabekov"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=5))  # Tashkent, used only for the "updated" stamp
ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "assets" / "fonts"
SEED = ROOT / "assets" / "seed.json"

# Samarkand tilework: lapis ground, turquoise glaze, saffron gold.
THEMES = {
    "dark": dict(
        bg="#0F1F3D", rule="#22385F", ink="#EEF2FA", muted="#9AACCC", faint="#6F84AA",
        tq="#3CC6BC", gold="#F2B544", gold_text="#F2B544", cobalt="#6E97F2", rose="#EF7A6A",
        empty="#1C3159", silver="#C3CDDD", bronze="#D99A66", pattern_tq=0.8, pattern_gold=0.75,
    ),
    "light": dict(
        bg="#F1F5FB", rule="#D3DCEA", ink="#102044", muted="#4F6188", faint="#6F7F9F",
        tq="#14958D", gold="#D69A2D", gold_text="#8F5C00", cobalt="#2F5FD0", rose="#C9493A",
        empty="#DCE4F0", silver="#8D99AD", bronze="#B66A33", pattern_tq=0.75, pattern_gold=0.9,
    ),
}
FONT_CUTS = ("Display", "Text", "TextSemi")
FALLBACK = "'Segoe UI', Helvetica, Arial, sans-serif"
FAMILY = {cut: f"'BG-{cut}', {FALLBACK}" for cut in FONT_CUTS}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


# ---------------------------------------------------------------- fonts / svg

class Fonts:
    """Measures text with the real glyph advances and embeds per-card subsets."""

    def __init__(self) -> None:
        self.fonts = {cut: TTFont(FONT_DIR / f"Bricolage-{cut}.woff2") for cut in FONT_CUTS}
        self.cmap = {cut: f.getBestCmap() for cut, f in self.fonts.items()}
        self.hmtx = {cut: f["hmtx"].metrics for cut, f in self.fonts.items()}
        self.upm = {cut: f["head"].unitsPerEm for cut, f in self.fonts.items()}
        self._faces: dict[tuple[str, str], str] = {}

    def width(self, s: str, cut: str, size: float) -> float:
        cmap, hmtx, upm = self.cmap[cut], self.hmtx[cut], self.upm[cut]
        units = sum(hmtx[cmap[ord(ch)]][0] if ord(ch) in cmap else upm * 0.55 for ch in s)
        return units * size / upm

    def face(self, cut: str, chars: set[str]) -> str:
        text = "".join(sorted(chars))
        key = (cut, text)
        if key not in self._faces:
            font = TTFont(FONT_DIR / f"Bricolage-{cut}.woff2")  # fresh copy: subsetting mutates
            opts = subset.Options()
            opts.flavor = "woff2"
            sub = subset.Subsetter(opts)
            sub.populate(text=text)
            sub.subset(font)
            buf = io.BytesIO()
            font.save(buf)
            b64 = base64.b64encode(buf.getvalue()).decode()
            self._faces[key] = f"@font-face{{font-family:'BG-{cut}';src:url(data:font/woff2;base64,{b64}) format('woff2')}}"
        return self._faces[key]


class Card:
    def __init__(self, fonts: Fonts, theme: str, w: int, h: int, label: str) -> None:
        self.fonts, self.t, self.w, self.h, self.label = fonts, THEMES[theme], w, h, label
        self.body: list[str] = []
        self.defs: list[str] = []
        self.css: list[str] = []
        self.used: dict[str, set[str]] = {cut: set() for cut in FONT_CUTS}

    def c(self, key: str) -> str:
        return self.t.get(key, key)

    def width(self, s: str, cut: str, size: float) -> float:
        return self.fonts.width(s, cut, size)

    def text(self, x: float, y: float, s: str, cut: str = "Text", size: float = 16, fill: str = "ink",
             anchor: str = "start", max_width: float | None = None) -> float:
        """Draw text; shrink it if it would overflow max_width. Returns the drawn width."""
        if max_width is not None:
            w = self.width(s, cut, size)
            if w > max_width:
                size = size * max_width / w
        self.used[cut].update(s)
        anchor_attr = "" if anchor == "start" else f' text-anchor="{anchor}"'
        self.body.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FAMILY[cut]}" font-size="{size:.2f}" '
            f'fill="{self.c(fill)}"{anchor_attr}>{escape(s)}</text>')
        return self.width(s, cut, size)

    def add(self, raw: str) -> None:
        self.body.append(raw)

    def frame(self, radius: int = 18) -> None:
        self.add(f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="{radius}" '
                 f'fill="{self.c("bg")}" stroke="{self.c("rule")}"/>')

    def render(self) -> str:
        faces = "".join(self.fonts.face(cut, chars) for cut, chars in self.used.items() if chars)
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        label = escape(self.label, {'"': "&quot;"})
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{label}">'
                f"<title>{escape(self.label)}</title><style>{faces}{''.join(self.css)}</style>"
                f"{defs}{''.join(self.body)}</svg>\n")


# ---------------------------------------------------------------- geometry

KHATAM = math.cos(math.pi / 4) / math.cos(math.pi / 8)


def star_d(cx: float, cy: float, r: float, rot: float = 0.0, inner_ratio: float = KHATAM) -> str:
    """Eight-point star. The default ratio is the khatam {8/2} of girih tiling (two squares)."""
    inner = r * inner_ratio
    pts = []
    for k in range(16):
        a = math.radians(rot + k * 22.5)
        rr = r if k % 2 == 0 else inner
        pts.append(f"{cx + rr * math.cos(a):.2f},{cy + rr * math.sin(a):.2f}")
    return "M" + "L".join(pts) + "Z"


def polygram_d(cx: float, cy: float, r: float, n: int = 8, step: int = 3, rot: float = 0.0) -> str:
    """Regular star polygon {n/step}, drawn as one continuous line."""
    pts = []
    for k in range(n):
        a = math.radians(rot + 360 * ((k * step) % n) / n)
        pts.append(f"{cx + r * math.cos(a):.2f},{cy + r * math.sin(a):.2f}")
    return "M" + "L".join(pts) + "Z"


# ---------------------------------------------------------------- data

def http_json(url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"User-Agent": "profile-cards (github.com/OtabekJurabekov)", "Accept": "application/json"}
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


def gh_graphql(query: str, variables: dict, token: str) -> dict:
    out = http_json("https://api.github.com/graphql", {"query": query, "variables": variables},
                    {"Authorization": f"bearer {token}"})
    if out.get("errors"):
        raise RuntimeError(out["errors"])
    return out["data"]


CALENDAR_Q = """query($login:String!,$from:DateTime!,$to:DateTime!){user(login:$login){
contributionsCollection(from:$from,to:$to){contributionCalendar{weeks{contributionDays{date contributionCount}}}}}}"""


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_calendar(token: str | None) -> dict:
    """The same per-day counts as the contribution grid, from account creation to today."""
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set")
    created = gh_graphql("query($login:String!){user(login:$login){createdAt}}", {"login": LOGIN}, token)
    start = dt.datetime.fromisoformat(created["user"]["createdAt"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.timezone.utc)
    days: dict[str, int] = {}
    cur = start
    while cur < now:  # the API allows at most one year per request
        end = min(cur + dt.timedelta(days=365) - dt.timedelta(seconds=1), now)
        data = gh_graphql(CALENDAR_Q, {"login": LOGIN, "from": iso(cur), "to": iso(end)}, token)
        for week in data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                days[day["date"]] = max(days.get(day["date"], 0), day["contributionCount"])
        cur = end + dt.timedelta(seconds=1)
    if not days:
        raise RuntimeError("empty contribution calendar")
    return {"created": start.date().isoformat(), "days": dict(sorted(days.items()))}


def fetch_codeforces() -> dict:
    info = http_json(f"https://codeforces.com/api/user.info?handles={CF_HANDLE}")
    hist = http_json(f"https://codeforces.com/api/user.rating?handle={CF_HANDLE}")
    if info.get("status") != "OK" or hist.get("status") != "OK":
        raise RuntimeError(f"codeforces status {info.get('status')}/{hist.get('status')}")
    u = info["result"][0]
    return {
        "rating": u["rating"], "rank": u["rank"], "max_rating": u["maxRating"], "max_rank": u["maxRank"],
        "contests": len(hist["result"]),
        "history": [[h["ratingUpdateTimeSeconds"], h["newRating"]] for h in hist["result"]],
    }


LEETCODE_Q = """query($u:String!){
matchedUser(username:$u){submitStatsGlobal{acSubmissionNum{difficulty count}}}
userContestRanking(username:$u){rating attendedContestsCount topPercentage}}"""


def fetch_leetcode() -> dict:
    out = http_json("https://leetcode.com/graphql", {"query": LEETCODE_Q, "variables": {"u": LC_USER}},
                    {"Referer": "https://leetcode.com/", "User-Agent": "Mozilla/5.0 (profile-cards)"})
    if out.get("errors") or not out.get("data", {}).get("matchedUser"):
        raise RuntimeError(out.get("errors") or "user not found")
    solved = {s["difficulty"]: s["count"] for s in out["data"]["matchedUser"]["submitStatsGlobal"]["acSubmissionNum"]}
    contest = out["data"]["userContestRanking"] or {}
    return {
        "solved": solved.get("All", 0), "easy": solved.get("Easy", 0), "medium": solved.get("Medium", 0),
        "hard": solved.get("Hard", 0), "rating": round(contest.get("rating") or 0),
        "contests": contest.get("attendedContestsCount") or 0, "top_percent": contest.get("topPercentage"),
    }


# ---------------------------------------------------------------- streaks

def daterange(a: dt.date, b: dt.date):
    for i in range((b - a).days + 1):
        yield a + dt.timedelta(days=i)


def streak_stats(days: dict[str, int]) -> dict:
    first = dt.date.fromisoformat(min(days))
    today = dt.date.fromisoformat(max(days))  # the calendar's last day is GitHub's "today"
    seq = [(d, days.get(d.isoformat(), 0)) for d in daterange(first, today)]

    longest = (0, None, None)
    run, run_start = 0, None
    for d, n in seq:
        if n > 0:
            if run == 0:
                run_start = d
            run += 1
            if run >= longest[0]:  # ties go to the most recent run
                longest = (run, run_start, d)
        else:
            run = 0

    # Today still counts as "in progress": a streak only breaks once a whole day passes empty.
    i = len(seq) - 1
    if seq[i][1] == 0:
        i -= 1
    cur_end = seq[i][0] if i >= 0 and seq[i][1] > 0 else None
    cur = 0
    while i >= 0 and seq[i][1] > 0:
        cur += 1
        i -= 1
    cur_start = seq[i + 1][0] if cur else None

    return {
        "today": today, "first": first, "total": sum(n for _, n in seq),
        "year_total": sum(n for d, n in seq if d.year == today.year),
        "current": cur, "current_start": cur_start, "current_end": cur_end,
        "longest": longest[0], "longest_start": longest[1], "longest_end": longest[2],
    }


def fmt_day(d: dt.date, with_year: bool = False) -> str:
    s = f"{MONTHS[d.month - 1]} {d.day}"
    return f"{s}, {d.year}" if with_year else s


def fmt_range(a: dt.date, b: dt.date, today: dt.date) -> str:
    if a == b:
        return fmt_day(a, a.year != today.year)
    if a.year != b.year:
        return f"{fmt_day(a, True)} – {fmt_day(b, True)}"
    return f"{fmt_day(a)} – {fmt_day(b)}" + (f", {b.year}" if b.year != today.year else "")


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


# ---------------------------------------------------------------- cards

def hero(card: Card) -> None:
    w, h = card.w, card.h
    card.defs.append(f'<clipPath id="hc"><rect x="0" y="0" width="{w}" height="{h}" rx="20"/></clipPath>')
    card.defs.append('<linearGradient id="hf" x1="540" y1="0" x2="820" y2="0" gradientUnits="userSpaceOnUse">'
                     '<stop offset="0" stop-color="#000"/><stop offset="1" stop-color="#fff"/></linearGradient>')
    card.defs.append(f'<mask id="hm"><rect x="0" y="0" width="{w}" height="{h}" fill="url(#hf)"/></mask>')
    card.css.append(
        ".g{animation:draw 1.6s cubic-bezier(.55,.1,.25,1) backwards}"
        "@keyframes draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}"
        "@media (prefers-reduced-motion:reduce){.g{animation:none}}")
    card.add(f'<rect x="0" y="0" width="{w}" height="{h}" rx="20" fill="{card.c("bg")}"/>')

    # Khatam lattice: eight-point stars whose tips touch, with {8/3} rosettes inside.
    r, s = 46.0, 92.0
    focus = (w - 60, 40)
    lines = []
    for row in range(-1, 6):
        for col in range(4, 13):
            cx, cy = col * s - 20, row * s + 30
            delay = math.hypot(cx - focus[0], cy - focus[1]) / 900
            common = f'pathLength="1" stroke-dasharray="1" fill="none" class="g" style="animation-delay:{delay:.2f}s"'
            lines.append(f'<path d="{star_d(cx, cy, r)}" stroke="{card.c("tq")}" stroke-width="1.6" '
                         f'stroke-opacity="{card.t["pattern_tq"]}" {common}/>')
            lines.append(f'<path d="{polygram_d(cx, cy, r * 0.56, rot=22.5)}" stroke="{card.c("gold")}" '
                         f'stroke-width="1.3" stroke-opacity="{card.t["pattern_gold"]}" {common}/>')
    card.add(f'<g clip-path="url(#hc)"><g mask="url(#hm)">{"".join(lines)}</g></g>')
    card.add(f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="20" fill="none" stroke="{card.c("rule")}"/>')

    x = 56
    card.text(x, 92, "Tashkent, Uzbekistan", "TextSemi", 18, "muted")
    card.text(x, 166, "Otabek Jurabekov", "Display", 78, "ink", max_width=560)
    card.text(x, 214, "Backend engineer building fintech at Revolution Group.", "Text", 21, "ink", max_width=540)
    card.text(x, 244, "Competitive programmer: IOI 2023, ICPC regional gold.", "Text", 21, "ink", max_width=540)
    card.text(x, h - 40, "The word “algorithm” comes from al-Khwārizmī of Khwarazm, today’s Uzbekistan.",
              "Text", 14.5, "faint", max_width=620)


def contributions(card: Card, st: dict, fetched: str) -> None:
    card.frame()
    t = st["today"]
    x0 = 44

    # Left: current streak.
    card.add(f'<path d="{star_d(x0 + 8, 56, 9.5, inner_ratio=0.5)}" fill="{card.c("gold")}"/>')
    card.text(x0 + 26, 62, "Current streak", "TextSemi", 17, "muted")
    num = f"{st['current']:,}"
    nw = card.text(x0 - 3, 152, num, "Display", 104, "ink")
    card.text(x0 + nw + 8, 152, "day" if st["current"] == 1 else "days", "Text", 26, "muted")
    if st["current"]:
        card.text(x0, 188, fmt_range(st["current_start"], st["current_end"], t), "TextSemi", 18, "gold_text")
    else:
        card.text(x0, 188, "A contribution today starts a new one", "Text", 17, "muted")

    # Right: the last 26 weeks, one bar per day, streak days in gold.
    cx0, cx1, top, base = 384, 956, 44, 192
    start = t - dt.timedelta(days=26 * 7 - 1)
    span = list(daterange(start, t))
    counts = [st["days"].get(d.isoformat(), 0) for d in span]
    nonzero = sorted(c for c in counts if c > 0)
    cap = nonzero[int(len(nonzero) * 0.95) - 1] if len(nonzero) >= 20 else (max(counts) or 1)
    step = (cx1 - cx0) / len(span)
    bw = max(step - 1.15, 1.2)
    in_streak = (lambda d: st["current_start"] <= d <= st["current_end"]) if st["current"] else (lambda d: False)
    bars = []
    for i, (d, n) in enumerate(zip(span, counts)):
        x = cx0 + i * step
        if n == 0:
            bars.append(f'<rect x="{x:.2f}" y="{base - 2:.1f}" width="{bw:.2f}" height="2" fill="{card.c("empty")}"/>')
            continue
        hgt = max(4.0, (base - top) * min(1.0, math.sqrt(n / cap)))
        color = card.c("gold") if in_streak(d) else card.c("tq")
        bars.append(f'<rect x="{x:.2f}" y="{base - hgt:.2f}" width="{bw:.2f}" height="{hgt:.2f}" rx="0.8" fill="{color}"/>')
    card.add("".join(bars))
    for i, d in enumerate(span):
        if d.day == 1 and i < len(span) - 7:
            x = cx0 + i * step
            card.add(f'<line x1="{x:.2f}" y1="{base + 4}" x2="{x:.2f}" y2="{base + 9}" stroke="{card.c("rule")}"/>')
            card.text(x, base + 26, MONTHS[d.month - 1], "Text", 13.5, "faint")

    # Bottom row: the rest of the numbers, as one sentence-like line.
    card.add(f'<line x1="{x0}" y1="236.5" x2="{card.w - 44}" y2="236.5" stroke="{card.c("rule")}"/>')
    y = 270
    items = [
        ("Longest streak", plural(st["longest"], "day")),
        (f"Since {MONTHS[st['first'].month - 1]} {st['first'].year}", f"{st['total']:,} contributions"),
        (f"In {t.year}", f"{st['year_total']:,}"),
    ]
    x = x0
    for label, value in items:
        x += card.text(x, y, label, "Text", 15.5, "muted") + 8
        x += card.text(x, y, value, "TextSemi", 17, "ink") + 34
    stamp = dt.datetime.fromisoformat(fetched).astimezone(LOCAL_TZ)
    card.text(card.w - 44, y, f"Updated {fmt_day(stamp.date())}, {stamp:%H:%M} Tashkent time",
              "Text", 13.5, "faint", anchor="end")


CF_TIERS = [(1200, "Pupil"), (1400, "Specialist"), (1600, "Expert"), (1900, "Candidate master"),
            (2100, "Master"), (2300, "International master"), (2400, "Grandmaster")]


def competitive(card: Card, cf: dict, lc: dict) -> None:
    card.frame()
    card.add(f'<line x1="500.5" y1="40" x2="500.5" y2="{card.h - 40}" stroke="{card.c("rule")}"/>')

    # Codeforces
    x0, x1 = 44, 456
    card.text(x0, 62, "Codeforces", "TextSemi", 17, "muted")
    nw = card.text(x0 - 2, 148, str(cf["rating"]), "Display", 92, "ink")
    card.text(x0 + nw + 12, 148, cf["rank"].capitalize(), "TextSemi", 22, "cobalt")
    card.text(x0, 182, f"Max {cf['max_rating']}, {plural(cf['contests'], 'rated contest')}", "Text", 16.5, "muted")
    hist = [r for _, r in cf["history"]]
    if len(hist) >= 2:
        top, bot = 208, 262
        lo, hi = min(hist) - 60, max(hist) + 60
        ys = lambda v: bot - (v - lo) / (hi - lo) * (bot - top)
        tier = max(((th, name) for th, name in CF_TIERS if th <= cf["rating"]), default=None)
        if tier and lo < tier[0] < hi:
            y = ys(tier[0])
            card.add(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="{card.c("rule")}" stroke-dasharray="3 4"/>')
            card.text(x0, y - 6, f"{tier[1]} from {tier[0]}", "Text", 12.5, "faint")
        xs = [x0 + i * (x1 - x0) / (len(hist) - 1) for i in range(len(hist))]
        pts = " ".join(f"{x:.1f},{ys(v):.1f}" for x, v in zip(xs, hist))
        card.add(f'<polyline points="{pts}" fill="none" stroke="{card.c("cobalt")}" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
        card.add(f'<circle cx="{xs[-1]:.1f}" cy="{ys(hist[-1]):.1f}" r="3.6" fill="{card.c("cobalt")}"/>')

    # LeetCode
    x0, x1 = 544, 956
    card.text(x0, 62, "LeetCode", "TextSemi", 17, "muted")
    nw = card.text(x0 - 2, 148, str(lc["rating"]), "Display", 92, "ink")
    if lc.get("top_percent") is not None:
        card.text(x0 + nw + 12, 148, f"Top {lc['top_percent']:g}%", "TextSemi", 22, "gold_text")
    card.text(x0, 182, f"Contest rating, {plural(lc['contests'], 'contest')}", "Text", 16.5, "muted")
    parts = [("Easy", lc["easy"], "tq"), ("Medium", lc["medium"], "gold"), ("Hard", lc["hard"], "rose")]
    total = sum(n for _, n, _ in parts) or 1
    card.defs.append(f'<clipPath id="lcb"><rect x="{x0}" y="212" width="{x1 - x0}" height="10" rx="5"/></clipPath>')
    x, segs = float(x0), []
    for _, n, color in parts:
        wseg = (x1 - x0) * n / total
        segs.append(f'<rect x="{x:.2f}" y="212" width="{max(wseg - 2, 0):.2f}" height="10" fill="{card.c(color)}"/>')
        x += wseg
    card.add(f'<g clip-path="url(#lcb)">{"".join(segs)}</g>')
    x = float(x0)
    for name, n, color in parts:
        card.add(f'<circle cx="{x + 4:.1f}" cy="249" r="4" fill="{card.c(color)}"/>')
        x += 13 + card.text(x + 13, 254, f"{name} {n:,}", "Text", 14.5, "muted") + 20
    card.text(x1, 254, f"{lc['solved']:,} solved", "TextSemi", 16.5, "ink", anchor="end")


HONORS = [
    ("IOI 2023", "International Olympiad in Informatics", "Budapest, represented Uzbekistan", "tq"),
    ("ICPC 2023", "Uzbekistan Regional Contest", "Gold medal, 3rd place", "gold"),
    ("IZhO", "International Zhautykov Olympiad", "Bronze medal", "bronze"),
    ("SEAMO", "Southeast Asian Mathematical Olympiad", "Gold, silver and bronze medals", "gold"),
    ("HKIMO", "Hong Kong Int’l Mathematical Olympiad", "Silver medal", "silver"),
    ("AMC", "American Mathematics Competitions", "Gold award", "gold"),
]


def honors(card: Card) -> None:
    colw, rowh = 334, 122
    for i, (title, name, result, color) in enumerate(HONORS):
        x = 8 + (i % 3) * colw
        y = 22 + (i // 3) * rowh
        card.add(f'<path d="{star_d(x + 15, y + 15, 16, inner_ratio=0.5)}" fill="{card.c(color)}"/>')
        tx = x + 44
        card.text(tx, y + 23, title, "TextSemi", 22, "ink", max_width=colw - 56)
        card.text(tx, y + 52, name, "Text", 15.5, "muted", max_width=colw - 56)
        card.text(tx, y + 78, result, "TextSemi", 16, "ink", max_width=colw - 56)


# ---------------------------------------------------------------- main

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="dist", type=Path)
    ap.add_argument("--offline", action="store_true", help="render from the saved data.json only")
    args = ap.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    state = load_json(out / "data.json")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    sources = {"calendar": lambda: fetch_calendar(token), "codeforces": fetch_codeforces, "leetcode": fetch_leetcode}
    for key, fetch in sources.items():
        if args.offline:
            break
        try:
            state[key] = {**fetch(), "fetched": now}
            print(f"{key}: fetched")
        except Exception as exc:  # keep the last good data, but say so in the run log
            print(f"::warning::{key} fetch failed, keeping last good data: {exc}")
    seed = load_json(SEED)
    for key in ("codeforces", "leetcode"):
        state.setdefault(key, seed[key])
    if "calendar" not in state:
        print("::error::no contribution data, fetched or saved; nothing to render")
        return 1
    (out / "data.json").write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")

    st = streak_stats(state["calendar"]["days"])
    st["days"] = state["calendar"]["days"]
    print(f"streak: current {st['current']} ({st['current_start']} .. {st['current_end']}), "
          f"longest {st['longest']}, total {st['total']}")

    fonts = Fonts()
    alt = {
        "hero": "Otabek Jurabekov, backend engineer and competitive programmer from Tashkent",
        "contributions": (f"Current streak {st['current']} days, longest {st['longest']} days, "
                          f"{st['total']:,} contributions since {st['first'].year}"),
        "competitive": (f"Codeforces rating {state['codeforces']['rating']}, "
                        f"LeetCode contest rating {state['leetcode']['rating']}"),
        "honors": "Olympiad and contest honors",
    }
    sizes = {"hero": (1000, 330), "contributions": (1000, 300), "competitive": (1000, 300), "honors": (1000, 262)}
    for theme in THEMES:
        for name, (w, h) in sizes.items():
            card = Card(fonts, theme, w, h, alt[name])
            if name == "hero":
                hero(card)
            elif name == "contributions":
                contributions(card, st, state["calendar"]["fetched"])
            elif name == "competitive":
                competitive(card, state["codeforces"], state["leetcode"])
            else:
                honors(card)
            (out / f"{name}-{theme}.svg").write_text(card.render(), encoding="utf-8")
    print(f"wrote {len(sizes) * len(THEMES)} cards to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
