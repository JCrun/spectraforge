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

## ?? & ????
- GitHub ???https://github.com/JCrun/spectraforge
- ??? CLI ??? JSON/Excel/??????? `res/` ???Git ???????????????????
- ????????? Release ??? issue comment ???????? repo ???

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
├── gpu_specs.json                   # 示例抓取结果（可替换）
├── amd_*.json / gpu_specs--2025.json# 手工 patch / 历史列表样例
├── gpu_ladder.xlsx                  # 导出样例（git 已忽略 *.xlsx）
└── tmp.py                           # 合并/修补示例脚本
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
     --start-year 2015 \
     --end-year 2024 \
     --manufacturers NVIDIA AMD Intel \
     --output data/gpu_specs.json \
     --concurrency 4 \
     --browser-fallback-on-fail
   ```
   常用参数：`--fill-missing-details` 只补齐缺失详情、`--skip-details` 仅抓列表、`--auto-refresh-on-429` 自动刷新 cookies。

3. **补抓失败详情**
   ```bash
   spectraforge-retry \
     --input data/gpu_specs.json \
     --max-retry 50 \
     --browser-fallback-on-fail
   ```

4. **导出 Excel 阶梯图**
   ```bash
   spectraforge-export \
     --input data/gpu_specs.json \
     --output output/gpu_ladder.xlsx \
     --top-n-chart 64
   ```
   导出的工作簿包含：分组排行榜 (Tier/Year/Manufacturer 等) 以及 Top-N 柱状图，可直接分享。

## CLI 参数概览
| 命令 | 关键参数 | 说明 |
| --- | --- | --- |
| `spectraforge-scrape` | `--start-year/--end-year`, `--manufacturers`, `--concurrency`, `--prefer-browser-for-*`, `--fill-missing-details` | 控制抓取范围、并发、浏览器兜底、仅补齐缺失详情等。 |
| `spectraforge-retry` | `--max-retry`, `--detail-delay`, `--auto-refresh-on-429` | 继续抓 failed_details，支持限速与自动刷新 cookies。 |
| `spectraforge-export` | `--top-n-chart`, `--output` | 自定义排行输出和 Top-N 图尺寸。 |

更多 hidden gems：
- **速率控制**：`--delay`、`--detail-delay`、`--rate-limit-sleep` 组合避免触发 TPU 防护。
- **Playwright cookies**：使用 `.playwright-state.json` 统一存储，`--refresh-cookies` 或 `--manual-confirm` 支持手动过人机验证。

## 数据格式
- `listings`: `[GPUListing]`，含 `manufacturer/year/name/detail_url` 等字段。
- `details`: `{detail_url: GPUDetail}`，确保 `normalize_detail_url` 后唯一。
- `failed_details`: `[{"url": str, "error": str}]`，供重试脚本再次尝试。
- `filters`: 记录站内 API 查询参数与返回数量，方便溯源。

## 最佳实践
- 遵守 TechPowerUp 的网站条款，合理设置 `--delay`/`--detail-delay`，必要时手动通过防火墙验证。
- 建议将 JSON/Excel 输出写入 `data/` 或 `output/` 目录（已通过 `.gitignore` 排除大型二进制文件）。
- 若要制作二进制工具，可使用更新后的 `spectraforge.spec` 直接调用 `pyinstaller spectraforge.spec`。
- 欢迎基于 `gpu_ladder` 包扩展新的分析脚本（例如额外的 CSV/DB 导出）。

---
命名为 **SpectraForge**，寓意“在图形光谱中锻造数据梯队”，既贴合 GPU/光谱主题，也体现精工打造的数据流水线。若准备发布到 GitHub，可直接使用 `spectraforge` 作为仓库名；待你确认后，我可以协助补充徽章、CI 等内容。
