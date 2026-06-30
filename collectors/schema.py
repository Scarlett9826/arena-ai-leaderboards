"""Unified snapshot schema for cross-source leaderboard tracking.

This module defines the canonical `RankEntry` record and converters that
normalize per-source snapshots (LMArena, Artificial Analysis, ...) into
flat lists of entries the differ / reporter can consume uniformly.

Design notes
------------
- We use stdlib dataclasses only (no pydantic).
- `model_id` is a stable, source-namespaced key so the differ can match the
  same model across snapshots even if the display name changes slightly.
- `board` is a slash-delimited path so we can group/filter easily.
- `extra` carries per-source fields that don't fit the common columns
  (CI bounds, vote counts, token speeds, raw eval values, ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Core record
# ---------------------------------------------------------------------------

@dataclass
class RankEntry:
    source: str            # "lmarena" | "artificial_analysis"
    board: str             # e.g. "lmarena/text/overall", "aa/intelligence_index"
    snapshot_date: str     # ISO date, e.g. "2026-06-30"
    model_id: str          # stable primary key (source-namespaced)
    model_display: str     # human-readable name
    organization: str      # vendor / creator
    rank: int | None       # 1-indexed rank within board; None if not ranked
    score: float | None    # primary numeric score for this board
    score_unit: str        # "elo" | "index" | "pct" | "tokens_per_sec" | "seconds" | "usd_per_1m"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# LMArena -> RankEntry[]
# ---------------------------------------------------------------------------

def lmarena_to_entries(snapshot_json: dict[str, Any]) -> list[RankEntry]:
    """Convert a single-subset LMArena snapshot file into RankEntry list.

    Expected input shape::

        {
          "meta": {
            "source": "lmarena",
            "subset": "text",
            "leaderboard_publish_date": "2026-06-25",
            ...
          },
          "rows": [
            {"category": "overall", "rank": 1, "model_name": "...",
             "organization": "...", "license": "...",
             "rating": 1502.3, "rating_lower": ..., "rating_upper": ...,
             "variance": ..., "vote_count": ...},
            ...
          ]
        }

    Board naming: ``lmarena/{subset}/{category}``.
    Model id:     ``lmarena::{organization}::{model_name}``.
    """
    meta = snapshot_json.get("meta") or {}
    subset = meta.get("subset") or "unknown"
    # Prefer the leaderboard publish date if present, else fall back to
    # fetched_at's date prefix, else empty.
    snapshot_date = (
        meta.get("leaderboard_publish_date")
        or (meta.get("fetched_at", "") or "")[:10]
        or ""
    )

    entries: list[RankEntry] = []
    for row in snapshot_json.get("rows") or []:
        category = row.get("category") or "overall"
        org = row.get("organization") or ""
        model_name = row.get("model_name") or ""
        if not model_name:
            continue

        rank = row.get("rank")
        rating = row.get("rating")

        extra = {
            "license": row.get("license"),
            "rating_lower": row.get("rating_lower"),
            "rating_upper": row.get("rating_upper"),
            "variance": row.get("variance"),
            "vote_count": row.get("vote_count"),
        }
        # Drop None-valued extras to keep diffs clean.
        extra = {k: v for k, v in extra.items() if v is not None}

        entries.append(
            RankEntry(
                source="lmarena",
                board=f"lmarena/{subset}/{category}",
                snapshot_date=snapshot_date,
                model_id=f"lmarena::{org}::{model_name}",
                model_display=model_name,
                organization=org,
                rank=int(rank) if isinstance(rank, (int, float)) else None,
                score=float(rating) if isinstance(rating, (int, float)) else None,
                score_unit="elo",
                extra=extra,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Artificial Analysis -> RankEntry[]
# ---------------------------------------------------------------------------

# (board_name, score_unit, locator) where locator tells us where to read the
# raw score from the per-model record produced by the AA collector.
# locator forms:
#   ("eval", key)    -> models[i]["evaluations"][key]
#   ("speed", key)   -> models[i]["speed"][key]
#   ("pricing", key) -> models[i]["pricing"][key]
_AA_BOARDS: list[tuple[str, str, tuple[str, str]]] = [
    ("intelligence_index", "index", ("eval", "artificial_analysis_intelligence_index")),
    ("coding_index",       "index", ("eval", "artificial_analysis_coding_index")),
    ("math_index",         "index", ("eval", "artificial_analysis_math_index")),
    ("mmlu_pro",           "pct",   ("eval", "mmlu_pro")),
    ("gpqa",               "pct",   ("eval", "gpqa")),
    ("hle",                "pct",   ("eval", "hle")),
    ("livecodebench",      "pct",   ("eval", "livecodebench")),
    ("scicode",            "pct",   ("eval", "scicode")),
    ("math_500",           "pct",   ("eval", "math_500")),
    ("aime",               "pct",   ("eval", "aime")),
    ("aime_25",            "pct",   ("eval", "aime_25")),
    ("output_speed",       "tokens_per_sec", ("speed", "output_tokens_per_second")),
    ("ttft",               "seconds",        ("speed", "ttft_seconds")),
    ("price_blended",      "usd_per_1m",     ("pricing", "price_1m_blended_3_to_1")),
]


def _aa_lookup(model: dict[str, Any], locator: tuple[str, str]) -> Any:
    section, key = locator
    if section == "eval":
        return (model.get("evaluations") or {}).get(key)
    if section == "speed":
        return (model.get("speed") or {}).get(key)
    if section == "pricing":
        return (model.get("pricing") or {}).get(key)
    return None


def aa_to_entries(snapshot_json: dict[str, Any]) -> list[RankEntry]:
    """Convert an Artificial Analysis snapshot file into RankEntry list.

    Expects the structure produced by ``collectors.artificial_analysis``:
    each model carries ``evaluations``, ``pricing``, ``speed`` and a
    pre-computed ``ranks`` dict keyed by the short board name (e.g.
    ``intelligence_index``).
    """
    meta = snapshot_json.get("meta") or {}
    snapshot_date = (meta.get("fetched_at", "") or "")[:10]

    entries: list[RankEntry] = []
    for m in snapshot_json.get("models") or []:
        mid_raw = m.get("id") or ""
        model_id = f"aa::{mid_raw}"
        display = m.get("name") or m.get("slug") or mid_raw
        org = m.get("creator_name") or m.get("creator_slug") or ""
        ranks = m.get("ranks") or {}

        for board_short, unit, locator in _AA_BOARDS:
            score = _aa_lookup(m, locator)
            rank = ranks.get(board_short)
            # If neither rank nor score is available, skip emitting an
            # entry (no signal for this board on this model).
            if score is None and rank is None:
                continue

            extra: dict[str, Any] = {"slug": m.get("slug"), "creator_slug": m.get("creator_slug")}
            if m.get("release_date"):
                extra["release_date"] = m["release_date"]

            entries.append(
                RankEntry(
                    source="artificial_analysis",
                    board=f"aa/{board_short}",
                    snapshot_date=snapshot_date,
                    model_id=model_id,
                    model_display=display,
                    organization=org,
                    rank=int(rank) if isinstance(rank, (int, float)) else None,
                    score=float(score) if isinstance(score, (int, float)) else None,
                    score_unit=unit,
                    extra=extra,
                )
            )
    return entries


__all__ = [
    "RankEntry",
    "lmarena_to_entries",
    "aa_to_entries",
]
