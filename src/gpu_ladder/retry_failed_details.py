import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import httpx

from .scrape_techpowerup import (
    DEFAULT_HEADERS,
    FirewallNotCleared,
    TechPowerUpGPUClient,
    bootstrap_storage_state,
    fetch_details_concurrently,
    fetch_html_via_browser,
    load_cookies,
    normalize_detail_url,
    to_human_listing_url,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="仅补抓 failed_details 并写回 JSON",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", default="gpu_specs.json", help="输入 JSON 文件")
    parser.add_argument("--output", default=None, help="输出 JSON 文件，默认覆盖输入文件")
    parser.add_argument("--storage-state", default=".playwright-state.json", help="Playwright cookies 文件")
    parser.add_argument("--concurrency", type=int, default=2, help="详情页并发请求数")
    parser.add_argument("--detail-delay", type=float, default=1.0, help="详情页请求之间等待秒数")
    parser.add_argument("--http-timeout", type=float, default=30.0, help="HTTP 超时时间")
    parser.add_argument("--retries", type=int, default=4, help="单个请求最大重试次数")
    parser.add_argument("--max-retry", type=int, default=None, help="最多补抓多少条 failed_details")
    parser.add_argument("--http2", action="store_true", help="启用 HTTP/2（需要 pip install 'httpx[http2]'）")
    parser.add_argument("--refresh-cookies", action="store_true", help="忽略已有 cookies 并重新获取")
    parser.add_argument("--headless", action="store_true", help="以 headless 模式打开浏览器获取 cookies")
    parser.add_argument("--wait-seconds", type=int, default=120, help="等待人机验证通过的最长秒数")
    parser.add_argument("--manual-confirm", action="store_true", help="验证完成后回终端按 Enter 继续")
    parser.add_argument("--auto-refresh-on-429", action="store_true", help="发生 429 时自动刷新 cookies")
    parser.add_argument("--refresh-cooldown", type=float, default=20.0, help="自动刷新 cookies 的最小间隔秒数")
    parser.add_argument("--rate-limit-sleep", type=float, default=12.0, help="429 后重试前的等待秒数")
    parser.add_argument("--browser-fallback-on-fail", action="store_true", help="HTTP 重试耗尽时使用浏览器抓取")
    parser.add_argument("--prefer-browser-for-details", action="store_true", help="详情页优先使用浏览器抓取")
    parser.add_argument("--log-detail-progress", action="store_true", help="输出详情页抓取日志")
    parser.add_argument("--pretty", action="store_true", help="以缩进格式写回 JSON")
    return parser


async def run(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    details = payload.get("details")
    if not isinstance(details, dict):
        details = {}

    failed_entries = payload.get("failed_details") or []
    if not isinstance(failed_entries, list):
        raise RuntimeError("failed_details 格式错误，预期为数组")

    retry_urls: List[str] = []
    seen = set()
    for item in failed_entries:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        retry_urls.append(url)

    if args.max_retry is not None and args.max_retry >= 0:
        retry_urls = retry_urls[: args.max_retry]

    if not retry_urls:
        print("[done] failed_details 为空或无可用 URL，无需补抓。")
        return 0

    state_path = Path(args.storage_state)
    if args.refresh_cookies or not state_path.exists():
        await bootstrap_storage_state(
            state_path,
            headless=args.headless,
            wait_seconds=args.wait_seconds,
            manual_confirm=args.manual_confirm,
        )

    cookies = load_cookies(state_path)
    if not cookies:
        raise RuntimeError("未能从 storage state 中解析到 techpowerup cookies")

    timeout = httpx.Timeout(args.http_timeout)
    client_kwargs = dict(
        headers=DEFAULT_HEADERS,
        cookies=cookies,
        timeout=timeout,
        http2=args.http2,
    )

    refresh_cb: Optional[Callable[[str], Awaitable[Dict[str, str]]]] = None
    browser_fetch_cb: Optional[Callable[[str], Awaitable[str]]] = None

    if args.auto_refresh_on_429:

        async def refresh_cookies(context_url: str) -> Dict[str, str]:
            target_url = to_human_listing_url(context_url)
            print(f"[info] 检测到 429，尝试刷新 cookies: {target_url}")
            await bootstrap_storage_state(
                state_path,
                headless=args.headless,
                wait_seconds=args.wait_seconds,
                manual_confirm=args.manual_confirm,
                target_url=target_url,
            )
            return load_cookies(state_path)

        refresh_cb = refresh_cookies

    if args.browser_fallback_on_fail:

        async def browser_fetch(url: str) -> str:
            return await fetch_html_via_browser(
                url,
                state_path,
                headless=args.headless,
                wait_seconds=args.wait_seconds,
                manual_confirm=args.manual_confirm,
            )

        browser_fetch_cb = browser_fetch

    failed_again: List[Dict[str, str]] = []
    fetched = {}
    async with httpx.AsyncClient(**client_kwargs) as client:
        scraper = TechPowerUpGPUClient(
            client,
            filter_template="year_{year}~mfgr_{manufacturer}",
            retries=args.retries,
            refresh_cookies=refresh_cb,
            refresh_cooldown_sec=args.refresh_cooldown,
            rate_limit_sleep_sec=args.rate_limit_sleep,
            browser_fallback_fetch=browser_fetch_cb,
            prefer_browser_for_details=args.prefer_browser_for_details,
        )
        fetched = await fetch_details_concurrently(
            scraper,
            retry_urls,
            args.concurrency,
            args.detail_delay,
            log_progress=args.log_detail_progress,
            failures=failed_again,
        )

    normalized_keys = {normalize_detail_url(key): key for key in details.keys()}
    for url, detail in fetched.items():
        norm = normalize_detail_url(url)
        key = normalized_keys.get(norm, url)
        details[key] = detail.__dict__
        normalized_keys[norm] = key

    attempted = set(retry_urls)
    untouched_failures = [
        item
        for item in failed_entries
        if isinstance(item, dict) and str(item.get("url") or "").strip() not in attempted
    ]
    remaining_failures = untouched_failures + failed_again

    payload["details"] = details
    if remaining_failures:
        payload["failed_details"] = remaining_failures
    else:
        payload.pop("failed_details", None)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()

    indent = 2 if args.pretty else None
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")

    print(
        "[done] 补抓完成: 成功 {ok} 条，失败 {fail} 条，剩余 failed_details {remain} 条，已写回 {path}".format(
            ok=len(fetched),
            fail=len(failed_again),
            remain=len(remaining_failures),
            path=output_path,
        )
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except FirewallNotCleared as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[warn] 用户中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
