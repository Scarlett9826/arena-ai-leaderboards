"""Render a daily alert into GitHub-Issue ready artifacts.

Consumes ``data/alerts/{today}.json`` (structured changes) plus the matching
``.md`` (the human-readable body produced by ``analysis.differ``) and writes:

    /tmp/issue_title.txt   single line, the issue title
    /tmp/issue_body.md     full markdown body (≈ the .md report)
    /tmp/issue_labels.txt  comma-separated labels

Stdout prints the three output paths (one per line) so the calling workflow
can pipe them into ``gh issue create``.

CLI::

    python -m analysis.issue_writer                       # uses newest alerts/*.json
    python -m analysis.issue_writer --date 2026-06-30
    python -m analysis.issue_writer --alerts-dir data/alerts --out-dir /tmp
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "ALERT": 2}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _pick_latest(alerts_dir: Path) -> str | None:
    if not alerts_dir.is_dir():
        return None
    dates = sorted(
        p.stem for p in alerts_dir.glob("*.json")
        if DATE_RE.match(p.stem)
    )
    return dates[-1] if dates else None


def _top_severity(changes: list[dict]) -> str:
    if not changes:
        return "NONE"
    return max((c.get("severity", "INFO") for c in changes), key=lambda s: SEVERITY_ORDER.get(s, 0))


def _make_title(date_str: str, severity: str, n_changes: int) -> str:
    if severity == "ALERT":
        return f"🚨 MiMo 榜单变动 · {date_str} · {n_changes} 项重要变化"
    if severity == "WARN":
        return f"⚠️  MiMo 榜单变动 · {date_str} · {n_changes} 项变化"
    # Shouldn't normally be called for INFO/NONE, but be defensive.
    return f"ℹ️ MiMo 榜单 · {date_str} · 无重要变化"


def _make_labels(severity: str) -> list[str]:
    if severity == "ALERT":
        return ["mimo-alert", "severity:alert", "auto"]
    if severity == "WARN":
        return ["mimo-alert", "severity:warn", "auto"]
    return ["mimo-alert", "severity:info", "auto"]


def run(date_str: str, alerts_dir: Path, out_dir: Path) -> int:
    json_path = alerts_dir / f"{date_str}.json"
    md_path = alerts_dir / f"{date_str}.md"

    if not json_path.is_file():
        print(f"error: {json_path} not found", file=sys.stderr)
        return 1
    if not md_path.is_file():
        print(f"error: {md_path} not found", file=sys.stderr)
        return 1

    data = json.loads(json_path.read_text(encoding="utf-8"))
    changes = data.get("changes", []) or []

    severity = _top_severity(changes)
    # n_changes counted is "noteworthy" changes (WARN/ALERT). For an
    # INFO-only day the workflow shouldn't have invoked us at all, but
    # keep a sane fallback.
    noteworthy = [c for c in changes if c.get("severity") in ("WARN", "ALERT")]
    n_changes = len(noteworthy) or len(changes)

    title = _make_title(date_str, severity, n_changes)
    labels = _make_labels(severity)

    # Body: prepend a tiny TL;DR header summarising counts, then full md.
    primary = sum(1 for c in changes if c.get("is_primary"))
    competitor = sum(1 for c in changes if c.get("is_competitor") and not c.get("is_primary"))
    by_sev: dict[str, int] = {}
    for c in changes:
        s = c.get("severity", "INFO")
        by_sev[s] = by_sev.get(s, 0) + 1
    sev_summary = " · ".join(f"{k}: {v}" for k, v in sorted(by_sev.items(), key=lambda kv: -SEVERITY_ORDER.get(kv[0], 0)))

    tl_dr = (
        f"> **TL;DR** · severity = `{severity}` · {n_changes} 项重要变化 "
        f"(primary {primary} / competitor {competitor}) · {sev_summary}\n\n"
    )
    body = tl_dr + md_path.read_text(encoding="utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    title_path = out_dir / "issue_title.txt"
    body_path = out_dir / "issue_body.md"
    labels_path = out_dir / "issue_labels.txt"

    title_path.write_text(title + "\n", encoding="utf-8")
    body_path.write_text(body, encoding="utf-8")
    labels_path.write_text(",".join(labels), encoding="utf-8")

    # stdout: paths, one per line — workflow uses these.
    print(title_path)
    print(body_path)
    print(labels_path)
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render daily alert into GitHub Issue artifacts")
    p.add_argument(
        "--alerts-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "alerts",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/tmp"),
    )
    p.add_argument("--date", help="YYYY-MM-DD (default: newest alert file)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    date_str = args.date or _pick_latest(args.alerts_dir.resolve())
    if not date_str:
        print(f"error: no alert files in {args.alerts_dir}", file=sys.stderr)
        return 2
    if not DATE_RE.match(date_str):
        print(f"error: bad date {date_str!r}", file=sys.stderr)
        return 2
    return run(date_str, args.alerts_dir.resolve(), args.out_dir.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
