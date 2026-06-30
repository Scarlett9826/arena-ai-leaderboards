# 🚀 新手上手指南

> 完全没用过 GitHub Actions / Python 项目？看这一份就够。
> 全程跟着抄就行，**不需要懂代码**。

---

## 这个项目能帮你做什么？

每天自动去 **LMArena**（大模型竞技场）和 **Artificial Analysis**（AI 模型分析平台）查 **MiMo 系列模型的最新排名**，一旦发现明显升降，就：

- 📝 在仓库里建一个 **GitHub Issue**（写清楚谁动了、动了几名）
- 📧 **自动给你发邮件**（前提：你 watch 了仓库）
- 📂 把每天的快照存进 `data/` 文件夹，方便回看历史

**整个流程跑在 GitHub 服务器上，零成本，你不用挂自己的电脑。**

---

## 你需要准备什么？

| 东西 | 怎么搞 | 必需？ |
|---|---|---|
| GitHub 账号 | https://github.com 注册 | ✅ |
| Artificial Analysis API Key | 下面教 | ✅ |
| Git（本地命令行工具） | macOS 自带；Windows 装 [Git for Windows](https://git-scm.com/) | 只有想本地跑才需要 |
| Python 3.10+ | macOS 一般自带；或装 [python.org](https://python.org) | 只有想本地跑才需要 |

> 💡 **最快路径**：完全不装东西，直接在 GitHub 网页上操作，5 分钟可以让它跑起来。下面 [路径 A](#路径-a最省事零本地安装) 就是这个。

---

## 路径 A：最省事（零本地安装）

### Step 1：Fork 这个仓库

1. 打开本仓库主页（你正在看的这个）
2. 右上角点 **Fork** 按钮
3. 选你自己的 GitHub 账号，点 **Create fork**

完成后你会有一个 `https://github.com/<你的用户名>/arena-ai-leaderboards` 的副本。

---

### Step 2：申请 Artificial Analysis 免费 API Key

**这一步是为什么**：项目要去 AA 平台查数据，它要求每个请求带一个"门禁卡"（API Key），免费档每天 1000 次请求，对我们绰绰有余（一天就用 1 次）。

1. 打开 https://artificialanalysis.ai/login
2. 用邮箱/Google 注册，登录
3. 登录后进入 **API 控制台**（菜单里找 "API" 或者直接访问 https://artificialanalysis.ai/dashboard/api）
4. 点 **Generate New Key**
5. **复制那串以 `aa_` 开头的字符串**（只显示一次，关掉就找不回来，要重新生成）

```
示例：aa_CxYWWAclhjrmIaxVLLTukPDcRsAMCtmd  ← 长这样
```

---

### Step 3：把 API Key 存进 GitHub 仓库的"保险柜"

**这一步是为什么**：API Key 是密码，**绝对不能写进代码里**，否则别人 fork 你的仓库就能盗用。GitHub 提供了 Secrets 功能，专门存这种密码。

1. 在你 fork 的仓库页面，点顶部菜单的 **Settings**
2. 左边栏找到 **Secrets and variables** → **Actions**
3. 点 **New repository secret**（绿色按钮）
4. 填写：
   - **Name**：`AA_API_KEY`（必须一模一样，大小写敏感）
   - **Secret**：粘贴你刚才复制的那串 `aa_...`
5. 点 **Add secret**

✅ 完成。你现在看不到这个 key 的内容了，但 GitHub Actions 跑的时候能用。

---

### Step 4：打开 Actions 权限（重要！）

**默认 GitHub 不允许 Actions 改你的仓库**，要手动开。

1. 还在 **Settings** 页
2. 左边栏 **Actions** → **General**
3. 滚动到最下面 **Workflow permissions** 区
4. 选中 **Read and write permissions**（让 Actions 能 commit 数据、开 issue）
5. 勾上 **Allow GitHub Actions to create and approve pull requests**
6. 点 **Save**

---

### Step 5：手动跑一次，验证一切 OK

不用等到明天 9 点定时任务，可以现在就触发一次：

1. 仓库页面点顶部菜单 **Actions**
2. 第一次进会让你确认 "I understand my workflows, go ahead and enable them" → 点
3. 左边栏选 **Daily MiMo Leaderboard Track**
4. 右边点 **Run workflow** 下拉框 → 直接点绿色的 **Run workflow** 按钮（参数留空就行）
5. 等 1-2 分钟，刷新页面看运行状态：
   - 🟡 黄色 = 进行中
   - ✅ 绿色 = 成功
   - ❌ 红色 = 失败（点进去看日志，常见错误见下面 [FAQ](#常见错误faq)）

跑成功后：
- 仓库根目录会多出 `data/lmarena/<今天日期>/` 和 `data/artificial_analysis/<今天日期>/` 两个文件夹
- 这是你的第一份"基线快照"，明天再跑就有对比了

---

### Step 6：订阅邮件通知

**这一步是为什么**：MiMo 排名出现重要变动时，项目会自动开 GitHub Issue。你 watch 仓库后，Issue 创建会自动发邮件给你。

1. 仓库主页右上角找 **Watch** 按钮（带眼睛图标）
2. 点 **Watch** → **Custom** → 勾选 **Issues**（也可以全选 **All Activity**）
3. 确保你 GitHub 账号的邮箱接收通知：
   - 打开 https://github.com/settings/notifications
   - 确认 **Email** 那栏勾上了 "Issues"

✅ 大功告成。明天起每天早上 9 点（北京时间）自动跑，有重要变动会发邮件给你。

---

## 路径 B：本地也能跑（开发/调试用）

如果你想在自己电脑上跑（比如改改 watchlist、调试一下），跟着做：

### 1. 把代码下载到本地

打开终端（macOS：`Cmd+空格` 搜 "Terminal"；Windows：开始菜单搜 "Git Bash"）

```bash
# 进入你想放代码的文件夹，比如桌面
cd ~/Desktop

# 把仓库下载下来（换成你 fork 后的地址）
git clone https://github.com/<你的用户名>/arena-ai-leaderboards.git

# 进入项目目录
cd arena-ai-leaderboards
```

### 2. 装 Python 依赖

```bash
# 安装项目（一次就好）
pip install -e .
```

如果报错 `command not found: pip`，试试 `pip3 install -e .`。

### 3. 配置 API Key

```bash
# 复制示例文件
cp .env.example .env

# 用任何文本编辑器（VS Code、记事本、nano、vim）打开 .env，
# 把里面的 your_artificial_analysis_api_key_here 换成你的真实 key
```

⚠️ **`.env` 文件已经在 `.gitignore` 里，永远不会被提交到 GitHub**，放心填。

### 4. 跑起来

```bash
# 抓 LMArena 数据
python -m collectors.lmarena

# 抓 AA 数据
python -m collectors.artificial_analysis

# 算差异（第一次没基线，不会报告变动）
python -m analysis.differ
```

每个命令完成后，看 `data/` 目录就有当天数据了。

---

## 用起来之后怎么看结果？

### 看每天的数据
```
data/
├── lmarena/
│   ├── latest.json                  ← 最新一天的指针
│   └── 2026-06-30/                  ← 当天的快照
│       ├── text.json                  LMArena 文本对话榜（最重要）
│       ├── vision.json                视觉理解榜
│       ├── webdev.json                网页开发榜
│       ├── text_to_image.json         文生图榜
│       ├── ...（共 14 个子榜）
│       └── _summary.json              所有子榜的汇总
└── artificial_analysis/
    ├── latest.json
    └── 2026-06-30/
        ├── llms.json                  544 个模型 × 14 个评测维度
        └── _summary.json              每个维度的 top10 + MiMo 全景表
```

### 看变动告警

- **GitHub Issues 标签 `mimo-alert`** → 这是历史告警列表
- **`data/alerts/<日期>.md`** → 每天的人类可读报告
- **邮箱** → 重要变动会自动发到你 GitHub 注册邮箱

### 看一个具体模型当前在哪些榜单第几名

打开 `data/artificial_analysis/<最新日期>/_summary.json`，里面有专门的 "Xiaomi models" 段落，列出 MiMo 系列所有模型在所有维度的排名。

LMArena 在 `data/lmarena/<最新日期>/_summary.json` 同理。

---

## 常见错误 FAQ

### ❌ Action 跑失败，日志写 "AA_API_KEY is required"
- **原因**：Secret 没配，或者名字写错了
- **解决**：回 Step 3，确认 Secret 名字一字不差是 `AA_API_KEY`（大写）

### ❌ Action 跑失败，日志写 "401 Invalid API key"
- **原因**：API Key 复制错了，或者被你不小心删了
- **解决**：去 AA 平台重新生成一个，更新 GitHub Secret

### ❌ Action 跑失败，日志写 "Permission denied" 或 "remote: Write access denied"
- **原因**：Step 4 的 workflow 权限没开
- **解决**：回 Step 4，确保选了 "Read and write permissions"

### ❌ Action 第一次跑成功，但没收到邮件
- **正常**：第一次没基线，没东西可比较。明天跑第二次（或自己再手动 Run 一次造对比），有变动才会开 issue 发邮件

### ❌ 本地跑 `python -m collectors.lmarena` 报 `ModuleNotFoundError`
- **原因**：`pip install -e .` 没执行成功
- **解决**：确认在项目根目录，重跑 `pip install -e .`，看有没有报错

### ❌ macOS 本地跑 SSL 证书错误
- **原因**：Python 3.14 在某些 macOS 上 SSL 证书路径有问题
- **解决**：项目本来就是用 `httpx`（而非 `urllib`）实现的，理论上不会遇到。如果遇到，跑一次：
  ```bash
  pip install --upgrade certifi
  ```

### ❌ 我想改告警阈值，比如排名变化 2 名就告警
打开 `analysis/watchlist.yaml`，改 `rank_change_warn` / `rank_change_alert`，commit 推上去就生效。

### ❌ 我想加更多关注的对手模型
还是 `analysis/watchlist.yaml`，在 `competitors_models` 列表加关键词（比如 `- minimax`）。

---

## 进阶玩法

### 想补抓某一天的历史数据？
仓库里有第二个 workflow：**Manual Backfill**。Actions 页面选它，在 `dates` 参数填 `2026-06-25,2026-06-28`（逗号分隔多个日期）即可。

### 想看趋势图？
当前版本没有 web dashboard，但所有历史都在 git 里，可以：
```bash
git log --all --oneline -- data/lmarena/  # 看历史 commit
```
后续会考虑加 GitHub Pages 静态图表（路线图见 README）。

### 不想接收太多邮件？
回 GitHub watch 设置，只勾选 **Issues** 里的 **mimo-alert** label —— 或者只看 severity:alert 的（severity:warn 也别订）。

---

## 项目维护问题

### 数据源停了/改了怎么办？
- **LMArena**：项目读的是官方 HuggingFace Dataset，如果它停更新，所有人都没数据，不用慌
- **Artificial Analysis**：免费 API 1000 req/day 我们用不到 30 次。如果它收费政策变了，README 会更新 fallback 方案

### 我看到 issue 里报错怎么提？
- 在你 fork 的仓库里直接 **New Issue**，label 选 `bug`
- 贴日志（Actions 页面里点失败的 run，复制错误段）
- 如果改了 watchlist 或代码，说明改了什么

---

## 📞 还有问题？

- 看 `README.md`：项目架构和数据格式细节
- 看 `analysis/watchlist.yaml`：所有可配置项都在这里
- 看 `.github/workflows/daily-track.yml`：定时任务怎么跑的

跑通一次以后，你就完全不用操心了——每天早上 9 点自动开工，重要变动直接进你邮箱。
