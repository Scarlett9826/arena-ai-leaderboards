#!/usr/bin/env python3
"""历史趋势分析：扫描 data/ 下所有日期目录，输出某个模型的排名变化趋势。"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from query._common import (  # noqa: E402
    Record,
    available_dates,
    find_one_model,
    fmt_delta,
    fmt_rank,
    fmt_score,
    iter_dated_snapshots,
    load_full_snapshot,
    markdown_error,
    normalize_model_token,
    print_md,
    resolve_data_root,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="query/trend.py",
        description="某个模型在历史快照中的排名 / 分数趋势。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python query/trend.py mimo-v2.5-pro\n"
            "  python query/trend.py mimo-v2.5-pro --days 30\n"
            "  python query/trend.py mimo-v2.5-pro --subset lmarena/text/overall\n"
            "  python query/trend.py mimo-v2.5-pro --since 2026-06-01\n"
        ),
    )
    p.add_argument("model", help="模型名（支持子串匹配）")
    p.add_argument("--days", type=int, default=7, help="最近 N 天（默认 7）")
    p.add_argument("--since", help="起始日期 YYYY-MM-DD（覆盖 --days）")
    p.add_argument(
        "--subset",
        help="只看某个子榜，格式 `lmarena/<subset>/<category>` 或 `aa/<board_key>`",
    )
    p.add_argument("--data-root", help="覆盖默认 data/ 路径")
    return p


def _parse_iso(d: str) -> date | None:
    try:
        return date.fromisoformat(d)
    except ValueError:
        return None


def _select_dates(all_dates: list[str], days: int, since: str | None) -> list[str]:
    if since:
        try:
            cutoff = date.fromisoformat(since)
        except ValueError:
            return []
        return [d for d in all_dates if (_parse_iso(d) or date.min) >= cutoff]
    if not all_dates:
        return []
    latest = _parse_iso(all_dates[-1])
    if latest is None:
        return all_dates[-days:]
    cutoff = latest - timedelta(days=days - 1)
    return [d for d in all_dates if (_parse_iso(d) or date.min) >= cutoff]


def _match_board(rec: Record, filt: str) -> bool:
    b = filt.lower().strip()
    if b.startswith("lmarena/"):
        return rec.source == "LMArena" and rec.board.lower() == b[len("lmarena/"):]
    if b.startswith("aa/"):
        return rec.source == "AA" and rec.board.lower() == b[len("aa/"):]
    return rec.board.lower() == b


def render(
    model: str,
    dates: list[str],
    series: dict[str, list[tuple[str, Record | None]]],
    subset_filter: str | None,
) -> str:
    lines: list[str] = [f"# `{model}` 历史趋势", ""]
    if not dates:
        lines.append("> ⚠️ 没有可用的历史快照。")
        return "\n".join(lines)
    lines.append(
        f"时间范围：`{dates[0]}` ~ `{dates[-1]}`（共 {len(dates)} 个快照）"
    )
    if subset_filter:
        lines.append(f"过滤子榜：`{subset_filter}`")
    lines.append("")

    if not series:
        lines.append("> ⚠️ 该模型在选定时间范围内没有上榜数据。")
        return "\n".join(lines)

    if len(dates) < 2:
        lines.append(
            "> ℹ️ 当前 `data/` 下只有 **1 个**快照，无法计算趋势。"
            "通过定时拉取（例如每日运行采集脚本）累积更多快照后再来查看。"
        )
        lines.append("")

    valid_boards: list[str] = []
    short_boards: list[str] = []
    summary_changes: list[tuple[str, int | None, float | None]] = []

    # Sort boards so they print in a stable order: source then key
    def _board_sort_key(b: str) -> tuple[int, str]:
        # series keys are "LMArena|board" or "AA|board"
        src, rest = b.split("|", 1)
        return (0 if src == "LMArena" else 1, rest)

    for key in sorted(series.keys(), key=_board_sort_key):
        points = series[key]
        observed = [(d, r) for d, r in points if r is not None and r.rank is not None]
        if len(observed) < 2:
            short_boards.append(key)
            continue
        valid_boards.append(key)

        src, board = key.split("|", 1)
        lines.append(f"## {src} `{board}`")
        lines.append("")
        lines.append("| 日期 | 排名 | 分数 | 变化 |")
        lines.append("|---|---:|---:|---:|")
        prev_rank: int | None = None
        prev_score: float | None = None
        first_rank: int | None = None
        first_score: float | None = None
        last_rank: int | None = None
        last_score: float | None = None
        for d, r in points:
            if r is None or r.rank is None:
                lines.append(f"| {d} | — | — | — |")
                continue
            if first_rank is None:
                first_rank = r.rank
                first_score = r.score
            last_rank = r.rank
            last_score = r.score
            if prev_rank is None:
                change = "-"
            else:
                d_rank = r.rank - prev_rank
                if d_rank == 0:
                    change = "（无变化）"
                else:
                    change = fmt_delta(d_rank, sign="rank")
                if prev_score is not None and r.score is not None:
                    ds = r.score - prev_score
                    change += f" ({ds:+.2f})"
            lines.append(
                f"| {d} | {fmt_rank(r.rank)} | {fmt_score(r.score, r.lower_is_better)} | {change} |"
            )
            prev_rank = r.rank
            prev_score = r.score
        lines.append("")
        if first_rank is not None and last_rank is not None:
            d_rank = last_rank - first_rank
            d_score = (
                last_score - first_score
                if (last_score is not None and first_score is not None)
                else None
            )
            score_part = (
                f"分数 {('▲ +' + f'{d_score:.2f}') if d_score and d_score > 0 else ('▼ ' + f'{d_score:.2f}') if d_score and d_score < 0 else '持平'}"
                if d_score is not None
                else ""
            )
            rank_part = fmt_delta(d_rank, sign="rank") if d_rank != 0 else "持平"
            lines.append(
                f"> 期间累计：排名 {rank_part}" + (f" · {score_part}" if score_part else "")
            )
            lines.append("")
            summary_changes.append((key, d_rank, d_score))

    # ---- overall summary ----
    if summary_changes:
        up = [(k, dr) for k, dr, _ in summary_changes if dr is not None and dr < 0]
        down = [(k, dr) for k, dr, _ in summary_changes if dr is not None and dr > 0]
        flat = [k for k, dr, _ in summary_changes if dr == 0]
        lines.append("## 💡 趋势总结")
        lines.append("")
        lines.append(
            f"覆盖 {len(summary_changes)} 个有完整对比数据的子榜："
            f"上升 **{len(up)}** · 下降 **{len(down)}** · 持平 **{len(flat)}**。"
        )
        if up:
            best = min(up, key=lambda x: x[1])  # most negative ⇒ biggest improvement
            lines.append(
                f"- 最大上升：`{best[0].split('|', 1)[1]}` ({fmt_delta(best[1], 'rank')} 名)"
            )
        if down:
            worst = max(down, key=lambda x: x[1])
            lines.append(
                f"- 最大下降：`{worst[0].split('|', 1)[1]}` ({fmt_delta(worst[1], 'rank')} 名)"
            )
        lines.append("")

    if short_boards:
        lines.append(
            f"## 📭 历史不足无法对比的子榜（{len(short_boards)} 个）"
        )
        lines.append("")
        SAMPLE = 8
        for k in sorted(short_boards, key=_board_sort_key)[:SAMPLE]:
            src, board = k.split("|", 1)
            n = sum(1 for _, r in series[k] if r is not None and r.rank is not None)
            lines.append(f"- {src} `{board}`（仅 {n} 个有效快照）")
        if len(short_boards) > SAMPLE:
            lines.append(f"- … 以及另外 {len(short_boards) - SAMPLE} 个子榜")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = resolve_data_root(args.data_root)
    if not data_root.exists():
        print_md(markdown_error("数据目录不存在", f"找不到目录 `{data_root}`。"))
        return 1

    # Collect all available dates from both sources, union+sorted.
    dates_lm = available_dates(data_root, "lmarena")
    dates_aa = available_dates(data_root, "artificial_analysis")
    all_dates = sorted(set(dates_lm) | set(dates_aa))

    if not all_dates:
        print_md(markdown_error(
            "没有可用快照",
            "`data/lmarena/` 与 `data/artificial_analysis/` 下都没有日期目录。",
        ))
        return 1

    selected = _select_dates(all_dates, args.days, args.since)
    if not selected:
        print_md(markdown_error(
            "时间范围内没有数据",
            f"过滤后没有命中任何快照。可用日期：{', '.join(all_dates)}",
        ))
        return 1

    # Resolve the model name against the *latest* snapshot to give friendly errors.
    latest_records, _ = load_full_snapshot(data_root)
    model, matches = find_one_model(latest_records, args.model)
    if not matches:
        print_md(markdown_error(
            "未找到模型",
            f"在最新快照中找不到匹配 `{args.model}` 的模型。",
        ))
        return 1
    if model is None:
        body = "匹配到多个模型，请用更精确的名字：\n\n" + "\n".join(
            f"- `{m}`" for m in matches[:20]
        )
        print_md(markdown_error("模型名歧义", body))
        return 1

    canon = normalize_model_token(model)
    # Build per-board time series.
    # key: "<source>|<board>", value: list[(date, Record|None)]
    series: dict[str, list[tuple[str, Record | None]]] = {}
    for d, recs in iter_dated_snapshots(data_root, selected):
        rows = [r for r in recs if normalize_model_token(r.model_name) == canon]
        if args.subset:
            rows = [r for r in rows if _match_board(r, args.subset)]
        keys_today: dict[str, Record] = {f"{r.source}|{r.board}": r for r in rows}
        # Initialize new keys with prior dates as None
        for k in keys_today:
            if k not in series:
                series[k] = [(prev_d, None) for prev_d, _ in (series.get(next(iter(series), ""), []) or [])]
                # Simpler: pad based on already-seen dates
                series[k] = [(pd, None) for pd in _seen_dates(series, exclude=k)]
        # Append today's value to every key (None if absent)
        all_keys = set(series.keys()) | set(keys_today.keys())
        for k in all_keys:
            if k not in series:
                series[k] = [(pd, None) for pd in _seen_dates(series, exclude=k)]
            series[k].append((d, keys_today.get(k)))

    if args.subset and not series:
        print_md(markdown_error(
            "子榜无数据",
            f"`{model}` 在子榜 `{args.subset}` 的选定时间范围内没有数据。",
        ))
        return 1

    print_md(render(model, selected, series, args.subset))
    return 0


def _seen_dates(series: dict, exclude: str) -> list[str]:
    for k, v in series.items():
        if k == exclude:
            continue
        return [d for d, _ in v]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
