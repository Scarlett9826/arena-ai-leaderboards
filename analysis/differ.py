"""Rank-change detection engine for MiMo leaderboard tracker.

Reads today's and a baseline date's snapshots from
``data/lmarena/{date}/*.json`` and ``data/artificial_analysis/{date}/llms.json``,
computes per-model deltas, classifies severity per ``analysis/watchlist.yaml``,
and writes:

    data/alerts/{today}.json   structured change list
    data/alerts/{today}.md     human-readable report grouped by severity

It also emits three lines on stdout, intended to be appended to
``$GITHUB_OUTPUT`` by the calling workflow::

    has_changes=true|false
    severity=ALERT|WARN|INFO|NONE
    summary_path=data/alerts/2026-06-30.md

CLI::

    python -m analysis.differ
    python -m analysis.differ --baseline-date 2026-06-29
    python -m analysis.differ --today 2026-06-30 --baseline-date 2026-06-29
    python -m analysis.differ --data-root /tmp/fake_data
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import yaml

# ---------------------------------------------------------------------------
# RankEntry: try the real schema from collectors first, fall back to a local
# definition so this module is usable on its own (for tests & dev).
# ---------------------------------------------------------------------------
try:
    from collectors.schema import (  # type: ignore[attr-defined]
        RankEntry,
        lmarena_to_entries,
        aa_to_entries,
    )

    _USING_COLLECTORS_SCHEMA = True
except Exception:  # pragma: no cover - exercised by tests/dev
    _USING_COLLECTORS_SCHEMA = False

    @dataclass
    class RankEntry:  # type: ignore[no-redef]
        source: str
        board: str
        snapshot_date: str
        model_id: str
        model_display: str
        organization: str
        rank: int | None
        score: float | None
        score_unit: str
        extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Minimal local parsers. The collectors agent is expected to provide
    # canonical versions; these are intentionally permissive so the
    # differ keeps working even if upstream renames a field.
    # ------------------------------------------------------------------
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

    def lmarena_to_entries(snapshot_json: dict) -> list[RankEntry]:  # type: ignore[no-redef]
        meta = snapshot_json.get("meta", {}) or {}
        board_name = meta.get("leaderboard") or meta.get("board") or "unknown"
        board = f"lmarena/{board_name}"
        snap_date = (meta.get("fetched_at") or "")[:10] or meta.get("snapshot_date", "")
        out: list[RankEntry] = []
        for m in snapshot_json.get("models", []) or []:
            display = m.get("model") or m.get("model_display") or ""
            org = m.get("vendor") or m.get("organization") or ""
            model_id = m.get("model_id") or _slug(f"{org}-{display}") if display else ""
            if not model_id:
                continue
            out.append(
                RankEntry(
                    source="lmarena",
                    board=board,
                    snapshot_date=snap_date,
                    model_id=model_id,
                    model_display=display,
                    organization=org,
                    rank=m.get("rank"),
                    score=m.get("score"),
                    score_unit="arena_elo",
                    extra={k: v for k, v in m.items() if k not in {"rank", "model", "vendor", "score"}},
                )
            )
        return out

    def aa_to_entries(snapshot_json: dict) -> list[RankEntry]:  # type: ignore[no-redef]
        meta = snapshot_json.get("meta", {}) or {}
        snap_date = (meta.get("fetched_at") or "")[:10] or meta.get("snapshot_date", "")
        out: list[RankEntry] = []
        models = snapshot_json.get("models") or snapshot_json.get("data") or []
        for m in models:
            display = m.get("model_display") or m.get("name") or m.get("model") or ""
            org = m.get("organization") or m.get("creator") or m.get("vendor") or ""
            model_id = m.get("model_id") or m.get("slug") or _slug(f"{org}-{display}")
            if not model_id:
                continue
            out.append(
                RankEntry(
                    source="artificial_analysis",
                    board="aa/intelligence_index",
                    snapshot_date=snap_date,
                    model_id=model_id,
                    model_display=display,
                    organization=org,
                    rank=m.get("rank"),
                    score=m.get("intelligence_index") or m.get("score"),
                    score_unit="intelligence_index",
                    extra={
                        k: v for k, v in m.items()
                        if k not in {"rank", "name", "model", "model_display",
                                     "organization", "creator", "vendor",
                                     "intelligence_index", "score"}
                    },
                )
            )
        return out


# ---------------------------------------------------------------------------
# Constants & types
# ---------------------------------------------------------------------------
SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "ALERT": 2}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Change:
    """One detected change for a single (source, board, model_id) triple."""
    source: str
    board: str
    model_id: str
    model_display: str
    organization: str

    # Either old or new may be None: new model / dropped model.
    old_rank: int | None
    new_rank: int | None
    rank_delta: int | None          # new - old; None if either side missing
    old_score: float | None
    new_score: float | None
    score_delta: float | None
    score_delta_pct: float | None
    score_unit: str

    kind: str                       # "new" | "dropped" | "changed"
    crossed_milestones: list[int]   # milestones whose threshold was crossed
    is_primary: bool
    is_competitor: bool
    severity: str                   # INFO | WARN | ALERT
    reasons: list[str]              # human-readable rule firings

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_watchlist(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _list_dates(dir_: Path) -> list[str]:
    if not dir_.is_dir():
        return []
    return sorted(
        p.name for p in dir_.iterdir()
        if p.is_dir() and DATE_RE.match(p.name)
    )


def find_today_and_baseline(
    data_root: Path,
    source_subdir: str,
    today: str | None,
    baseline: str | None,
) -> tuple[str | None, str | None]:
    """Resolve (today_date, baseline_date) for one source.

    today    : if explicit, use it; else latest available
    baseline : if explicit, use it; else second-newest *before* today
    Returns (None, _) if no dates at all.
    """
    dates = _list_dates(data_root / source_subdir)
    if not dates:
        return None, None

    if today is None:
        today_d = dates[-1]
    else:
        today_d = today

    if baseline is not None:
        baseline_d = baseline
    else:
        earlier = [d for d in dates if d < today_d]
        baseline_d = earlier[-1] if earlier else None

    return today_d, baseline_d


def load_lmarena_snapshot(date_dir: Path) -> list[RankEntry]:
    """Load every JSON file in an lmarena date dir, except _index.json."""
    out: list[RankEntry] = []
    if not date_dir.is_dir():
        return out
    for f in sorted(date_dir.glob("*.json")):
        if f.name.startswith("_") or f.name == "latest.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] failed to parse {f}: {e}", file=sys.stderr)
            continue
        # If the snapshot didn't record its subset, infer from filename.
        if isinstance(data, dict):
            meta = data.setdefault("meta", {})
            meta.setdefault("subset", f.stem)
            # Back-compat for older snapshot shape (used by our fallback parser
            # & by early lmarena collector revisions).
            meta.setdefault("leaderboard", f.stem)
        out.extend(lmarena_to_entries(data))
    return out


def load_aa_snapshot(date_dir: Path) -> list[RankEntry]:
    out: list[RankEntry] = []
    f = date_dir / "llms.json"
    if not f.is_file():
        return out
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] failed to parse {f}: {e}", file=sys.stderr)
        return out
    out.extend(aa_to_entries(data))
    return out


def load_all_entries(data_root: Path, lmarena_date: str | None, aa_date: str | None) -> list[RankEntry]:
    entries: list[RankEntry] = []
    if lmarena_date:
        entries.extend(load_lmarena_snapshot(data_root / "lmarena" / lmarena_date))
    if aa_date:
        entries.extend(load_aa_snapshot(data_root / "artificial_analysis" / aa_date))
    return entries


# ---------------------------------------------------------------------------
# Watchlist matching
# ---------------------------------------------------------------------------
def _icontains_any(haystack: str, needles: Iterable[str]) -> bool:
    h = (haystack or "").lower()
    return any(n.lower() in h for n in needles if n)


def is_primary(entry: RankEntry, wl: dict[str, Any]) -> bool:
    prim = wl.get("primary", {}) or {}
    org_needle = (prim.get("organization") or "").lower()
    if org_needle and org_needle in (entry.organization or "").lower():
        return True
    for sub in prim.get("model_id_substrings", []) or []:
        if sub and sub.lower() in (entry.model_id or "").lower():
            return True
        if sub and sub.lower() in (entry.model_display or "").lower():
            return True
    return False


def is_competitor(entry: RankEntry, wl: dict[str, Any]) -> bool:
    if _icontains_any(entry.organization, wl.get("competitors_organizations", []) or []):
        return True
    if _icontains_any(entry.model_display, wl.get("competitors_models", []) or []):
        return True
    if _icontains_any(entry.model_id, wl.get("competitors_models", []) or []):
        return True
    return False


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------
def _key(e: RankEntry) -> tuple[str, str, str]:
    return (e.source, e.board, e.model_id)


def _milestones_crossed(
    old_rank: int | None, new_rank: int | None, milestones: list[int]
) -> list[int]:
    """Return milestones whose threshold was crossed (either direction).

    A model crosses milestone M between ranks A and B iff
    min(A,B) <= M < max(A,B), i.e. M sits strictly between the two
    (we use ``<=`` on the lower side so that landing *exactly* on M counts
    as a crossing when coming from worse-than-M).
    """
    crossed: list[int] = []
    if old_rank is None or new_rank is None:
        # Treat new entries as crossing every milestone they sit at/above.
        present_rank = new_rank if old_rank is None else old_rank
        if present_rank is None:
            return crossed
        for m in milestones:
            if present_rank <= m:
                crossed.append(m)
        return crossed

    lo, hi = sorted([old_rank, new_rank])
    for m in milestones:
        # crossed if the milestone lies strictly between the two ranks,
        # inclusive on the "better" side: entering Top10 from #11 means
        # lo=10, hi=11 → 10 lies in [lo, hi) → crossed.
        if lo <= m < hi:
            crossed.append(m)
    return crossed


def _rate_severity(
    change: Change, wl: dict[str, Any]
) -> tuple[str, list[str]]:
    """Classify a Change into INFO / WARN / ALERT and collect reasons."""
    rules = wl.get("alert_rules", {}) or {}
    overrides = wl.get("severity_overrides", {}) or {}

    sev = "INFO"
    reasons: list[str] = []

    def bump(level: str, why: str) -> None:
        nonlocal sev
        if SEVERITY_ORDER[level] > SEVERITY_ORDER[sev]:
            sev = level
        reasons.append(why)

    # New / dropped
    if change.kind == "new" and rules.get("new_model", True):
        bump("WARN", "new on board")
    if change.kind == "dropped" and rules.get("dropped_model", True):
        bump("WARN", "dropped from board")

    # Rank movement
    if change.rank_delta is not None:
        amag = abs(change.rank_delta)
        a_warn = int(rules.get("rank_change_warn", 3))
        a_alert = int(rules.get("rank_change_alert", 8))
        if amag >= a_alert:
            bump("ALERT", f"rank moved {change.rank_delta:+d} (≥{a_alert})")
        elif amag >= a_warn:
            bump("WARN", f"rank moved {change.rank_delta:+d} (≥{a_warn})")
        elif amag >= 1:
            bump("INFO", f"rank moved {change.rank_delta:+d}")

    # Score movement (percentage)
    if change.score_delta_pct is not None:
        thr = float(rules.get("score_change_pct", 1.0))
        if abs(change.score_delta_pct) >= thr:
            level = "WARN" if abs(change.score_delta_pct) >= thr * 3 else "INFO"
            bump(level, f"score {change.score_delta_pct:+.2f}%")

    # Milestones crossed
    if change.crossed_milestones:
        bump(
            "WARN",
            "crossed milestones " + ", ".join(f"Top{m}" for m in change.crossed_milestones),
        )

    # Primary overrides
    if change.is_primary:
        min_sev = overrides.get("primary_min_severity", "WARN")
        if SEVERITY_ORDER.get(min_sev, 1) > SEVERITY_ORDER[sev]:
            sev = min_sev
            reasons.append(f"primary min-severity={min_sev}")

        mile_alert = set(overrides.get("primary_milestone_alert", []) or [])
        if mile_alert & set(change.crossed_milestones):
            hits = sorted(mile_alert & set(change.crossed_milestones))
            bump("ALERT", "primary crossed milestone " + ", ".join(f"Top{m}" for m in hits))

    return sev, reasons


def diff_entries(
    today_entries: list[RankEntry],
    baseline_entries: list[RankEntry],
    wl: dict[str, Any],
) -> list[Change]:
    """Pair today vs baseline by (source, board, model_id), build Change list."""
    rules = wl.get("alert_rules", {}) or {}
    milestones: list[int] = sorted(rules.get("rank_milestones", []) or [])

    today_map = {_key(e): e for e in today_entries}
    base_map = {_key(e): e for e in baseline_entries}
    all_keys = set(today_map) | set(base_map)

    changes: list[Change] = []
    for k in all_keys:
        t = today_map.get(k)
        b = base_map.get(k)
        ref = t or b
        assert ref is not None

        primary = is_primary(ref, wl)
        competitor = is_competitor(ref, wl)

        # Restrict alerts to watchlist; INFO-only for everything else,
        # and skip non-watchlist entirely if they didn't move much.
        if not primary and not competitor:
            continue

        old_rank = b.rank if b else None
        new_rank = t.rank if t else None
        old_score = b.score if b else None
        new_score = t.score if t else None

        if t is None:
            kind = "dropped"
        elif b is None:
            kind = "new"
        else:
            kind = "changed"

        rank_delta = (
            new_rank - old_rank
            if (old_rank is not None and new_rank is not None)
            else None
        )

        score_delta: float | None
        score_delta_pct: float | None
        if old_score is not None and new_score is not None:
            score_delta = new_score - old_score
            score_delta_pct = (
                100.0 * score_delta / old_score if old_score else None
            )
        else:
            score_delta = None
            score_delta_pct = None

        crossed = _milestones_crossed(old_rank, new_rank, milestones)

        # Filter out "no-op" rows for changed kind.
        if kind == "changed" and rank_delta in (0, None) and (
            score_delta_pct is None or abs(score_delta_pct) < 1e-6
        ) and not crossed:
            continue

        change = Change(
            source=ref.source,
            board=ref.board,
            model_id=ref.model_id,
            model_display=ref.model_display,
            organization=ref.organization,
            old_rank=old_rank,
            new_rank=new_rank,
            rank_delta=rank_delta,
            old_score=old_score,
            new_score=new_score,
            score_delta=score_delta,
            score_delta_pct=score_delta_pct,
            score_unit=ref.score_unit,
            kind=kind,
            crossed_milestones=crossed,
            is_primary=primary,
            is_competitor=competitor,
            severity="INFO",
            reasons=[],
        )
        change.severity, change.reasons = _rate_severity(change, wl)
        changes.append(change)

    # Sort: primary first, then severity desc, then magnitude of rank move desc
    def _sort_key(c: Change) -> tuple[int, int, int, str]:
        return (
            0 if c.is_primary else 1,
            -SEVERITY_ORDER[c.severity],
            -(abs(c.rank_delta) if c.rank_delta is not None else 999),
            c.model_display,
        )
    changes.sort(key=_sort_key)
    return changes


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def _fmt_rank(r: int | None) -> str:
    return f"#{r}" if r is not None else "—"


def _fmt_rank_arrow(c: Change) -> str:
    if c.old_rank is None and c.new_rank is not None:
        return f"NEW → **#{c.new_rank}**"
    if c.new_rank is None and c.old_rank is not None:
        return f"#{c.old_rank} → **DROPPED**"
    if c.rank_delta is None:
        return f"{_fmt_rank(c.old_rank)} → {_fmt_rank(c.new_rank)}"
    arrow = "▲" if c.rank_delta < 0 else ("▼" if c.rank_delta > 0 else "·")
    sign = -c.rank_delta  # positive = improved
    return f"#{c.old_rank} → **#{c.new_rank}** ({arrow} {sign:+d})"


def _fmt_score_arrow(c: Change) -> str:
    if c.old_score is None and c.new_score is None:
        return "—"
    if c.old_score is None:
        return f"NEW → {c.new_score:g}"
    if c.new_score is None:
        return f"{c.old_score:g} → —"
    if c.score_delta is None:
        return f"{c.old_score:g} → {c.new_score:g}"
    pct = f" ({c.score_delta_pct:+.2f}%)" if c.score_delta_pct is not None else ""
    return f"{c.old_score:g} → {c.new_score:g} ({c.score_delta:+g}){pct}"


def render_markdown(
    today: str,
    baseline: str | None,
    changes: list[Change],
    is_first_snapshot: bool,
) -> str:
    lines: list[str] = []
    lines.append(f"# MiMo 榜单变动 · {today}")
    lines.append("")
    if is_first_snapshot:
        lines.append("📭 **首次快照**：尚无 baseline，本次不计算变动。")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"📊 对比基准：{baseline} → {today}")
    lines.append("")

    primaries = [c for c in changes if c.is_primary]
    competitors = [c for c in changes if not c.is_primary and c.is_competitor]
    milestones = [c for c in changes if c.crossed_milestones]

    # MiMo section
    lines.append("## 🎯 MiMo 系列变动")
    lines.append("")
    if not primaries:
        lines.append("_本日 MiMo 系列无变动。_")
        lines.append("")
    else:
        by_source: dict[str, list[Change]] = {}
        for c in primaries:
            by_source.setdefault(c.source, []).append(c)
        for source in sorted(by_source):
            title = "LMArena" if source == "lmarena" else "Artificial Analysis"
            lines.append(f"### {title}")
            lines.append("")
            lines.append("| 子榜 | 模型 | 排名变化 | 分数变化 | severity | 原因 |")
            lines.append("|---|---|---|---|---|---|")
            for c in by_source[source]:
                board_short = c.board.split("/", 1)[-1]
                lines.append(
                    f"| {board_short} | {c.model_display or c.model_id} | "
                    f"{_fmt_rank_arrow(c)} | {_fmt_score_arrow(c)} | "
                    f"{c.severity} | {'; '.join(c.reasons) or '—'} |"
                )
            lines.append("")

    # Milestones
    if milestones:
        lines.append("## 🏁 关键 milestone")
        lines.append("")
        for c in milestones:
            ms = ", ".join(f"Top{m}" for m in c.crossed_milestones)
            improved = (
                c.rank_delta is not None and c.rank_delta < 0
            ) or (c.old_rank is None and c.new_rank is not None)
            icon = "✅" if improved else "⚠️"
            who = c.model_display or c.model_id
            board_short = c.board.split("/", 1)[-1]
            direction = "进入" if improved else "跌出"
            lines.append(
                f"- {icon} **{who} {direction} {c.source} {board_short} {ms}** "
                f"({_fmt_rank(c.old_rank)} → {_fmt_rank(c.new_rank)})"
            )
        lines.append("")

    # Competitors
    lines.append("## 🥊 竞品动向（mimo 关注的对手）")
    lines.append("")
    if not competitors:
        lines.append("_本日竞品无显著变动。_")
        lines.append("")
    else:
        by_source = {}
        for c in competitors:
            by_source.setdefault(c.source, []).append(c)
        for source in sorted(by_source):
            title = "LMArena" if source == "lmarena" else "Artificial Analysis"
            lines.append(f"### {title}")
            lines.append("")
            lines.append("| 子榜 | 模型 | 组织 | 排名变化 | 分数变化 | severity |")
            lines.append("|---|---|---|---|---|---|")
            for c in by_source[source]:
                board_short = c.board.split("/", 1)[-1]
                lines.append(
                    f"| {board_short} | {c.model_display or c.model_id} | "
                    f"{c.organization or '—'} | {_fmt_rank_arrow(c)} | "
                    f"{_fmt_score_arrow(c)} | {c.severity} |"
                )
            lines.append("")

    # Trends links
    lines.append("## 📈 趋势链接")
    lines.append(
        f"- [本日 LMArena 快照](data/lmarena/{today}/) | "
        f"[本日 AA 快照](data/artificial_analysis/{today}/llms.json)"
    )
    if baseline:
        lines.append(
            f"- baseline: [LMArena](data/lmarena/{baseline}/) | "
            f"[AA](data/artificial_analysis/{baseline}/llms.json)"
        )
    lines.append("")
    lines.append("---")
    lines.append("🤖 自动生成 · 数据源：lmarena.ai (HF Dataset), artificialanalysis.ai")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _emit_gha_outputs(has_changes: bool, severity: str, summary_path: Path) -> None:
    print(f"has_changes={'true' if has_changes else 'false'}")
    print(f"severity={severity}")
    print(f"summary_path={summary_path.as_posix()}")


def run(
    data_root: Path,
    watchlist_path: Path,
    today: str | None,
    baseline: str | None,
) -> int:
    wl = load_watchlist(watchlist_path)

    # Resolve dates per source independently (the two sources may not always
    # both run on the same day; we don't want one missing source to mask the
    # other).
    lm_today, lm_base = find_today_and_baseline(data_root, "lmarena", today, baseline)
    aa_today, aa_base = find_today_and_baseline(
        data_root, "artificial_analysis", today, baseline
    )

    chosen_today = lm_today or aa_today or today or date.today().isoformat()
    chosen_baseline = lm_base or aa_base

    today_entries = load_all_entries(data_root, lm_today, aa_today)
    baseline_entries = load_all_entries(data_root, lm_base, aa_base)

    is_first = chosen_baseline is None and not baseline_entries

    if is_first:
        changes: list[Change] = []
    else:
        changes = diff_entries(today_entries, baseline_entries, wl)

    # Output paths
    alerts_dir = data_root / "alerts"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    md_path = alerts_dir / f"{chosen_today}.md"
    json_path = alerts_dir / f"{chosen_today}.json"

    md = render_markdown(chosen_today, chosen_baseline, changes, is_first)
    md_path.write_text(md, encoding="utf-8")

    json_path.write_text(
        json.dumps(
            {
                "today": chosen_today,
                "baseline": chosen_baseline,
                "is_first_snapshot": is_first,
                "lmarena": {"today": lm_today, "baseline": lm_base},
                "artificial_analysis": {"today": aa_today, "baseline": aa_base},
                "changes": [c.to_dict() for c in changes],
                "today_entry_count": len(today_entries),
                "baseline_entry_count": len(baseline_entries),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Decide top-level severity for the workflow
    if not changes:
        top_sev = "NONE"
        has_changes = False
    else:
        top_sev = max(
            (c.severity for c in changes),
            key=lambda s: SEVERITY_ORDER[s],
        )
        # INFO-only days don't open an issue.
        has_changes = top_sev in ("WARN", "ALERT")

    # Prefer a path relative to the repo root (data_root.parent) for GHA.
    try:
        display_path = md_path.relative_to(data_root.parent)
    except ValueError:
        display_path = md_path
    _emit_gha_outputs(has_changes, top_sev, display_path)
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MiMo leaderboard rank-change differ")
    p.add_argument(
        "--data-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
        help="Root data directory (default: <repo>/data)",
    )
    p.add_argument(
        "--watchlist",
        type=Path,
        default=Path(__file__).resolve().parent / "watchlist.yaml",
    )
    p.add_argument("--today", help="YYYY-MM-DD (default: newest available)")
    p.add_argument("--baseline-date", dest="baseline", help="YYYY-MM-DD")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    for d in (args.today, args.baseline):
        if d is not None and not DATE_RE.match(d):
            print(f"error: bad date {d!r}, expected YYYY-MM-DD", file=sys.stderr)
            return 2
    return run(
        data_root=args.data_root.resolve(),
        watchlist_path=args.watchlist.resolve(),
        today=args.today,
        baseline=args.baseline,
    )


if __name__ == "__main__":
    raise SystemExit(main())
