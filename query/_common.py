"""Shared helpers for the `query/*.py` scripts.

All public helpers return plain dicts / dataclasses so individual scripts can
shape them into markdown freely. Anything user-facing must be Chinese
markdown (the AI agent uses it verbatim).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# --------------------------------------------------------------------------- #
# Paths / project root                                                        #
# --------------------------------------------------------------------------- #

# Repo layout: <root>/query/_common.py  →  <root> is parents[1]
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = DEFAULT_PROJECT_ROOT / "data"


def resolve_data_root(override: str | Path | None) -> Path:
    """Return the data root, falling back to <project>/data."""
    if override:
        p = Path(override).expanduser().resolve()
    else:
        p = DEFAULT_DATA_ROOT
    return p


def log(msg: str) -> None:
    """Diagnostic logging to stderr (stdout is reserved for markdown)."""
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# LMArena subset registry                                                     #
# --------------------------------------------------------------------------- #

LMARENA_SUBSETS: tuple[str, ...] = (
    "text",
    "text_style_control",
    "vision",
    "vision_style_control",
    "webdev",
    "search",
    "search_style_control",
    "document",
    "document_style_control",
    "text_to_image",
    "image_edit",
    "text_to_video",
    "image_to_video",
    "video_edit",
)


# --------------------------------------------------------------------------- #
# AA board registry                                                           #
# --------------------------------------------------------------------------- #
#
# A board key in `model.ranks` may or may not match a key in `model.evaluations`.
# The three composite indices use the `artificial_analysis_*` prefix in eval.
# `output_speed` / `ttft` / `price_blended` live under `speed` / `pricing`.

AA_RANK_KEYS: tuple[str, ...] = (
    "intelligence_index",
    "coding_index",
    "math_index",
    "mmlu_pro",
    "gpqa",
    "hle",
    "livecodebench",
    "scicode",
    "math_500",
    "aime",
    "aime_25",
    "output_speed",
    "ttft",
    "price_blended",
)

# board key -> ("section", "evaluation key" or attribute path).
# Used to resolve the *score* that corresponds to the rank.
_AA_EVAL_KEY: dict[str, tuple[str, str]] = {
    "intelligence_index": ("evaluations", "artificial_analysis_intelligence_index"),
    "coding_index": ("evaluations", "artificial_analysis_coding_index"),
    "math_index": ("evaluations", "artificial_analysis_math_index"),
    "mmlu_pro": ("evaluations", "mmlu_pro"),
    "gpqa": ("evaluations", "gpqa"),
    "hle": ("evaluations", "hle"),
    "livecodebench": ("evaluations", "livecodebench"),
    "scicode": ("evaluations", "scicode"),
    "math_500": ("evaluations", "math_500"),
    "aime": ("evaluations", "aime"),
    "aime_25": ("evaluations", "aime_25"),
    "output_speed": ("speed", "output_tokens_per_second"),
    "ttft": ("speed", "ttft_seconds"),
    "price_blended": ("pricing", "price_1m_blended_3_to_1"),
}


def aa_board_score(model: dict[str, Any], board: str) -> float | None:
    section, key = _AA_EVAL_KEY.get(board, ("evaluations", board))
    sec = model.get(section) or {}
    val = sec.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Normalized record                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Record:
    """One (model, board) data point, normalized across LMArena & AA."""

    source: str              # "LMArena" | "AA"
    subset: str              # e.g. "text" or "intelligence_index"; for AA the board itself
    category: str            # e.g. "overall"; for AA, "" (empty)
    board: str               # human-readable label like "text/overall" or "intelligence_index"
    model_name: str          # canonical name as it appears in the source
    rank: int | None
    score: float | None
    denom: int | None        # total models on this board (sample size)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def lower_is_better(self) -> bool:
        # AA price/latency boards: smaller value is better. Rank semantics are
        # already "smaller rank = better", but we expose the flag for callers
        # that print the score.
        return self.source == "AA" and self.subset in {"ttft", "price_blended"}


# --------------------------------------------------------------------------- #
# Loading snapshots                                                           #
# --------------------------------------------------------------------------- #


def read_latest_date(data_root: Path, source_dir: str) -> str | None:
    p = data_root / source_dir / "latest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("date")
    except (OSError, json.JSONDecodeError):
        return None


def available_dates(data_root: Path, source_dir: str) -> list[str]:
    """All YYYY-MM-DD subdirectories under data/<source_dir>/."""
    root = data_root / source_dir
    if not root.exists():
        return []
    out: list[str] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if len(name) == 10 and name[4] == "-" and name[7] == "-":
            out.append(name)
    out.sort()
    return out


def load_lmarena_snapshot(
    data_root: Path,
    date: str,
    subsets: Iterable[str] | None = None,
) -> tuple[list[Record], dict[str, Any]]:
    """Load every LMArena subset for `date` into Record list.

    Returns (records, meta) where meta has `publish_date` (latest across files)
    and `loaded_subsets`.
    """
    snap_dir = data_root / "lmarena" / date
    records: list[Record] = []
    publish_dates: list[str] = []
    loaded: list[str] = []
    targets = list(subsets) if subsets is not None else list(LMARENA_SUBSETS)
    for subset in targets:
        f = snap_dir / f"{subset}.json"
        if not f.exists():
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[warn] failed to read {f}: {exc}")
            continue
        loaded.append(subset)
        meta = doc.get("meta") or {}
        pd = meta.get("leaderboard_publish_date")
        if pd:
            publish_dates.append(pd)
        rows = doc.get("rows") or []
        # Compute denominator (total models per category) for this subset.
        per_cat: dict[str, int] = {}
        for r in rows:
            cat = r.get("category") or ""
            per_cat[cat] = per_cat.get(cat, 0) + 1
        for r in rows:
            cat = r.get("category") or ""
            try:
                rank_val = r.get("rank")
                rank = int(rank_val) if rank_val is not None else None
            except (TypeError, ValueError):
                rank = None
            try:
                rating = float(r["rating"]) if r.get("rating") is not None else None
            except (TypeError, ValueError):
                rating = None
            try:
                votes = int(r["vote_count"]) if r.get("vote_count") is not None else None
            except (TypeError, ValueError):
                votes = None
            records.append(
                Record(
                    source="LMArena",
                    subset=subset,
                    category=cat,
                    board=f"{subset}/{cat}" if cat else subset,
                    model_name=r.get("model_name", ""),
                    rank=rank,
                    score=rating,
                    denom=per_cat.get(cat),
                    extra={
                        "organization": r.get("organization"),
                        "license": r.get("license"),
                        "vote_count": votes,
                    },
                )
            )
    snap_meta = {
        "publish_date": max(publish_dates) if publish_dates else None,
        "loaded_subsets": loaded,
    }
    return records, snap_meta


def load_aa_snapshot(
    data_root: Path,
    date: str,
) -> tuple[list[Record], dict[str, Any]]:
    f = data_root / "artificial_analysis" / date / "llms.json"
    records: list[Record] = []
    if not f.exists():
        return records, {"snapshot_date": None, "n_models": 0}
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[warn] failed to read {f}: {exc}")
        return records, {"snapshot_date": None, "n_models": 0}
    meta = doc.get("meta") or {}
    models = doc.get("models") or []
    # Precompute denominator for each board.
    denom: dict[str, int] = {}
    for board in AA_RANK_KEYS:
        denom[board] = sum(
            1 for m in models if (m.get("ranks") or {}).get(board) is not None
        )
    for m in models:
        ranks = m.get("ranks") or {}
        for board in AA_RANK_KEYS:
            r_val = ranks.get(board)
            if r_val is None:
                continue
            try:
                rank = int(r_val)
            except (TypeError, ValueError):
                continue
            records.append(
                Record(
                    source="AA",
                    subset=board,
                    category="",
                    board=board,
                    model_name=m.get("name", ""),
                    rank=rank,
                    score=aa_board_score(m, board),
                    denom=denom[board],
                    extra={
                        "slug": m.get("slug"),
                        "creator": m.get("creator_name"),
                        "release_date": m.get("release_date"),
                    },
                )
            )
    snap_meta = {
        "snapshot_date": meta.get("snapshot_date") or date,
        "n_models": len(models),
        "fetched_at": meta.get("fetched_at"),
    }
    return records, snap_meta


def load_full_snapshot(
    data_root: Path,
    lmarena_date: str | None = None,
    aa_date: str | None = None,
) -> tuple[list[Record], dict[str, Any]]:
    """Convenience wrapper: load LMArena + AA using `latest.json` by default."""
    lm_date = lmarena_date or read_latest_date(data_root, "lmarena")
    aa_date = aa_date or read_latest_date(data_root, "artificial_analysis")
    records: list[Record] = []
    meta: dict[str, Any] = {"lmarena_date": lm_date, "aa_date": aa_date}
    if lm_date:
        lm_recs, lm_meta = load_lmarena_snapshot(data_root, lm_date)
        records.extend(lm_recs)
        meta["lmarena"] = lm_meta
    if aa_date:
        aa_recs, aa_meta = load_aa_snapshot(data_root, aa_date)
        records.extend(aa_recs)
        meta["aa"] = aa_meta
    return records, meta


# --------------------------------------------------------------------------- #
# Fuzzy model-name matching                                                   #
# --------------------------------------------------------------------------- #


def normalize_model_token(s: str) -> str:
    """Lowercase + collapse separators/whitespace/parens-punctuation.

    Keeps parenthesized qualifiers because LMArena uses them to distinguish
    variants such as `mimo-v2-flash (thinking)` vs `… (non-thinking)`. We only
    drop separators that are pure noise across sources: `.`, `_`, `-`, `/`, ` `.
    """
    s = (s or "").lower().strip()
    for sep in (" ", "_", ".", "/", "-"):
        s = s.replace(sep, "")
    return s


def _candidate_names(records: Iterable[Record]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in records:
        if r.model_name and r.model_name not in seen:
            seen.add(r.model_name)
            out.append(r.model_name)
    return out


def find_models(
    records: Iterable[Record],
    query: str,
) -> list[str]:
    """Return canonical `model_name`s matching `query` (case-insensitive).

    Strategy:
      1) exact match (case-insensitive)
      2) normalized-equal (strip parens/separators, lowercase)
      3) substring match on either raw lowercase or normalized form
    Returns the union, preserving order: exact > normalized > substring.
    """
    q_raw = (query or "").strip().lower()
    q_norm = normalize_model_token(query)
    names = _candidate_names(records)

    exact: list[str] = []
    norm_eq: list[str] = []
    substr: list[str] = []
    seen: set[str] = set()

    def add(bucket: list[str], n: str) -> None:
        if n not in seen:
            seen.add(n)
            bucket.append(n)

    for n in names:
        n_low = n.lower()
        n_norm = normalize_model_token(n)
        if n_low == q_raw or n_norm == q_norm:
            add(exact, n)
        elif q_norm and q_norm == n_norm:
            add(norm_eq, n)
        elif q_raw and q_raw in n_low:
            add(substr, n)
        elif q_norm and q_norm in n_norm:
            add(substr, n)

    return exact + norm_eq + substr


def find_one_model(records: Iterable[Record], query: str) -> tuple[str | None, list[str]]:
    """Return (best_match, all_matches). best_match=None on ambiguity, [] on miss.

    Different sources spell the same model differently (e.g. LMArena
    `mimo-v2.5-pro` vs AA `MiMo-V2.5-Pro`). When all matches share the same
    *normalized* form, we treat them as one logical model: best_match is the
    LMArena-preferred spelling (or the first), and all_matches is the full
    group so callers can filter `model_name in matches`.
    """
    matches = find_models(records, query)
    if not matches:
        return None, []
    if len(matches) == 1:
        return matches[0], matches
    q_norm = normalize_model_token(query)
    # Strong hit: every match has the same normalized form (cross-source spelling).
    norms = {normalize_model_token(m) for m in matches}
    if len(norms) == 1:
        # Prefer a name that equals the user's normalized query, else first.
        for m in matches:
            if normalize_model_token(m) == q_norm:
                return m, matches
        return matches[0], matches
    # Otherwise: did exactly one match equal the user's normalized query?
    exact_norm = [m for m in matches if normalize_model_token(m) == q_norm]
    if len(exact_norm) == 1:
        return exact_norm[0], matches
    if len(exact_norm) > 1 and len({normalize_model_token(m) for m in exact_norm}) == 1:
        # All exact-normalized are cross-source spellings of the same model.
        return exact_norm[0], exact_norm
    return None, matches


def mimo_model_names(records: Iterable[Record]) -> list[str]:
    """All canonical model names that look like MiMo across both sources."""
    return find_models(records, "mimo")


# --------------------------------------------------------------------------- #
# Markdown helpers                                                            #
# --------------------------------------------------------------------------- #


def fmt_rank(r: int | None, denom: int | None = None) -> str:
    if r is None:
        return "—"
    if denom:
        return f"#{r}/{denom}"
    return f"#{r}"


def fmt_score(s: float | None, lower_is_better: bool = False) -> str:
    if s is None:
        return "—"
    # AA price_blended in $/M tokens; ttft in seconds; rest are 0-1 or 0-100
    if abs(s) < 1:
        return f"{s:.3f}"
    if abs(s) < 10:
        return f"{s:.2f}"
    return f"{s:.1f}"


def fmt_delta(delta: int | float | None, sign: str = "rank") -> str:
    """`sign='rank'` ⇒ smaller is better (▲ when delta<0)."""
    if delta is None:
        return "—"
    if isinstance(delta, float) and delta == int(delta):
        delta = int(delta)
    if delta == 0:
        return "（无变化）"
    if sign == "rank":
        if delta < 0:  # rank decreased ⇒ improved
            return f"▲ {-delta}"
        return f"▼ {delta}"
    # 'score': larger is better
    if isinstance(delta, int):
        return f"▲ +{delta}" if delta > 0 else f"▼ {delta}"
    return f"▲ +{delta:+.2f}" if delta > 0 else f"▼ {delta:.2f}"


def markdown_error(title: str, body: str) -> str:
    return f"# ❌ {title}\n\n{body}\n"


def print_md(md: str) -> None:
    """Write markdown to stdout, ensuring a trailing newline."""
    if not md.endswith("\n"):
        md += "\n"
    sys.stdout.write(md)


# --------------------------------------------------------------------------- #
# Trend helpers                                                               #
# --------------------------------------------------------------------------- #


def iter_dated_snapshots(
    data_root: Path,
    dates: list[str],
) -> Iterator[tuple[str, list[Record]]]:
    """Yield (date, records) tuples; records combine LMArena + AA for that date."""
    for d in dates:
        recs: list[Record] = []
        if (data_root / "lmarena" / d).exists():
            lm, _ = load_lmarena_snapshot(data_root, d)
            recs.extend(lm)
        if (data_root / "artificial_analysis" / d).exists():
            aa, _ = load_aa_snapshot(data_root, d)
            recs.extend(aa)
        yield d, recs
