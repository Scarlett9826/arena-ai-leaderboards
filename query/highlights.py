#!/usr/bin/env python3
"""MiMo 系列模型在当前快照上的高光 / 短板汇总。"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from query._common import (  # noqa: E402
    Record,
    find_one_model,
    fmt_rank,
    fmt_score,
    load_full_snapshot,
    markdown_error,
    mimo_model_names,
    normalize_model_token,
    print_md,
    resolve_data_root,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="query/highlights.py",
        description="MiMo 系列模型在当前快照上的高光与短板分析。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python query/highlights.py\n"
            "  python query/highlights.py --model mimo-v2.5-pro\n"
            "  python query/highlights.py --top 5\n"
        ),
    )
    p.add_argument("--model", help="只看某个 MiMo 型号（默认全部 MiMo）")
    p.add_argument("--top", type=int, default=5, help="高光 / 短板各取前 N（默认 5）")
    p.add_argument("--data-root", help="覆盖默认 data/ 路径")
    return p


def _row_label(r: Record) -> str:
    return f"{r.source} `{r.board}`"


def _percentile(rank: int | None, denom: int | None) -> float | None:
    if rank is None or not denom:
        return None
    return rank / denom


def render(
    rows: list[Record],
    top_n: int,
    meta: dict,
    title_suffix: str,
) -> str:
    lines: list[str] = []
    lm_date = meta.get("lmarena_date") or "?"
    aa_date = meta.get("aa_date") or "?"
    title = "MiMo 系列榜单亮点" if not title_suffix else f"`{title_suffix}` 榜单亮点"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"📅 数据快照：LMArena `{lm_date}` · AA `{aa_date}`")
    lines.append("")

    ranked = [r for r in rows if r.rank is not None]
    if not ranked:
        lines.append("> ⚠️ 当前快照内没有可用的排名数据。")
        return "\n".join(lines)

    # ---- best (lowest rank) ----
    best = sorted(ranked, key=lambda r: (r.rank, -(r.denom or 0)))[:top_n]
    lines.append(f"## 🏆 最高光时刻（排名最靠前的 {len(best)} 项）")
    lines.append("")
    lines.append("| 模型 | 来源 | 子榜 | 排名 | 样本量 | 分数 |")
    lines.append("|---|---|---|---:|---:|---:|")
    for r in best:
        lines.append(
            f"| `{r.model_name}` | {r.source} | `{r.board}` | "
            f"{fmt_rank(r.rank)} | {r.denom or '—'} | "
            f"{fmt_score(r.score, r.lower_is_better)} |"
        )
    lines.append("")

    # ---- worst (largest percentile rank) ----
    with_denom = [r for r in ranked if r.denom]
    worst_pool = with_denom if with_denom else ranked
    worst = sorted(
        worst_pool,
        key=lambda r: -(_percentile(r.rank, r.denom) or (r.rank / 1000.0)),
    )[:top_n]
    lines.append(f"## ⚠️ 较弱项（相对靠后的 {len(worst)} 项）")
    lines.append("")
    lines.append("| 模型 | 来源 | 子榜 | 排名 | 样本量 | 分位 |")
    lines.append("|---|---|---|---:|---:|---:|")
    for r in worst:
        pct = _percentile(r.rank, r.denom)
        pct_str = f"后 {pct * 100:.0f}%" if pct is not None else "—"
        lines.append(
            f"| `{r.model_name}` | {r.source} | `{r.board}` | "
            f"{fmt_rank(r.rank)} | {r.denom or '—'} | {pct_str} |"
        )
    lines.append("")

    # ---- per-model summary ----
    by_model: dict[str, list[Record]] = defaultdict(list)
    for r in ranked:
        by_model[r.model_name].append(r)

    lines.append("## 📊 各模型综合表现")
    lines.append("")
    lines.append("| 模型 | 上榜数 | 平均排名 | 最佳 | 最弱 |")
    lines.append("|---|---:|---:|---|---|")
    for name in sorted(by_model.keys()):
        bucket = by_model[name]
        avg = sum(r.rank for r in bucket) / len(bucket)
        best_r = min(bucket, key=lambda r: r.rank)
        # weakest by percentile if available, else by rank
        weak_r = max(
            bucket,
            key=lambda r: (_percentile(r.rank, r.denom) or (r.rank / 1000.0)),
        )
        best_cell = f"{_row_label(best_r)} #{best_r.rank}"
        weak_cell = f"{_row_label(weak_r)} #{weak_r.rank}"
        if weak_r.denom:
            weak_cell += f"/{weak_r.denom}"
        lines.append(
            f"| `{name}` | {len(bucket)} | #{avg:.1f} | {best_cell} | {weak_cell} |"
        )
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = resolve_data_root(args.data_root)
    if not data_root.exists():
        print_md(markdown_error("数据目录不存在", f"找不到目录 `{data_root}`。"))
        return 1

    records, meta = load_full_snapshot(data_root)
    if not records:
        print_md(markdown_error("数据为空", "当前 data/ 下没有可用快照。"))
        return 1

    if args.model:
        name, matches = find_one_model(records, args.model)
        if not matches:
            print_md(markdown_error(
                "未找到模型",
                f"找不到匹配 `{args.model}` 的模型。",
            ))
            return 1
        if name is None:
            body = "匹配到多个模型，请用更精确的名字：\n\n" + "\n".join(
                f"- `{m}`" for m in matches[:20]
            )
            print_md(markdown_error("模型名歧义", body))
            return 1
        canon = normalize_model_token(name)
        rows = [r for r in records if normalize_model_token(r.model_name) == canon]
        suffix = name
    else:
        mimo_names = set(mimo_model_names(records))
        if not mimo_names:
            print_md(markdown_error(
                "未找到 MiMo 数据",
                "当前快照里没有任何 MiMo 模型记录。",
            ))
            return 1
        rows = [r for r in records if r.model_name in mimo_names]
        suffix = ""

    print_md(render(rows, top_n=args.top, meta=meta, title_suffix=suffix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
