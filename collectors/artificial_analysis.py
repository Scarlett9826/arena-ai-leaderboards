"""Artificial Analysis (artificialanalysis.ai) leaderboard collector.

Pulls the full models snapshot from the AA v2 API and writes a normalized
JSON snapshot to ``data/artificial_analysis/{date}/llms.json`` together
with a per-board top-10 + Xiaomi summary.

AA's API returns a flat list of models without explicit ranks, so this
collector computes ranks locally for a fixed set of boards (intelligence
index, coding index, math index, individual evals, output speed, TTFT,
blended price).

Conventions
-----------
- Higher-is-better metrics rank descending.
- ``ttft`` (time-to-first-token) and ``price_blended`` rank ascending
  (lower is better).
- Models whose value for a metric is ``None`` are excluded from that
  board's ranking and receive ``rank = None``.

CLI::

    python3 -m collectors.artificial_analysis
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx


API_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3
BACKOFF_BASE = 1.5  # seconds; doubled each retry


# ---------------------------------------------------------------------------
# Board definitions
# ---------------------------------------------------------------------------

# (short_name, source_section, source_key, descending)
# descending=True -> higher is better.
_BOARDS: list[tuple[str, str, str, bool]] = [
    ("intelligence_index", "evaluations", "artificial_analysis_intelligence_index", True),
    ("coding_index",       "evaluations", "artificial_analysis_coding_index",       True),
    ("math_index",         "evaluations", "artificial_analysis_math_index",         True),
    ("mmlu_pro",           "evaluations", "mmlu_pro",                                True),
    ("gpqa",               "evaluations", "gpqa",                                    True),
    ("hle",                "evaluations", "hle",                                     True),
    ("livecodebench",      "evaluations", "livecodebench",                           True),
    ("scicode",            "evaluations", "scicode",                                 True),
    ("math_500",           "evaluations", "math_500",                                True),
    ("aime",               "evaluations", "aime",                                    True),
    ("aime_25",            "evaluations", "aime_25",                                 True),
    ("output_speed",       "_top",        "median_output_tokens_per_second",         True),
    ("ttft",               "_top",        "median_time_to_first_token_seconds",      False),
    ("price_blended",      "pricing",     "price_1m_blended_3_to_1",                 False),
]

BOARD_NAMES = [b[0] for b in _BOARDS]


# ---------------------------------------------------------------------------
# .env loading (stdlib only)
# ---------------------------------------------------------------------------

def _load_env_file(env_path: Path) -> None:
    """Populate os.environ with KEY=VALUE pairs from a .env file.

    Pre-existing environment variables take precedence (so callers can
    override). Lines that are blank, start with ``#`` or don't contain ``=``
    are ignored. Quotes around values are stripped.
    """
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get_api_key(project_root: Path) -> str:
    if "AA_API_KEY" not in os.environ:
        _load_env_file(project_root / ".env")
    key = os.environ.get("AA_API_KEY", "").strip()
    if not key or key == "your_artificial_analysis_api_key_here":
        raise RuntimeError(
            "AA_API_KEY missing. Set it in environment or in .env "
            "(see .env.example)."
        )
    return key


# ---------------------------------------------------------------------------
# HTTP fetch with retries
# ---------------------------------------------------------------------------

def fetch_models(api_key: str) -> list[dict[str, Any]]:
    """Call AA /api/v2/data/llms/models with retries; return the data list."""
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                resp = client.get(API_URL, headers=headers)
            # Retry on transient server errors / rate limits.
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"transient {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()
            payload = resp.json()
            models = payload.get("data")
            if not isinstance(models, list):
                raise ValueError(
                    f"unexpected AA response shape: top-level 'data' missing or not a list"
                )
            return models
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            sleep_s = BACKOFF_BASE * (2 ** (attempt - 1))
            print(
                f"[aa] fetch attempt {attempt} failed: {exc!r}; "
                f"retrying in {sleep_s:.1f}s",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
    assert last_exc is not None
    raise RuntimeError(f"AA fetch failed after {MAX_RETRIES} attempts: {last_exc!r}")


# ---------------------------------------------------------------------------
# Normalization + ranking
# ---------------------------------------------------------------------------

def _extract_value(model: dict[str, Any], section: str, key: str) -> Any:
    if section == "_top":
        return model.get(key)
    sub = model.get(section) or {}
    return sub.get(key)


def _normalize_model(raw: dict[str, Any]) -> dict[str, Any]:
    creator = raw.get("model_creator") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "slug": raw.get("slug"),
        "creator_slug": creator.get("slug"),
        "creator_name": creator.get("name"),
        "release_date": raw.get("release_date"),
        "evaluations": raw.get("evaluations") or {},
        "pricing": raw.get("pricing") or {},
        "speed": {
            "output_tokens_per_second": raw.get("median_output_tokens_per_second"),
            "ttft_seconds": raw.get("median_time_to_first_token_seconds"),
            "ttfat_seconds": raw.get("median_time_to_first_answer_token"),
        },
        "ranks": {},  # filled in by _compute_ranks
    }


def _compute_ranks(
    normalized: list[dict[str, Any]],
    raw_models: list[dict[str, Any]],
) -> None:
    """Mutate ``normalized`` in place, adding ``ranks[board]`` for each board.

    Tie handling: stable "competition" ranking (1, 2, 2, 4) is overkill for
    our purposes; we use dense 1-indexed positional ranking after a stable
    sort. This means ties get sequential ranks ordered by original list
    position, which is fine because we're tracking *changes* over time and
    AA's list order is stable across calls.
    """
    n = len(normalized)
    assert len(raw_models) == n

    for short, section, key, descending in _BOARDS:
        # Collect (index, value) for models that have a real numeric value.
        scored: list[tuple[int, float]] = []
        for i, raw in enumerate(raw_models):
            v = _extract_value(raw, section, key)
            if isinstance(v, (int, float)) and v is not None:
                # Reject NaN.
                if v != v:  # pragma: no cover - defensive
                    continue
                scored.append((i, float(v)))

        scored.sort(key=lambda t: t[1], reverse=descending)

        for rank_pos, (i, _v) in enumerate(scored, start=1):
            normalized[i]["ranks"][short] = rank_pos

        # Ensure every normalized model has the key (None for un-scored).
        for n_model in normalized:
            n_model["ranks"].setdefault(short, None)


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def _build_summary(
    normalized: list[dict[str, Any]],
    fetched_at: str,
) -> dict[str, Any]:
    """Top-10 per board + Xiaomi rank table."""
    by_id = {m["id"]: m for m in normalized}

    # Top-10 per board.
    top10: dict[str, list[dict[str, Any]]] = {}
    for short, section, key, descending in _BOARDS:
        ranked = [m for m in normalized if m["ranks"].get(short) is not None]
        ranked.sort(key=lambda m: m["ranks"][short])
        top10[short] = [
            {
                "rank": m["ranks"][short],
                "id": m["id"],
                "name": m["name"],
                "creator_slug": m["creator_slug"],
                "score": _extract_normalized_score(m, section, key),
            }
            for m in ranked[:10]
        ]

    # Xiaomi rank table: rows = MiMo models, cols = boards.
    xiaomi_models = [m for m in normalized if m.get("creator_slug") == "xiaomi"]
    xiaomi_models.sort(key=lambda m: (m.get("name") or ""))
    xiaomi_rows = []
    for m in xiaomi_models:
        row = {
            "id": m["id"],
            "name": m["name"],
            "slug": m["slug"],
            "release_date": m.get("release_date"),
            "ranks": {b: m["ranks"].get(b) for b in BOARD_NAMES},
            "scores": {
                b: _extract_normalized_score(m, sec, key)
                for b, sec, key, _ in _BOARDS
            },
        }
        xiaomi_rows.append(row)

    return {
        "fetched_at": fetched_at,
        "n_models": len(normalized),
        "boards": BOARD_NAMES,
        "top10_per_board": top10,
        "xiaomi": xiaomi_rows,
    }


def _extract_normalized_score(
    m: dict[str, Any], section: str, key: str
) -> Any:
    """Pull a board's raw score out of the *normalized* model record."""
    if section == "evaluations":
        return m["evaluations"].get(key)
    if section == "pricing":
        return m["pricing"].get(key)
    if section == "_top":
        # Map the original raw key to our normalized speed bucket.
        return {
            "median_output_tokens_per_second": m["speed"].get("output_tokens_per_second"),
            "median_time_to_first_token_seconds": m["speed"].get("ttft_seconds"),
            "median_time_to_first_answer_token": m["speed"].get("ttfat_seconds"),
        }.get(key)
    return None


# ---------------------------------------------------------------------------
# Disk layout
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def collect_all(out_dir: Path, today: date) -> dict[str, Any]:
    """Fetch + normalize + rank + persist. Returns a small result summary."""
    project_root = out_dir.parent if out_dir.name == "data" else _find_project_root(out_dir)
    api_key = _get_api_key(project_root)

    raw_models = fetch_models(api_key)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    normalized = [_normalize_model(m) for m in raw_models]
    _compute_ranks(normalized, raw_models)

    snapshot = {
        "meta": {
            "source": "artificial_analysis",
            "source_url": API_URL,
            "fetched_at": fetched_at,
            "snapshot_date": today.isoformat(),
            "n_models": len(normalized),
            "boards": BOARD_NAMES,
        },
        "models": normalized,
    }

    summary = _build_summary(normalized, fetched_at)

    date_dir = out_dir / "artificial_analysis" / today.isoformat()
    llms_path = date_dir / "llms.json"
    summary_path = date_dir / "_summary.json"
    latest_path = out_dir / "artificial_analysis" / "latest.json"

    _atomic_write_json(llms_path, snapshot)
    _atomic_write_json(summary_path, summary)
    _atomic_write_json(
        latest_path,
        {
            "snapshot_date": today.isoformat(),
            "fetched_at": fetched_at,
            "llms": f"{today.isoformat()}/llms.json",
            "summary": f"{today.isoformat()}/_summary.json",
            "n_models": len(normalized),
        },
    )

    return {
        "n_models": len(normalized),
        "n_xiaomi": sum(1 for m in normalized if m["creator_slug"] == "xiaomi"),
        "llms_path": str(llms_path),
        "summary_path": str(summary_path),
        "latest_path": str(latest_path),
    }


def _find_project_root(start: Path) -> Path:
    """Walk up from ``start`` looking for a directory containing .env or .git."""
    cur = start.resolve()
    for _ in range(6):
        if (cur / ".env").exists() or (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start.resolve()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: Iterable[str]) -> int:
    here = Path(__file__).resolve().parent.parent  # project root
    out_dir = here / "data"
    today = date.today()
    result = collect_all(out_dir, today)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
