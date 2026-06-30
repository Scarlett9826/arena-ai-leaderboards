#!/usr/bin/env python3
"""查询某个 MiMo 型号在当前榜单的排名。

输出中文 markdown，便于 AI Agent 直接转交用户。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

# allow `python query/rank.py` when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from query._common import (  # noqa: E402
    Record,
    fmt_rank,
    fmt_score,
    load_full_snapshot,
    markdown_error,
    mimo_model_names,
    find_models,
    find_one_model,
    print_md,
    resolve_data_root,
    log,
)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="query/rank.py",
        description="查询某个 MiMo 型号在 LMArena / AA 各榜单的排名。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python query/rank.py mimo-v2.5-pro\n"
            "  python query/rank.py mimo-v2.5-pro --board lmarena\n"
            "  python query/rank.py mimo-v2.5-pro --subset text/overall\n"
            "  python query/rank.py --list\n"
        ),
    )
    p.add_argument(
        "model",
        nargs="?",
        help="MiMo 模型名（支持模糊匹配，如 mimo-v2.5-pro 或 v2.5-pro）",
    )
    p.add_argument(
        "--board",
        choices=("all", "lmarena", "aa"),
        default="all",
        help="只看某来源（默认 all）",
    )
    p.add_argument(
        "--subset",
        help="具体子榜，如 `text/overall`（LMArena）或 `intelligence_index`（AA）",
    )
    p.add_argument(
        "--list",
        dest="list_models",
        action="store_true",
        help="列出当前快照中所有 MiMo 型号并退出",
    )
    p.add_argument("--data-root", help="覆盖默认 data/ 路径")
    p.add_argument("--top", type=int, default=5, help="顶部高光榜单数量（默认 5）")
    return p


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #


def _filter_records(
    records: Iterable[Record],
    model_names: set[str],
    board_filter: str,
    subset_filter: str | None,
) -> list[Record]:
    out: list[Record] = []
    for r in records:
        if r.model_name not in model_names:
            continue
        if board_filter == "lmarena" and r.source != "LMArena":
            continue
        if board_filter == "aa" and r.source != "AA":
            continue
        if subset_filter:
            sf = subset_filter.lower()
            # match either "subset/category" full board, or just subset, or AA board key
            if sf not in r.board.lower() and sf != r.subset.lower():
                continue
        out.append(r)
    return out


def _snapshot_header(meta: dict) -> str:
    lm_date = meta.get("lmarena_date") or "?"
    aa_date = meta.get("aa_date") or "?"
    lm_meta = meta.get("lmarena") or {}
    lm_publish = lm_meta.get("publish_date")
    parts = [f"📅 数据快照：LMArena `{lm_date}`"]
    if lm_publish:
        parts[-1] += f"（榜单发布 {lm_publish}）"
    parts.append(f"AA `{aa_date}`")
    return " · ".join(parts)


def render_list(records: list[Record]) -> str:
    """列出所有 MiMo 模型。"""
    mimo_lm = sorted({r.model_name for r in records if r.source == "LMArena" and "mimo" in r.model_name.lower()})
    mimo_aa = sorted({r.model_name for r in records if r.source == "AA" and "mimo" in r.model_name.lower()})
    lines = ["# MiMo 模型清单（当前快照）", ""]
    lines.append(f"## LMArena ({len(mimo_lm)} 个)")
    if mimo_lm:
        for n in mimo_lm:
            lines.append(f"- `{n}`")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append(f"## Artificial Analysis ({len(mimo_aa)} 个)")
    if mimo_aa:
        for n in mimo_aa:
            lines.append(f"- `{n}`")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("> 复制任意一个名字（或子串）作为查询参数即可，例如 `python query/rank.py mimo-v2.5-pro`。")
    return "\n".join(lines)


def render_ambiguous(query: str, matches: list[str]) -> str:
    body = (
        f"查询 `{query}` 匹配到多个模型，请使用更精确的名字：\n\n"
        + "\n".join(f"- `{m}`" for m in matches)
    )
    return markdown_error("模型名歧义", body)


def render_not_found(query: str, candidates: list[str]) -> str:
    body = f"在当前快照里找不到匹配 `{query}` 的模型。\n\n"
    if candidates:
        body += "**当前可用 MiMo 模型：**\n\n" + "\n".join(f"- `{c}`" for c in candidates)
    else:
        body += "当前快照内没有任何 MiMo 数据（请检查 `data/` 目录）。"
    return markdown_error("未找到模型", body)


def render_main(
    model: str,
    rows: list[Record],
    meta: dict,
    top_n: int,
) -> str:
    lines: list[str] = [f"# `{model}` 当前排名", ""]
    lines.append(_snapshot_header(meta))
    # Show aliases if rows reference multiple source-specific names.
    aliases = sorted({r.model_name for r in rows if r.model_name != model})
    if aliases:
        lines.append("")
        lines.append("> 跨源别名：" + " / ".join(f"`{a}`" for a in aliases))
    lines.append("")

    if not rows:
        lines.append("> ⚠️ 该模型在当前快照中无任何上榜数据。")
        return "\n".join(lines)

    # Sort: rank ascending, then board.
    ranked = [r for r in rows if r.rank is not None]
    ranked.sort(key=lambda r: (r.rank, r.source, r.board))

    # ----- top highlights -----
    top = ranked[:top_n]
    if top:
        lines.append(f"## 🏆 表现最好的 {len(top)} 个榜单")
        lines.append("")
        lines.append("| 排名 | 来源 | 子榜 | 分数 | 样本量 |")
        lines.append("|---:|---|---|---:|---:|")
        for r in top:
            denom = f"{r.denom}" if r.denom else "—"
            lines.append(
                f"| {fmt_rank(r.rank)} | {r.source} | `{r.board}` | "
                f"{fmt_score(r.score, r.lower_is_better)} | {denom} |"
            )
        lines.append("")

    # ----- full table grouped by source -----
    lines.append(f"## 📊 全部排名（{len(rows)} 个榜单）")
    lines.append("")

    by_source: dict[str, list[Record]] = defaultdict(list)
    for r in rows:
        by_source[r.source].append(r)
    # Stable source ordering: LMArena first, then AA, then anything else.
    source_order = ["LMArena", "AA"] + sorted(
        s for s in by_source if s not in ("LMArena", "AA")
    )
    for src in source_order:
        bucket = by_source.get(src)
        if not bucket:
            continue
        bucket.sort(key=lambda r: (r.rank is None, r.rank or 10**9, r.board))
        lines.append(f"### {src}（{len(bucket)} 个）")
        lines.append("")
        lines.append("| 排名 | 子榜 | 分数 | 样本量 | 投票/备注 |")
        lines.append("|---:|---|---:|---:|---|")
        for r in bucket:
            denom = f"{r.denom}" if r.denom else "—"
            extra = ""
            if r.source == "LMArena":
                v = (r.extra or {}).get("vote_count")
                extra = f"{v:,} 票" if isinstance(v, int) else "—"
            else:
                extra = "（AA 实时）"
            lines.append(
                f"| {fmt_rank(r.rank)} | `{r.board}` | "
                f"{fmt_score(r.score, r.lower_is_better)} | {denom} | {extra} |"
            )
        lines.append("")

    # ----- one-liner summary -----
    summary_bits = _build_summary(model, rows)
    if summary_bits:
        lines.append("## 💡 一句话总结")
        lines.append("")
        lines.append(summary_bits)
        lines.append("")

    return "\n".join(lines)


def _build_summary(model: str, rows: list[Record]) -> str:
    ranked = [r for r in rows if r.rank is not None]
    if not ranked:
        return ""
    lm_ranks = [r.rank for r in ranked if r.source == "LMArena"]
    aa_rows = [r for r in ranked if r.source == "AA"]
    parts: list[str] = []
    if lm_ranks:
        avg = sum(lm_ranks) / len(lm_ranks)
        best_lm = min((r for r in ranked if r.source == "LMArena"), key=lambda r: r.rank)
        parts.append(
            f"在 LMArena 共 {len(lm_ranks)} 个子榜中平均排名 #{avg:.1f}，"
            f"最强项 `{best_lm.board}` (#{best_lm.rank})"
        )
    if aa_rows:
        # surface intelligence/coding/math indices if present
        idx_priority = ("intelligence_index", "coding_index", "math_index")
        idx_rows = {r.subset: r for r in aa_rows if r.subset in idx_priority}
        pieces: list[str] = []
        for k in idx_priority:
            if k in idx_rows:
                rr = idx_rows[k]
                pieces.append(f"AA `{k}` #{rr.rank}/{rr.denom or '?'}")
        if pieces:
            parts.append("，".join(pieces))
        else:
            best_aa = min(aa_rows, key=lambda r: r.rank)
            parts.append(f"AA 最佳子榜 `{best_aa.board}` #{best_aa.rank}")
    return f"`{model}` " + "；".join(parts) + "。"


# --------------------------------------------------------------------------- #
# Entrypoint                                                                  #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.model and not args.list_models:
        print_md(markdown_error(
            "缺少参数",
            "请提供模型名，或使用 `--list` 列出所有 MiMo 模型。\n\n"
            "示例：`python query/rank.py mimo-v2.5-pro`",
        ))
        return 2

    data_root = resolve_data_root(args.data_root)
    if not data_root.exists():
        print_md(markdown_error(
            "数据目录不存在",
            f"找不到目录 `{data_root}`。请确认仓库结构或用 `--data-root` 指定。",
        ))
        return 1

    records, meta = load_full_snapshot(data_root)
    if not records:
        print_md(markdown_error(
            "数据为空",
            f"`{data_root}` 下没有可用的快照（LMArena/AA 均为空）。",
        ))
        return 1

    if args.list_models:
        print_md(render_list(records))
        return 0

    # Restrict candidate set by board filter so fuzzy match doesn't surface
    # an AA-only or LMArena-only name when the user asked for the other.
    pool = records
    if args.board == "lmarena":
        pool = [r for r in records if r.source == "LMArena"]
    elif args.board == "aa":
        pool = [r for r in records if r.source == "AA"]

    model, matches = find_one_model(pool, args.model)
    if not matches:
        candidates = mimo_model_names(records)
        print_md(render_not_found(args.model, candidates))
        return 1
    if model is None:
        # Ambiguous: but if user typed exactly a MiMo "family" like "mimo",
        # they probably want to see the list, not pick one.
        if len(matches) > 1:
            # If they look like distinct models, ask. Else collapse if unique
            # after exact lowercase match.
            print_md(render_ambiguous(args.model, matches))
            return 1
        model = matches[0]

    # Build the cross-source group: any record whose model_name shares the
    # same normalized form as the resolved canonical name.
    from query._common import normalize_model_token  # local import to keep top tidy
    canon = normalize_model_token(model)
    group = {r.model_name for r in records if normalize_model_token(r.model_name) == canon}
    if not group:
        group = {model}

    rows = _filter_records(records, group, args.board, args.subset)
    if not rows:
        print_md(markdown_error(
            "未找到匹配的子榜",
            f"`{model}` 在过滤条件 board=`{args.board}`"
            + (f" subset=`{args.subset}`" if args.subset else "")
            + " 下没有数据。",
        ))
        return 1

    print_md(render_main(model, rows, meta, top_n=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
