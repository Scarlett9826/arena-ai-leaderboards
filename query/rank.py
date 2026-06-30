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
    expand_subset_query,
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
    """筛选符合 model + source + subset 关键词的记录。

    subset_filter 通过 expand_subset_query 展开成多个候选子串（OR 匹配）。
    例如 "数学" → ("text/math", "industry_mathematical", "math_index", "math_500", "aime", "aime_25")
    匹配 r.board 或 r.subset 中任意一个出现该子串即算命中。
    """
    candidates = tuple(c.lower() for c in expand_subset_query(subset_filter)) if subset_filter else ()
    out: list[Record] = []
    for r in records:
        if r.model_name not in model_names:
            continue
        if board_filter == "lmarena" and r.source != "LMArena":
            continue
        if board_filter == "aa" and r.source != "AA":
            continue
        if candidates:
            board_lc = r.board.lower()
            subset_lc = r.subset.lower()
            if not any(c in board_lc or c in subset_lc for c in candidates):
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

    # ----- 📌 总榜位置（最显眼，放最上面）-----
    overall_block = _build_overall_block(rows)
    if overall_block:
        lines.append("## 📌 总榜位置")
        lines.append("")
        lines.append(overall_block)
        lines.append("")

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


def _build_overall_block(rows: list[Record]) -> str:
    """🌟 总榜（LMArena text/overall + AA intelligence_index）位置高亮。

    这两个是各自平台的"对外门面"总榜：
    - LMArena text/overall：基于全部 text 类对话的 Bradley-Terry 总分
    - AA intelligence_index：14 维评测加权综合智能指数
    """
    lm_overall = next(
        (r for r in rows if r.source == "LMArena" and r.board == "text/overall"),
        None,
    )
    aa_overall = next(
        (r for r in rows if r.source == "AA" and r.subset == "intelligence_index"),
        None,
    )
    if lm_overall is None and aa_overall is None:
        return ""
    bullets: list[str] = []
    if lm_overall is not None:
        denom = lm_overall.denom or "?"
        bullets.append(
            f"- **LMArena 总榜（`text/overall`）**：{fmt_rank(lm_overall.rank)} / {denom}　"
            f"分数 {fmt_score(lm_overall.score, lm_overall.lower_is_better)}"
        )
    else:
        bullets.append("- **LMArena 总榜（`text/overall`）**：未上榜")
    if aa_overall is not None:
        denom = aa_overall.denom or "?"
        bullets.append(
            f"- **AA 总榜（`intelligence_index`）**：{fmt_rank(aa_overall.rank)} / {denom}　"
            f"分数 {fmt_score(aa_overall.score, aa_overall.lower_is_better)}"
        )
    else:
        bullets.append("- **AA 总榜（`intelligence_index`）**：未测试（该模型 AA 未给出综合智能指数）")
    return "\n".join(bullets)


def _build_summary(model: str, rows: list[Record]) -> str:
    ranked = [r for r in rows if r.rank is not None]
    if not ranked:
        return ""
    parts: list[str] = []

    # 1) 优先突出总榜
    lm_overall = next(
        (r for r in ranked if r.source == "LMArena" and r.board == "text/overall"),
        None,
    )
    aa_overall = next(
        (r for r in ranked if r.source == "AA" and r.subset == "intelligence_index"),
        None,
    )
    if lm_overall:
        parts.append(f"LMArena 总榜 #{int(lm_overall.rank)}/{lm_overall.denom or '?'}")
    if aa_overall:
        parts.append(f"AA 综合智能 #{int(aa_overall.rank)}/{aa_overall.denom or '?'}")

    # 2) 再带上分榜亮点
    lm_rows = [r for r in ranked if r.source == "LMArena"]
    aa_rows = [r for r in ranked if r.source == "AA"]
    if lm_rows:
        # 排除 overall 找最强分项
        sub_lm = [r for r in lm_rows if r.board != "text/overall"]
        if sub_lm:
            best_lm = min(sub_lm, key=lambda r: r.rank)
            avg = sum(r.rank for r in lm_rows) / len(lm_rows)
            parts.append(
                f"LMArena 各分榜平均 #{avg:.1f}，最强分项 `{best_lm.board}` (#{int(best_lm.rank)})"
            )
    if aa_rows:
        idx_priority = ("coding_index", "math_index")
        idx_rows = {r.subset: r for r in aa_rows if r.subset in idx_priority}
        for k in idx_priority:
            if k in idx_rows:
                rr = idx_rows[k]
                parts.append(f"AA `{k}` #{int(rr.rank)}/{rr.denom or '?'}")

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
        # 看看展开后的关键词是否在"其它模型"身上能查到——
        # 如果能，说明该模型在这些榜单上未测试；如果不能，说明关键词拼错了。
        if args.subset:
            expanded = expand_subset_query(args.subset)
            cands_lc = tuple(c.lower() for c in expanded)
            board_exists_globally = any(
                any(c in r.board.lower() or c in r.subset.lower() for c in cands_lc)
                for r in records
            )
            if board_exists_globally:
                body = (
                    f"`{model}` 在子榜关键词 `{args.subset}` 对应的榜单上 **未被测试 / 未上榜**。\n\n"
                    f"该关键词展开为以下 board 子串：{', '.join(f'`{e}`' for e in expanded)}\n\n"
                    f"> AA 评测可能尚未跑该模型，LMArena 可能投票数不足导致未列入。"
                    f"建议换个更宽的关键词（如 `数学` → `推理`），或不带 --subset 看全部排名。"
                )
            else:
                body = (
                    f"找不到关键词 `{args.subset}` 对应的任何榜单。\n\n"
                    f"该关键词展开为：{', '.join(f'`{e}`' for e in expanded)}（在所有模型上都查不到）\n\n"
                    f"**LMArena 常用关键词**：`总榜` `代码` `数学` `中文` `推理` `指令` `长上下文` `多轮` `创意写作` `webdev`\n\n"
                    f"**AA 常用关键词**：`总榜` `coding` `gpqa` `mmlu_pro` `hle` `aime` `速度` `价格`"
                )
            print_md(markdown_error("未找到匹配的子榜", body))
        else:
            print_md(markdown_error(
                "未找到匹配的子榜",
                f"`{model}` 在过滤条件 board=`{args.board}` 下没有数据。",
            ))
        return 1

    print_md(render_main(model, rows, meta, top_n=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
