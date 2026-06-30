#!/usr/bin/env python3
"""对比两个模型（一个 MiMo 型号 + 一个竞品）在各榜单上的排名。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from query._common import (  # noqa: E402
    Record,
    find_one_model,
    fmt_rank,
    fmt_score,
    load_full_snapshot,
    markdown_error,
    normalize_model_token,
    print_md,
    resolve_data_root,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="query/compare.py",
        description="对比 MiMo 与竞品在 LMArena / AA 各榜单的排名差异。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python query/compare.py mimo-v2.5-pro deepseek-v3.5\n"
            "  python query/compare.py mimo-v2.5-pro qwen --board lmarena/text/overall\n"
        ),
    )
    p.add_argument("model_a", help="模型 A（通常是 MiMo 型号，支持子串）")
    p.add_argument("model_b", help="模型 B（竞品，支持子串）")
    p.add_argument(
        "--board",
        help="只看某个子榜，格式 `lmarena/<subset>/<category>` 或 `aa/<board_key>`",
    )
    p.add_argument("--data-root", help="覆盖默认 data/ 路径")
    return p


def _resolve_model(records: list[Record], query: str) -> tuple[str | None, list[str]]:
    model, matches = find_one_model(records, query)
    return model, matches


def _filter_by_board(records: list[Record], board: str | None) -> list[Record]:
    if not board:
        return records
    b = board.lower().strip()
    # accept "lmarena/text/overall" or "aa/intelligence_index" or just "text/overall"
    if b.startswith("lmarena/"):
        target = b[len("lmarena/"):]
        return [r for r in records if r.source == "LMArena" and r.board.lower() == target]
    if b.startswith("aa/"):
        target = b[len("aa/"):]
        return [r for r in records if r.source == "AA" and r.board.lower() == target]
    return [r for r in records if r.board.lower() == b]


def render_ambiguous(query: str, matches: list[str]) -> str:
    body = (
        f"查询 `{query}` 匹配到多个模型，请使用更精确的名字：\n\n"
        + "\n".join(f"- `{m}`" for m in matches[:20])
    )
    if len(matches) > 20:
        body += f"\n\n（共 {len(matches)} 个匹配，仅显示前 20 个）"
    return markdown_error("模型名歧义", body)


def render(
    a_name: str,
    b_name: str,
    a_rows: list[Record],
    b_rows: list[Record],
    meta: dict,
) -> str:
    a_map = {r.board: r for r in a_rows}
    b_map = {r.board: r for r in b_rows}

    common = sorted(
        set(a_map) & set(b_map),
        key=lambda k: (a_map[k].rank is None, a_map[k].rank or 10**9, k),
    )
    a_only = sorted(set(a_map) - set(b_map))
    b_only = sorted(set(b_map) - set(a_map))

    lines: list[str] = [f"# `{a_name}` vs `{b_name}` 对比", ""]
    lm_date = meta.get("lmarena_date") or "?"
    aa_date = meta.get("aa_date") or "?"
    lines.append(f"📅 数据快照：LMArena `{lm_date}` · AA `{aa_date}`")
    lines.append("")

    # --- common boards ---
    lines.append(f"## 共同上榜的子榜（{len(common)} 个，按 A 排名升序）")
    lines.append("")
    if common:
        lines.append(f"| 子榜 | `{a_name}` | `{b_name}` | A 是否领先？ |")
        lines.append("|---|---:|---:|:---:|")
        ahead = behind = tie = 0
        deltas: list[int] = []
        for board in common:
            ra, rb = a_map[board], b_map[board]
            cell_a = f"{fmt_rank(ra.rank, ra.denom)} ({fmt_score(ra.score, ra.lower_is_better)})"
            cell_b = f"{fmt_rank(rb.rank, rb.denom)} ({fmt_score(rb.score, rb.lower_is_better)})"
            if ra.rank is None or rb.rank is None:
                verdict = "—"
            else:
                diff = ra.rank - rb.rank  # negative ⇒ A ahead
                deltas.append(diff)
                if diff < 0:
                    verdict = f"✅ +{-diff}"
                    ahead += 1
                elif diff > 0:
                    verdict = f"❌ -{diff}"
                    behind += 1
                else:
                    verdict = "➖ 0"
                    tie += 1
            src_label = ra.source
            board_label = f"{src_label} `{ra.board}`"
            lines.append(f"| {board_label} | {cell_a} | {cell_b} | {verdict} |")
        lines.append("")
        if deltas:
            avg_diff = sum(deltas) / len(deltas)
            lines.append(
                f"> 共 {len(deltas)} 个可比子榜：A 领先 **{ahead}** · 落后 **{behind}** · "
                f"持平 **{tie}**，平均名次差 **{avg_diff:+.1f}**（负数=A 更靠前）。"
            )
            lines.append("")
    else:
        lines.append("> 两个模型没有共同上榜的子榜。")
        lines.append("")

    # --- A only ---
    lines.append(f"## 仅 `{a_name}` 上榜（{len(a_only)} 个）")
    lines.append("")
    if a_only:
        lines.append("| 子榜 | 排名 | 分数 |")
        lines.append("|---|---:|---:|")
        # Sort by rank
        for board in sorted(a_only, key=lambda k: (a_map[k].rank is None, a_map[k].rank or 10**9, k)):
            r = a_map[board]
            lines.append(
                f"| {r.source} `{r.board}` | {fmt_rank(r.rank, r.denom)} | "
                f"{fmt_score(r.score, r.lower_is_better)} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    # --- B only ---
    lines.append(f"## 仅 `{b_name}` 上榜（{len(b_only)} 个）")
    lines.append("")
    if b_only:
        lines.append("| 子榜 | 排名 | 分数 |")
        lines.append("|---|---:|---:|")
        for board in sorted(b_only, key=lambda k: (b_map[k].rank is None, b_map[k].rank or 10**9, k)):
            r = b_map[board]
            lines.append(
                f"| {r.source} `{r.board}` | {fmt_rank(r.rank, r.denom)} | "
                f"{fmt_score(r.score, r.lower_is_better)} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    # --- summary ---
    lines.append("## 💡 总结")
    lines.append("")
    if common:
        valid = [
            (a_map[k].rank, b_map[k].rank)
            for k in common
            if a_map[k].rank is not None and b_map[k].rank is not None
        ]
        if valid:
            ahead = sum(1 for ra, rb in valid if ra < rb)
            behind = sum(1 for ra, rb in valid if ra > rb)
            tie = sum(1 for ra, rb in valid if ra == rb)
            avg = sum(ra - rb for ra, rb in valid) / len(valid)
            best_lead = min(((ra - rb, k) for (ra, rb), k in zip(valid, [k for k in common if a_map[k].rank is not None and b_map[k].rank is not None])), key=lambda x: x[0])
            worst_gap = max(((ra - rb, k) for (ra, rb), k in zip(valid, [k for k in common if a_map[k].rank is not None and b_map[k].rank is not None])), key=lambda x: x[0])
            lines.append(
                f"在 {len(valid)} 个共同子榜上，`{a_name}` 领先 **{ahead}** 个、落后 **{behind}** 个、"
                f"持平 **{tie}** 个，平均名次差 **{avg:+.1f}**（负=A 更靠前）。"
            )
            if best_lead[0] < 0:
                lines.append(f"- A 领先最多：`{best_lead[1]}` (+{-best_lead[0]} 名)")
            if worst_gap[0] > 0:
                lines.append(f"- A 落后最多：`{worst_gap[1]}` (-{worst_gap[0]} 名)")
        else:
            lines.append("两个模型的共同子榜上至少一方排名缺失，无法直接比较。")
    else:
        lines.append("没有共同上榜的子榜可供比较。")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = resolve_data_root(args.data_root)
    if not data_root.exists():
        print_md(markdown_error(
            "数据目录不存在",
            f"找不到目录 `{data_root}`。",
        ))
        return 1

    records, meta = load_full_snapshot(data_root)
    if not records:
        print_md(markdown_error("数据为空", "当前 data/ 下没有可用快照。"))
        return 1

    a_name, a_matches = _resolve_model(records, args.model_a)
    if not a_matches:
        print_md(markdown_error("未找到模型 A", f"在快照中找不到匹配 `{args.model_a}` 的模型。"))
        return 1
    if a_name is None:
        print_md(render_ambiguous(args.model_a, a_matches))
        return 1

    b_name, b_matches = _resolve_model(records, args.model_b)
    if not b_matches:
        print_md(markdown_error("未找到模型 B", f"在快照中找不到匹配 `{args.model_b}` 的模型。"))
        return 1
    if b_name is None:
        print_md(render_ambiguous(args.model_b, b_matches))
        return 1

    a_norm = normalize_model_token(a_name)
    b_norm = normalize_model_token(b_name)
    a_group = {r.model_name for r in records if normalize_model_token(r.model_name) == a_norm}
    b_group = {r.model_name for r in records if normalize_model_token(r.model_name) == b_norm}
    a_rows = [r for r in records if r.model_name in a_group]
    b_rows = [r for r in records if r.model_name in b_group]

    if args.board:
        a_rows = _filter_by_board(a_rows, args.board)
        b_rows = _filter_by_board(b_rows, args.board)

    print_md(render(a_name, b_name, a_rows, b_rows, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
