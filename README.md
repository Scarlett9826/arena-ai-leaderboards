# MiMo Leaderboard Tracker

> 自动追踪小米 MiMo 系列大模型在 LMArena、Artificial Analysis 等权威榜单的实时排名变动。

[![Daily Track](https://github.com/Scarlett9826/arena-ai-leaderboards/actions/workflows/daily-track.yml/badge.svg)](https://github.com/Scarlett9826/arena-ai-leaderboards/actions/workflows/daily-track.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

> 👋 **第一次接触？** 看 **[新手 5 分钟上手指南 → GETTING_STARTED.md](GETTING_STARTED.md)**，全程跟着抄，不用懂代码。

## 🎯 项目目标

- **每日**抓取 LMArena 全部子榜（text / vision / webdev / search / 多模态生成 …）+ Artificial Analysis 多维度指标
- **自动检测** MiMo 系列（如 `mimo-v2.5-pro`, `mimo-v2-pro`, …）的排名 / 分数变化
- **重要变动**自动开 GitHub Issue —— 仓库订阅者（Watch）即可邮件通知
- 所有数据快照按日期落盘到 `data/`，可追溯、可 diff、可二次分析

## 📊 当前追踪范围

### 数据源

- **LMArena**（HuggingFace dataset: [`lmarena-ai/leaderboard-dataset`](https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset)）
  - 子榜：`text` / `vision` / `webdev` / `search` / `document` / `text_to_image` / `image_edit` / `text_to_video` / `image_to_video` / `video_edit`（含 `style_control` 变种）
  - 每个子榜内部还分 `overall` / `coding` / `math` / `chinese` / `hard_prompts` / `creative_writing` / … 多个 category
  - 直接读 Parquet，**免鉴权**
  - 上游更新频率：3–6 天一次

- **Artificial Analysis**（官方 REST API: <https://artificialanalysis.ai>）
  - 综合指标：`intelligence_index`, `coding_index`, `math_index`
  - 子项 benchmark：`mmlu_pro`, `gpqa`, `hle`, `livecodebench`, `aime`, …
  - 性能：`output_tps`, `ttft`
  - 价格：`blended_usd_per_1m_tokens`
  - 需要 `AA_API_KEY`（免费额度 1000 req/day 足够）
  - 上游更新频率：日级

### 当前 MiMo 在榜情况

> 数据每日自动更新，最新快照见 [`data/lmarena/latest.json`](data/) 与 [`data/artificial_analysis/latest.json`](data/)。
> 历史变动报告见 [`data/alerts/`](data/)。
> 由于本仓库刚切换到新管道，前几天的快照请等待首次 cron 执行后再查看。

## 🏗️ 架构

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Actions  ·  daily cron @ 01:00 UTC (09:00 CST)        │
├──────────────────────────────────────────────────────────────┤
│  collectors/lmarena.py              → data/lmarena/{date}/*.json
│  collectors/artificial_analysis.py  → data/artificial_analysis/{date}/llms.json
│  analysis/differ.py                 → data/alerts/{date}.{md,json}
│        │
│        └─ if severity ≥ WARN
│                 │
│  analysis/issue_writer.py  →  gh issue create  (邮件提醒订阅者)
└──────────────────────────────────────────────────────────────┘
```

模块入口约定（供脚本/CI 调用）：

```python
from collectors.lmarena              import collect_all as collect_lmarena
from collectors.artificial_analysis  import collect_all as collect_aa
from analysis.differ                 import main       as run_diff
from analysis.issue_writer           import main       as write_issue
```

## 🚀 快速开始

### 本地运行

```bash
git clone https://github.com/Scarlett9826/arena-ai-leaderboards.git
cd arena-ai-leaderboards

# 推荐用虚拟环境
python3.12 -m venv .venv && source .venv/bin/activate

pip install -e .

# 配置 Artificial Analysis key（LMArena 不需要）
cp .env.example .env
# 编辑 .env，填入从 https://artificialanalysis.ai/login 申请的 key
export $(grep -v '^#' .env | xargs)

python -m collectors.lmarena
python -m collectors.artificial_analysis
python -m analysis.differ          # 输出今日 vs 上一份快照的变动
python -m analysis.issue_writer    # 生成 /tmp/issue_*.{txt,md}（可选）
```

### 部署到自己的 fork

1. **Fork** 本仓库到自己账号
2. 在 repo **Settings → Secrets and variables → Actions** 新增：
   - `AA_API_KEY` —— Artificial Analysis API key（免费 tier 即可）
3. 在 repo **Settings → Actions → General**：
   - **Workflow permissions** 选 *Read and write permissions*
   - 勾选 *Allow GitHub Actions to create and approve pull requests*（用于开 issue）
4. 在 repo **Watch → Custom → Issues** 订阅，即可邮件接收变动通知
5. workflow 会自动每日 01:00 UTC（09:00 CST）执行；也可在 *Actions* 页手动触发

### 手动补抓（backfill）

如果某天 workflow 失败、需要重跑：

- *Actions* → **Manual Backfill** → *Run workflow*
- 在 `dates` 参数填入 `2026-06-28,2026-06-29`（逗号分隔）

## 📂 数据格式

### LMArena 子榜 (`data/lmarena/{date}/{subset}.json`)

```json
{
  "subset": "text",
  "date": "2026-06-30",
  "categories": {
    "overall": [
      { "rank": 1, "model": "gpt-X", "score": 1387, "votes": 91234 },
      { "rank": 7, "model": "mimo-v2.5-pro", "score": 1302, "votes": 12044 }
    ],
    "coding":   [ ... ],
    "math":     [ ... ],
    "chinese":  [ ... ]
  }
}
```

伴生文件：

- `data/lmarena/{date}/_summary.json` —— 聚合元数据 + MiMo 系列摘要
- `data/lmarena/latest.json` —— 指针 `{"date": "YYYY-MM-DD"}`

### Artificial Analysis (`data/artificial_analysis/{date}/llms.json`)

```json
{
  "date": "2026-06-30",
  "models": [
    {
      "id": "mimo-v2.5-pro",
      "intelligence_index": 64.2,
      "coding_index": 58.1,
      "math_index": 71.0,
      "evaluations": { "mmlu_pro": 0.812, "gpqa": 0.578, "hle": 0.214 },
      "median_output_tps": 142.3,
      "median_time_to_first_token_seconds": 0.41,
      "pricing": { "price_1m_blended_3_to_1": 1.25 }
    }
  ]
}
```

### 变动报告 (`data/alerts/{date}.md` + `.json`)

```markdown
# MiMo Leaderboard Alert — 2026-06-30

**Severity:** WARN
**Baseline:** 2026-06-27

## Changes

- `mimo-v2.5-pro` on **LMArena · text · coding**: rank **9 → 6** (+3) ✅
- `mimo-v2-pro`  on **Artificial Analysis · intelligence_index**: 61.0 → 59.4 (−1.6) ⚠️
```

## 🔧 配置

调整告警阈值 / 关注的对手模型，编辑 [`analysis/watchlist.yaml`](analysis/watchlist.yaml)：

```yaml
mimo_models:        # MiMo 系列识别 pattern
  - mimo-v2.5-pro
  - mimo-v2-pro
  - "mimo-*"

competitors:        # 重点对比的对手
  - qwen-3-max
  - deepseek-v4
  - gpt-5

thresholds:
  rank_delta_warn: 3
  rank_delta_crit: 5
  score_delta_warn: 1.5
```

## 🧪 开发

```bash
pip install -e ".[dev]"
pytest                 # 跑 tests/test_differ.py 等
ruff check .
```

## 📜 License

MIT — 继承自上游项目。

## 🙏 致谢

- 上游项目：[oolong-tea-2026/arena-ai-leaderboards](https://github.com/oolong-tea-2026/arena-ai-leaderboards)
- 数据源：
  - [LMArena](https://lmarena.ai) · `lmarena-ai/leaderboard-dataset` on HuggingFace（CC-BY-4.0）
  - [Artificial Analysis](https://artificialanalysis.ai)（Free tier API）
