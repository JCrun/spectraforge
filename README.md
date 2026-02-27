# SpectraForge

> TechPowerUp GPU 数据抓取 + 阶梯图导出工具链  
> An async toolkit for mirroring TechPowerUp GPU specs and building Excel ladders.

SpectraForge 由三个独立但互补的 CLI 组成：

1. **scrape** – 异步抓取 GPU 列表及详情，并生成结构化的 `gpu_specs.json`。
2. **retry** – 针对 `failed_details` 再次补抓，保证数据完整性。
3. **export** – 将标准化 JSON 转成分组排序的 Excel 阶梯图 (含 Top-N 可视化)。

## 项目亮点 / Highlights
- 🕸️ **稳定的反爬策略**：httpx + Tenacity 自动退避、429 触发的浏览器兜底、可选 Playwright cookie 刷新。
- ⚙️ **模块化架构**：`gpu_ladder` 包提供可复用的客户端、队列与数据模型，方便扩展新的导出或分析工具。
- 📊 **完整的数据流**：统一的 JSON schema，配套 Excel 导出、Top-N 柱状图与梯队排名。
- 🚀 **一键命令体验**：`pyproject.toml` 暴露 `spectraforge-*` 三个 CLI，`pip install -e .` 后即可全局使用。
- 🧱 **开源友好结构**：`src/`-layout、`.gitignore`、明确的依赖声明，方便直接推送到 GitHub。

## 目录结构
```text
.
├── README.md
├── pyproject.toml
├── requirements.txt
├── spectraforge.spec                # PyInstaller 构建脚本（入口已指向 src 包）
├── src/
│   └── gpu_ladder/
│       ├── __init__.py              # 包元数据 + CLI 入口重导出
│       ├── scrape_techpowerup.py    # 主抓取器
│       ├── retry_failed_details.py  # failed_details 补抓脚本
│       └── export_gpu_excel.py      # Excel 阶梯图导出
└── res/
```

## 快速开始
1. **准备环境**
   ```bash
   pyenv local 3.11        # 或使用系统 Python ≥ 3.10
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   pip install -e .        # 安装 SpectraForge 及 CLI
   playwright install chromium  # 首次使用 Playwright 必须安装浏览器
   ```
   若仅想临时运行，也可以 `pip install -r requirements.txt` 后通过 `PYTHONPATH=src` 来执行。

2. **抓取指定年度/厂商**
   ```bash
   spectraforge-scrape \
     --refresh-cookies \
     --manual-confirm \
     --start-year 2017 \
     --end-year 2026 \
     --pretty \
     --auto-refresh-on-429 \
     --log-detail-progress \
     --concurrency 1 \
     --detail-delay 3 \
     --delay 2 \
     --refresh-cooldown 60 \
     --rate-limit-sleep 30 \
     --browser-fallback-on-fail \
     --prefer-browser-for-details \
     --prefer-browser-for-listings \
     --fill-missing-details \
     --output res/gpu_specs.json
   ```
   这是偏稳健的低并发实战参数组合。若未安装 CLI，也可用等价入口：`python -m gpu_ladder.scrape_techpowerup ...`（在 `pip install -e .` 或配置 `PYTHONPATH=src` 后）。

3. **补抓失败详情**
   ```bash
   spectraforge-retry \
     --input res/gpu_specs.json \
     --max-retry 50 \
     --browser-fallback-on-fail
   ```

4. **导出 Excel 阶梯图**
   ```bash
   spectraforge-export \
     --input res/gpu_specs.json \
     --output res/gpu_ladder.xlsx \
     --top-n-chart 64
   ```
   导出的工作簿包含：分组排行榜 (Tier/Year/Manufacturer 等) 以及 Top-N 柱状图，可直接分享。

## CLI 参数说明（完整）

### `spectraforge-scrape`（`gpu_ladder.scrape_techpowerup`）
| 参数 | 默认值 | 说明 | 常见使用建议 |
| --- | --- | --- | --- |
| `--start-year` | 当前年份-1 | 起始年份（包含） | 建议与 `--end-year` 配套使用。 |
| `--end-year` | 当前年份 | 结束年份（包含） | 大跨度抓取时配合较低并发。 |
| `--manufacturers` | `NVIDIA AMD Intel Moore Threads` | 厂商过滤（空则用内置默认集合） | 仅抓主流可传 `NVIDIA AMD Intel`。 |
| `--filter-template` | `year_{year}~mfgr_{manufacturer}` | TPU 列表查询模板 | 一般无需修改，除非 TPU 参数结构变化。 |
| `--delay` | `1.0` | 相邻列表过滤请求间隔秒数 | 遇限流可提高到 `2~5`。 |
| `--detail-delay` | `1.0` | 相邻详情请求间隔秒数 | 稳定优先建议 `2~5`。 |
| `--max-gpus` | 无限制 | 最多抓取 GPU 数量 | 联调时先限量提速。 |
| `--skip-details` | 关闭 | 仅抓列表，不抓详情 | 用于快速建立 `listings`。 |
| `--concurrency` | `2` | 详情抓取并发数 | 稳定优先用 `1`，速度优先再增大。 |
| `--http-timeout` | `30.0` | HTTP 超时秒数 | 网络慢可升到 `60`。 |
| `--http2` | 关闭 | 启用 HTTP/2（需 `httpx[http2]`） | 仅在环境已安装 `h2` 时启用。 |
| `--storage-state` | `.playwright-state.json` | Playwright cookies 存储文件 | 多项目可改成独立路径。 |
| `--refresh-cookies` | 关闭 | 忽略旧 cookies 并重新获取 | 首次运行或 cookies 失效时启用。 |
| `--headless` | 关闭 | 用无头浏览器获取 cookies | 需要人工过验证时不要开。 |
| `--wait-seconds` | `120` | 等待人机验证通过的最长秒数 | 验证慢时可增大。 |
| `--manual-confirm` | 关闭 | 人工确认后按 Enter 继续 | 配合 `--refresh-cookies` 使用最稳。 |
| `--auto-refresh-on-429` | 关闭 | 遇到 429 自动刷新 cookies 并继续 | 长时间批量抓取建议开启。 |
| `--refresh-cooldown` | `20.0` | 自动刷新 cookies 的最小间隔秒数 | 防止并发下频繁刷新。 |
| `--rate-limit-sleep` | `12.0` | 429 后重试前等待秒数（优先 `Retry-After`） | 触发限流频繁时提高到 `30+`。 |
| `--browser-fallback-on-fail` | 关闭 | HTTP 重试耗尽后回退浏览器抓取 | 稳定优先建议开启。 |
| `--prefer-browser-for-details` | 关闭 | 详情阶段优先浏览器抓取 | 详情页经常 429 时开启。 |
| `--prefer-browser-for-listings` | 关闭 | 列表阶段优先浏览器抓取 | 列表为空或被拦截时开启。 |
| `--fill-missing-details` | 关闭 | 基于已有输出，仅补缺失详情并写回 | 断点续抓/补齐数据用。 |
| `--log-detail-progress` | 关闭 | 输出详情抓取开始/完成日志 | 长任务建议开启便于排障。 |
| `--output` | `gpu_specs.json` | 输出 JSON 路径 | 推荐 `res/gpu_specs.json`。 |
| `--pretty` | 关闭 | JSON 缩进美化输出 | 便于人工查看与 diff。 |

上方示例中的这组参数：
`--refresh-cookies --manual-confirm --start-year 2017 --end-year 2026 --pretty --auto-refresh-on-429 --log-detail-progress --concurrency 1 --detail-delay 3 --delay 2 --refresh-cooldown 60 --rate-limit-sleep 30 --browser-fallback-on-fail --prefer-browser-for-details --prefer-browser-for-listings --fill-missing-details`  
是一个“低并发 + 强抗限流 + 可人工介入”的稳健模板，适合真实环境长时间抓取。

### `spectraforge-retry`（`gpu_ladder.retry_failed_details`）
| 参数 | 默认值 | 说明 | 常见使用建议 |
| --- | --- | --- | --- |
| `--input` | `gpu_specs.json` | 输入 JSON 路径 | 通常传 `res/gpu_specs.json`。 |
| `--output` | 覆盖输入文件 | 输出 JSON 路径 | 想保留原文件时指定新路径。 |
| `--storage-state` | `.playwright-state.json` | Playwright cookies 文件 | 与 `scrape` 保持同一会话更稳定。 |
| `--concurrency` | `2` | 详情补抓并发数 | 稳定优先可改为 `1`。 |
| `--detail-delay` | `1.0` | 详情请求间隔秒数 | 遇限流增大。 |
| `--http-timeout` | `30.0` | HTTP 超时秒数 | 慢网可调大。 |
| `--retries` | `4` | 单个请求最大重试次数 | 网络波动时可适当提高。 |
| `--max-retry` | 无限制 | 最多补抓多少条 `failed_details` | 分批补抓时很有用。 |
| `--http2` | 关闭 | 启用 HTTP/2（需 `httpx[http2]`） | 同 `scrape`。 |
| `--refresh-cookies` | 关闭 | 强制刷新 cookies | cookies 失效时启用。 |
| `--headless` | 关闭 | 无头浏览器拿 cookies | 同 `scrape`。 |
| `--wait-seconds` | `120` | 等待验证秒数 | 同 `scrape`。 |
| `--manual-confirm` | 关闭 | 手动确认后继续 | 同 `scrape`。 |
| `--auto-refresh-on-429` | 关闭 | 遇 429 自动刷新 cookies | 长任务建议开启。 |
| `--refresh-cooldown` | `20.0` | 自动刷新最小间隔秒数 | 同 `scrape`。 |
| `--rate-limit-sleep` | `12.0` | 429 后重试前等待秒数 | 同 `scrape`。 |
| `--browser-fallback-on-fail` | 关闭 | HTTP 失败后浏览器兜底 | 建议开启。 |
| `--prefer-browser-for-details` | 关闭 | 详情优先浏览器抓取 | 失败率高时开启。 |
| `--log-detail-progress` | 关闭 | 输出补抓进度日志 | 建议开启。 |
| `--pretty` | 关闭 | 缩进格式写回 JSON | 便于人工检查。 |

### `spectraforge-export`（`gpu_ladder.export_gpu_excel`）
| 参数 | 默认值 | 说明 | 常见使用建议 |
| --- | --- | --- | --- |
| `--input` | `gpu_specs.json` | 输入 JSON 路径 | 通常传 `res/gpu_specs.json`。 |
| `--output` | `gpu_ladder.xlsx` | 输出 Excel 路径 | 推荐 `res/gpu_ladder.xlsx`。 |
| `--top-n-chart` | `50` | Top-N 天梯图显示数量 | 常用 `32/50/64/100`。 |

## 场景模板（可直接复制）

### 1) 首次全量抓取（稳健低并发）
适合第一次建库或跨多年全量抓取，优先稳定性。

```bash
spectraforge-scrape \
  --refresh-cookies \
  --manual-confirm \
  --start-year 2017 \
  --end-year 2026 \
  --manufacturers NVIDIA AMD Intel \
  --pretty \
  --auto-refresh-on-429 \
  --log-detail-progress \
  --concurrency 1 \
  --detail-delay 3 \
  --delay 2 \
  --refresh-cooldown 60 \
  --rate-limit-sleep 30 \
  --browser-fallback-on-fail \
  --prefer-browser-for-details \
  --prefer-browser-for-listings \
  --output res/gpu_specs.json
```

### 2) 日常增量补抓（补缺失 + 清理失败项）
先用 `fill-missing-details` 补缺失，再用 `retry` 处理 `failed_details`。

```bash
spectraforge-scrape \
  --start-year 2017 \
  --end-year 2026 \
  --fill-missing-details \
  --auto-refresh-on-429 \
  --browser-fallback-on-fail \
  --prefer-browser-for-details \
  --prefer-browser-for-listings \
  --concurrency 1 \
  --detail-delay 3 \
  --delay 2 \
  --refresh-cooldown 60 \
  --rate-limit-sleep 30 \
  --log-detail-progress \
  --pretty \
  --output res/gpu_specs.json
```

```bash
spectraforge-retry \
  --input res/gpu_specs.json \
  --max-retry 50 \
  --auto-refresh-on-429 \
  --browser-fallback-on-fail \
  --prefer-browser-for-details \
  --detail-delay 3 \
  --rate-limit-sleep 30 \
  --log-detail-progress \
  --pretty
```

### 3) 仅导出 Excel（不抓取）
适合已有 `gpu_specs.json`，只重建报表或调整 Top-N。

```bash
spectraforge-export \
  --input res/gpu_specs.json \
  --output res/gpu_ladder.xlsx \
  --top-n-chart 64
```

如果你未安装 CLI，可将以上命令分别替换为：
- `python -m gpu_ladder.scrape_techpowerup ...`
- `python -m gpu_ladder.retry_failed_details ...`
- `python -m gpu_ladder.export_gpu_excel ...`

## 数据格式
- `listings`: `[GPUListing]`，含 `manufacturer/year/name/detail_url` 等字段。
- `details`: `{detail_url: GPUDetail}`，确保 `normalize_detail_url` 后唯一。
- `failed_details`: `[{"url": str, "error": str}]`，供重试脚本再次尝试。
- `filters`: 记录站内 API 查询参数与返回数量，方便溯源。

## 最佳实践
- 遵守 TechPowerUp 的网站条款，合理设置 `--delay`/`--detail-delay`，必要时手动通过防火墙验证。
- 建议将运行时导出写入 `res/` 目录（`.gitignore` 已忽略 `res/*`，保留 `res/.gitkeep`），并避免提交本地生成的 Excel 文件（`*.xlsx` 等已忽略）。
- 若要制作二进制工具，可使用更新后的 `spectraforge.spec` 直接调用 `pyinstaller spectraforge.spec`。
- 欢迎基于 `gpu_ladder` 包扩展新的分析脚本（例如额外的 CSV/DB 导出）。

