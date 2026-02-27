from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

BASE_URL = "https://www.techpowerup.com"
LIST_PATH = "/gpu-specs/"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.techpowerup.com/",
}
DEFAULT_MANUFACTURERS: Sequence[str] = (
    "NVIDIA",
    "AMD",
    "Intel",
    "Moore Threads",
)
REQUIRED_DETAIL_SECTIONS: Sequence[str] = (
    "Graphics Processor",
    "Clock Speeds",
    "Memory",
    "Board Design",
    "Render Config",
    "Theoretical Performance",
    "Graphics Features",
    "IGP Variants",
    "Mobile Graphics",
    "Graphics Card",
    "Integrated Graphics",
)
MULTI_CHIP_TITLE_HINTS: Dict[str, int] = {
    "geforce gtx 690": 2,
    "b300": 4,
}
BLOCK_PAGE_MARKERS: Sequence[str] = (
    "Automated bot check in progress",
    "Automated bot check",
    "Too Many Requests",
    "HTTP 429",
)


@dataclass
class GPUListing:
    manufacturer: str
    year: int
    name: str
    detail_url: str


@dataclass
class GPUDetail:
    url: str
    title: str
    hero: Dict[str, str]
    sections: Dict[str, Dict[str, str]]
    images: List[str]


class FirewallNotCleared(RuntimeError):
    """Raised when TechPowerUp still serves the bot-check page."""


class TechPowerUpGPUClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        filter_template: str,
        retries: int = 4,
        refresh_cookies: Optional[Callable[[str], Awaitable[Dict[str, str]]]] = None,
        refresh_cooldown_sec: float = 20.0,
        rate_limit_sleep_sec: float = 12.0,
        browser_fallback_fetch: Optional[Callable[[str], Awaitable[str]]] = None,
        prefer_browser_for_details: bool = False,
    ) -> None:
        self.client = client
        self.filter_template = filter_template
        self.retries = retries
        self.refresh_cookies = refresh_cookies
        self._refresh_lock = asyncio.Lock()
        self.refresh_cooldown_sec = refresh_cooldown_sec
        self._last_refresh_ts = 0.0
        self.rate_limit_sleep_sec = rate_limit_sleep_sec
        self.browser_fallback_fetch = browser_fallback_fetch
        self.prefer_browser_for_details = prefer_browser_for_details

    async def fetch_listing(self, manufacturer: str, year: int) -> Tuple[List[GPUListing], Optional[str]]:
        # Keep manufacturer casing as provided and include ajax flag to match site requests
        params = {
            "f": self.filter_template.format(manufacturer=manufacturer, year=year),
            "ajax": "",
        }
        html = await self._fetch_text(LIST_PATH, params=params)
        return parse_listing_document(html, manufacturer, year)

    async def fetch_detail(self, url: str) -> GPUDetail:
        if self.prefer_browser_for_details and self.browser_fallback_fetch:
            print(f"[info] 详情页优先使用浏览器抓取: {url}")
            html = await self.browser_fallback_fetch(url)
            return parse_detail_document(html, url)
        html = await self._fetch_text(url)
        return parse_detail_document(html, url)

    async def _fetch_text(self, path_or_url: str, params: Optional[Dict[str, str]] = None) -> str:
        url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}{path_or_url}"
        request_url = merge_url_query(url, params)
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.retries),
            wait=wait_exponential(multiplier=1.5, min=1, max=20),
            retry=retry_if_exception_type((httpx.HTTPError, FirewallNotCleared)),
            reraise=True,
        ):
            with attempt:
                response = await self.client.get(url, params=params)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429 and await self._refresh_cookies_with_callback(request_url):
                        retry_after = exc.response.headers.get("Retry-After")
                        wait_sec = self._resolve_retry_after_seconds(retry_after)
                        print(f"[info] 429 后等待 {wait_sec:.1f}s 再重试: {request_url}")
                        await asyncio.sleep(wait_sec)
                        continue
                    raise
                text = response.text
                if "Automated bot check" in text:
                    if await self._refresh_cookies_with_callback(request_url):
                        continue
                    raise FirewallNotCleared("Firewall page returned; refresh cookies with --refresh-cookies.")
                return text
        if self.browser_fallback_fetch:
            print(f"[info] HTTP 请求重试耗尽，切换浏览器方式抓取: {request_url}")
            return await self.browser_fallback_fetch(request_url)
        raise RuntimeError(f"Unable to fetch page after retries: {request_url}")

    async def _refresh_cookies_with_callback(self, context_url: str) -> bool:
        if not self.refresh_cookies:
            return False
        now = time.monotonic()
        if now - self._last_refresh_ts < self.refresh_cooldown_sec:
            print(
                "[info] 距离上次 cookie 刷新时间过短，先复用最新 cookies 并继续重试..."
            )
            return True
        if self._refresh_lock.locked():
            print("[info] cookie 刷新进行中，等待刷新完成后继续...")
        async with self._refresh_lock:
            now = time.monotonic()
            if now - self._last_refresh_ts < self.refresh_cooldown_sec:
                print(
                    "[info] 其他请求刚完成 cookie 刷新，继续使用新 cookies 重试..."
                )
                return True
            new_cookies = await self.refresh_cookies(context_url)
            if not new_cookies:
                return False
            self.client.cookies.clear()
            for name, value in new_cookies.items():
                self.client.cookies.set(name, value)
            self._last_refresh_ts = time.monotonic()
        return True

    def _resolve_retry_after_seconds(self, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                value = float(retry_after)
                if value > 0:
                    return value
            except ValueError:
                pass
        return self.rate_limit_sleep_sec


def parse_listing_document(
    html: str,
    manufacturer: str,
    year: int,
) -> Tuple[List[GPUListing], Optional[str]]:
    # AJAX requests return JSON with keys like {"list": "<...>", ...}
    try:
        parsed = json.loads(html)
        if isinstance(parsed, dict) and "list" in parsed:
            html = parsed.get("list") or ""
    except Exception:
        # not JSON, continue with original html
        pass

    listings: List[GPUListing] = []
    warning_text: Optional[str] = None

    try:
        from lxml import etree
        html_tree = etree.HTML(html)
        a_elements = html_tree.xpath('//table[@class="items-desktop-table"]//td//div[@class="item-name"]/a')

        for a in a_elements:
            href = a.get('href') or ''
            name = ''.join(a.itertext()).strip()
            detail_url = urljoin(BASE_URL + '/', href.lstrip('/'))
            if not name or not detail_url:
                continue
            listings.append(
                GPUListing(
                    manufacturer=manufacturer,
                    year=year,
                    name=name,
                    detail_url=detail_url,
                )
            )

    except ImportError:
        warning_text = "lxml not installed, strict XPath parsing unavailable"
    except Exception as exc:
        warning_text = f"Error parsing listing with lxml: {exc}"
    else:
        dedup = {item.detail_url: item for item in listings}
        return list(dedup.values()), None

    return [], warning_text


def merge_url_query(url: str, params: Optional[Dict[str, str]]) -> str:
    if not params:
        return url
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))


def to_human_listing_url(url: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "ajax" in query:
        query.pop("ajax", None)
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))


def normalize_detail_url(url: str) -> str:
    parsed = urlsplit((url or "").strip())
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))




def parse_detail_document(html: str, url: str) -> GPUDetail:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("main h1") or soup.select_one("h1")
    title = title_el.get_text(strip=True) if title_el else url

    hero = collect_key_values(
        soup.select_one("div.gpu-specs__summary")
        or soup.select_one("div#specs")
        or soup.select_one("section:first-of-type")
    )

    sections = collect_sections(soup)
    multiplier = detect_multi_chip_multiplier(soup, title, url, hero, sections)
    if multiplier > 1:
        sections = apply_multi_chip_multiplier(sections, multiplier)

    images = []
    for img in soup.select("img"):
        src = img.get("src") or ""
        if "gpu-specs" in src or "images-new" in src:
            images.append(urljoin(BASE_URL + "/", src.lstrip("/")))
    return GPUDetail(url=url, title=title, hero=hero, sections=sections, images=images)


def find_section_block(heading):
    sibling = heading.find_next_sibling()
    while sibling:
        if sibling.name and sibling.name.startswith("h") and sibling.name == heading.name:
            break
        if sibling.name in {"table", "dl", "div"}:
            return sibling
        sibling = sibling.find_next_sibling()
    return None


def normalize_section_title(title: str) -> str:
    return " ".join((title or "").strip().lower().split())


def merge_section_data(target: Dict[str, str], source: Dict[str, str]) -> Dict[str, str]:
    for key, value in source.items():
        if key and value and key not in target:
            target[key] = value
    return target


def collect_sections(soup: BeautifulSoup) -> Dict[str, Dict[str, str]]:
    sections: Dict[str, Dict[str, str]] = {}
    seen_norm_titles: Dict[str, str] = {}

    def upsert_section(title: str, data: Dict[str, str]) -> None:
        if not title or not data:
            return
        norm = normalize_section_title(title)
        if not norm:
            return
        existing_title = seen_norm_titles.get(norm)
        if existing_title:
            merge_section_data(sections[existing_title], data)
            return
        sections[title] = dict(data)
        seen_norm_titles[norm] = title

    headings = soup.select("main h2, main h3, main h4, h2.section, h3.section, h4.section")
    for heading in headings:
        section_title = heading.get_text(" ", strip=True)
        candidates = []
        if heading.parent:
            candidates.append(heading.parent)
        block = find_section_block(heading)
        if block:
            candidates.append(block)

        section_data: Dict[str, str] = {}
        for node in candidates:
            merge_section_data(section_data, collect_key_values(node))
        upsert_section(section_title, section_data)

    for required in REQUIRED_DETAIL_SECTIONS:
        required_norm = normalize_section_title(required)
        if required_norm in seen_norm_titles:
            continue
        heading = soup.find(
            ["h2", "h3", "h4", "h5", "strong"],
            string=lambda s: isinstance(s, str)
            and required_norm in normalize_section_title(s),
        )
        if not heading:
            continue
        section_data = collect_key_values(heading.parent) if heading.parent else {}
        block = find_section_block(heading)
        if block:
            merge_section_data(section_data, collect_key_values(block))
        upsert_section(required, section_data)

    return sections


def detect_multi_chip_multiplier(
    soup: BeautifulSoup,
    title: str,
    url: str,
    hero: Dict[str, str],
    sections: Dict[str, Dict[str, str]],
) -> int:
    class_multiplier = detect_multi_chip_multiplier_from_classes(soup)
    if class_multiplier > 1:
        return class_multiplier

    title_norm = normalize_section_title(title)
    url_norm = normalize_section_title(url)
    for hint, multiplier in MULTI_CHIP_TITLE_HINTS.items():
        if hint in title_norm or hint in url_norm:
            return multiplier

    probe_values: List[str] = [title]
    probe_keys = {
        "gpu name",
        "graphics processor",
        "gpu",
        "chip",
        "multi-gpu",
        "gpu count",
    }
    for key, value in hero.items():
        if normalize_section_title(key) in probe_keys:
            probe_values.append(value)
    for section_name, items in sections.items():
        if "graphics processor" in normalize_section_title(section_name):
            probe_values.extend(items.values())
        for key, value in items.items():
            if normalize_section_title(key) in probe_keys:
                probe_values.append(value)

    multiplier = 1
    for text in probe_values:
        content = text or ""
        if not re.search(r"(?i)multi\s*-?\s*gpu|dual\s*-?\s*gpu|quad\s*-?\s*gpu", content):
            continue
        for matched in re.finditer(r"(?i)(?:\bx\s*(\d+)\b|\b(\d+)\s*gpu\b)", content):
            for idx in range(1, 3):
                token = matched.group(idx)
                if token and token.isdigit():
                    value = int(token)
                    if 2 <= value <= 8:
                        multiplier = max(multiplier, value)
    return multiplier


def detect_multi_chip_multiplier_from_classes(soup: BeautifulSoup) -> int:
    multiplier = 1
    for node in soup.select("dd.multigpu[class], td.multigpu[class]"):
        classes = node.get("class") or []
        for cls in classes:
            match = re.search(r"multigpu-x(\d+)", cls, flags=re.IGNORECASE)
            if not match:
                continue
            value = int(match.group(1))
            if 2 <= value <= 8:
                multiplier = max(multiplier, value)
    return multiplier


def apply_multi_chip_multiplier(
    sections: Dict[str, Dict[str, str]],
    multiplier: int,
) -> Dict[str, Dict[str, str]]:
    if multiplier <= 1:
        return sections
    targets = {
        "memory",
        "render config",
        "theoretical performance",
    }
    transformed: Dict[str, Dict[str, str]] = {}
    for section_name, items in sections.items():
        section_norm = normalize_section_title(section_name)
        if section_norm not in targets:
            transformed[section_name] = items
            continue

        updated: Dict[str, str] = {}
        for key, value in items.items():
            key_norm = normalize_section_title(key)
            if section_norm == "memory" and "clock" in key_norm:
                updated[key] = value
                continue
            updated[key] = multiply_value_numbers(value, multiplier)
        transformed[section_name] = updated
    return transformed


def multiply_value_numbers(value: str, multiplier: int) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        plain = token.replace(",", "")
        try:
            if "." in plain:
                number = float(plain)
                result = number * multiplier
                text = f"{result:.4f}".rstrip("0").rstrip(".")
                return text
            number = int(plain)
            return f"{number * multiplier:,}"
        except ValueError:
            return token

    return re.sub(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", repl, value)


def collect_key_values(node) -> Dict[str, str]:
    if not node:
        return {}

    data: Dict[str, str] = {}
    for row in node.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        key = cells[0].get_text(strip=True).rstrip(":")
        value = cells[1].get_text(" ", strip=True)
        if key and value:
            data[key] = value

    for dl in node.select("dl"):
        terms = dl.find_all("dt")
        for term in terms:
            dd = term.find_next_sibling("dd")
            if not dd:
                continue
            key = term.get_text(strip=True).rstrip(":")
            value = dd.get_text(" ", strip=True)
            if key and value:
                data[key] = value

    for item in node.select("li"):
        label = item.find(class_="label") or item.find("strong")
        value = item.find(class_="value") or item
        if not label:
            continue
        key = label.get_text(strip=True).rstrip(":")
        text = value.get_text(" ", strip=True)
        if key and text:
            data[key] = text
    return data


async def bootstrap_storage_state(
    state_path: Path,
    headless: bool,
    wait_seconds: int,
    manual_confirm: bool,
    target_url: Optional[str] = None,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context()
        page = await context.new_page()
        open_url = target_url or f"{BASE_URL}{LIST_PATH}"
        print("[info] 打开浏览器获取 TechPowerUp cookies...")
        await page.goto(open_url, wait_until="domcontentloaded")
        body_text = await page.inner_text("body")
        if "Automated bot check" in body_text:
            print(
                "[info] 页面显示人机验证，请在弹出的浏览器窗口中移动鼠标/等待，"
                "或手动完成验证。"
            )
            if manual_confirm:
                print("[info] 完成验证后回到终端按 Enter 继续...")
                await asyncio.to_thread(input, "已看到当前目标页面内容后按 Enter: ")
            else:
                try:
                    await page.wait_for_function(
                        "() => !document.body.innerText.includes('Automated bot check')",
                        timeout=wait_seconds * 1000,
                    )
                except PlaywrightTimeoutError as exc:
                    await browser.close()
                    raise RuntimeError(
                        "未能在限定时间内通过人机验证，可加 --manual-confirm 并手动完成验证"
                    ) from exc
        body_text = await page.inner_text("body")
        if "Automated bot check" in body_text:
            await browser.close()
            raise RuntimeError("人机验证仍未通过，可能需要更长等待或更换网络/IP")
        await context.storage_state(path=str(state_path))
        await browser.close()
        print(f"[info] cookies 已保存到 {state_path}")


async def fetch_html_via_browser(
    url: str,
    state_path: Path,
    *,
    headless: bool,
    wait_seconds: int,
    manual_confirm: bool,
) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(storage_state=str(state_path) if state_path.exists() else None)
        page = await context.new_page()
        print(f"[info] 浏览器直连抓取: {url}")
        await page.goto(url, wait_until="domcontentloaded")
        body_text = await page.inner_text("body")
        block_reason = detect_block_reason(body_text)
        if block_reason:
            print(f"[info] 浏览器直连命中风控页面（{block_reason}），尝试处理后继续...")
            if manual_confirm:
                print("[info] 完成验证后回到终端按 Enter 继续...")
                await asyncio.to_thread(input, "已看到目标页面内容后按 Enter: ")
            else:
                try:
                    await page.wait_for_function(
                        "() => { const t = document.body?.innerText || ''; return !/Automated bot check|Too Many Requests|HTTP 429/i.test(t); }",
                        timeout=wait_seconds * 1000,
                    )
                except PlaywrightTimeoutError as exc:
                    await browser.close()
                    raise RuntimeError(f"浏览器直连抓取时仍受限（{block_reason}）") from exc
        body_text = await page.inner_text("body")
        block_reason = detect_block_reason(body_text)
        if block_reason:
            await browser.close()
            raise RuntimeError(f"浏览器直连抓取失败，页面仍受限（{block_reason}）: {url}")
        html = await page.content()
        await context.storage_state(path=str(state_path))
        await browser.close()
        return html


def detect_block_reason(text: str) -> Optional[str]:
    content = text or ""
    for marker in BLOCK_PAGE_MARKERS:
        if marker.lower() in content.lower():
            return marker
    return None


def build_listing_filter_url(filter_template: str, manufacturer: str, year: int) -> str:
    query = urlencode({"f": filter_template.format(manufacturer=manufacturer, year=year)})
    return f"{BASE_URL}{LIST_PATH}?{query}"


def load_cookies(state_path: Path) -> Dict[str, str]:
    data = json.loads(state_path.read_text(encoding="utf-8"))
    cookies = {}
    for cookie in data.get("cookies", []):
        domain = cookie.get("domain") or ""
        if "techpowerup.com" not in domain:
            continue
        cookies[cookie["name"]] = cookie["value"]
    return cookies


def resolve_manufacturers(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return list(DEFAULT_MANUFACTURERS)
    canonical = {item.lower(): item for item in DEFAULT_MANUFACTURERS}
    resolved: List[str] = []
    for value in values:
        token = value.strip()
        if not token:
            continue
        resolved.append(canonical.get(token.lower(), token))
    return resolved or list(DEFAULT_MANUFACTURERS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓取 TechPowerUp GPU Database 列表及详情",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    current_year = datetime.now().year
    parser.add_argument("--start-year", type=int, default=current_year - 1, help="起始年份")
    parser.add_argument("--end-year", type=int, default=current_year, help="结束年份 (包含)")
    parser.add_argument(
        "--manufacturers",
        nargs="+",
        help="厂商过滤，默认使用内置集合 (NVIDIA/AMD/Intel/ATI/3dfx/Matrox/XGI/Sony)",
    )
    parser.add_argument(
        "--filter-template",
        default="year_{year}~mfgr_{manufacturer}",
        help="TechPowerUp 查询过滤模板",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="相邻过滤请求的等待秒数")
    parser.add_argument("--detail-delay", type=float, default=1.0, help="详情页请求之间的等待秒数（防速率限制）")
    parser.add_argument("--max-gpus", type=int, default=None, help="最多抓取的 GPU 数量")
    parser.add_argument("--skip-details", action="store_true", help="仅抓取列表不访问详情页")
    parser.add_argument("--concurrency", type=int, default=2, help="详情页并发请求数")
    parser.add_argument("--http-timeout", type=float, default=30.0, help="HTTP 超时时间")
    parser.add_argument(
        "--http2",
        action="store_true",
        help="启用 HTTP/2（需要 pip install 'httpx[http2]'）",
    )
    parser.add_argument("--storage-state", default=".playwright-state.json", help="Playwright cookies 文件")
    parser.add_argument("--refresh-cookies", action="store_true", help="忽略已有 cookies 并重新获取")
    parser.add_argument("--headless", action="store_true", help="以 headless 模式打开浏览器获取 cookies")
    parser.add_argument("--wait-seconds", type=int, default=120, help="等待人机验证通过的最长秒数")
    parser.add_argument(
        "--manual-confirm",
        action="store_true",
        help="需要人工确认时，验证完成后回终端按 Enter 继续",
    )
    parser.add_argument(
        "--auto-refresh-on-429",
        action="store_true",
        help="发生 429 时尝试自动刷新 cookies 并继续任务",
    )
    parser.add_argument(
        "--refresh-cooldown",
        type=float,
        default=20.0,
        help="自动刷新 cookies 的最小间隔秒数（避免并发请求频繁重复刷新）",
    )
    parser.add_argument(
        "--rate-limit-sleep",
        type=float,
        default=12.0,
        help="429 后重试前的等待秒数（若响应含 Retry-After 则优先使用该值）",
    )
    parser.add_argument(
        "--browser-fallback-on-fail",
        action="store_true",
        help="当 HTTP 重试耗尽时，使用 Playwright 浏览器直连抓取页面",
    )
    parser.add_argument(
        "--prefer-browser-for-details",
        action="store_true",
        help="详情页阶段优先使用浏览器抓取（可减少 HTTP 429）",
    )
    parser.add_argument(
        "--prefer-browser-for-listings",
        action="store_true",
        help="列表页阶段优先使用浏览器抓取（可减少 HTTP 429/空列表）",
    )
    parser.add_argument(
        "--fill-missing-details",
        action="store_true",
        help="基于现有输出文件，仅补抓缺失详情并追加写回",
    )
    parser.add_argument(
        "--log-detail-progress",
        action="store_true",
        help="输出详情页抓取的开始/完成日志",
    )
    parser.add_argument("--output", default="gpu_specs.json", help="输出 JSON 文件")
    parser.add_argument("--pretty", action="store_true", help="以缩进格式写入 JSON")
    return parser


async def run(args: argparse.Namespace) -> Dict[str, object]:
    if args.start_year <= 0 or args.end_year < args.start_year:
        raise ValueError("年份区间不合法")

    manufacturers = resolve_manufacturers(args.manufacturers)
    combos = [
        (manufacturer, year)
        for year in range(args.start_year, args.end_year + 1)
        for manufacturer in manufacturers
    ]

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
    if args.http2:
        try:
            import h2  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError("启用了 --http2 但缺少依赖，请执行 pip install 'httpx[http2]'") from exc

    listings: List[GPUListing] = []
    filter_log: List[Dict[str, object]] = []
    refresh_cb: Optional[Callable[[str], Awaitable[Dict[str, str]]]] = None
    browser_fetch_cb: Optional[Callable[[str], Awaitable[str]]] = None
    if args.auto_refresh_on_429:
        async def refresh_cookies(context_url: str) -> Dict[str, str]:
            target_url = to_human_listing_url(context_url)
            print(f"[info] 检测到 429，尝试在目标页刷新 cookies: {target_url}")
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

    async with httpx.AsyncClient(**client_kwargs) as client:
        scraper = TechPowerUpGPUClient(
            client,
            filter_template=args.filter_template,
            refresh_cookies=refresh_cb,
            refresh_cooldown_sec=args.refresh_cooldown,
            rate_limit_sleep_sec=args.rate_limit_sleep,
            browser_fallback_fetch=browser_fetch_cb,
            prefer_browser_for_details=args.prefer_browser_for_details,
        )
        detail_map: Dict[str, GPUDetail] = {}
        failed_details: List[Dict[str, str]] = []

        if args.fill_missing_details:
            output_path = Path(args.output)
            if not output_path.exists():
                raise RuntimeError("启用 --fill-missing-details 时输出文件不存在")
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            existing_listings = payload.get("listings", [])
            listings = [
                GPUListing(
                    manufacturer=item.get("manufacturer", ""),
                    year=int(item.get("year", 0) or 0),
                    name=item.get("name", ""),
                    detail_url=item.get("detail_url", ""),
                )
                for item in existing_listings
                if item.get("detail_url")
            ]

            existing_details = payload.get("details", {})
            normalized_detail_map: Dict[str, str] = {}
            for key in existing_details.keys():
                normalized_detail_map[normalize_detail_url(key)] = key

            missing_urls = []
            for item in listings:
                norm = normalize_detail_url(item.detail_url)
                if norm not in normalized_detail_map:
                    missing_urls.append(item.detail_url)
            if args.max_gpus:
                missing_urls = missing_urls[: args.max_gpus]
            if not missing_urls:
                print("[done] 未发现缺失详情，无需补抓。")
                return payload

            print(f"[info] 缺失详情数量: {len(missing_urls)}，开始补抓...")
            fetched = await fetch_details_concurrently(
                scraper,
                missing_urls,
                args.concurrency,
                args.detail_delay,
                log_progress=args.log_detail_progress,
                failures=failed_details,
            )
            for url, detail in fetched.items():
                norm = normalize_detail_url(url)
                existing_key = normalized_detail_map.get(norm, url)
                existing_details[existing_key] = asdict(detail)
                normalized_detail_map[norm] = existing_key
            payload["details"] = existing_details
            payload["generated_at"] = datetime.now(timezone.utc).isoformat()
            payload["count"] = len(existing_listings)
            if failed_details:
                payload["failed_details"] = failed_details
                print(f"[warn] 本次补抓失败 {len(failed_details)} 条，已写入 failed_details")

            indent = 2 if args.pretty else None
            output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
            print(f"[done] 已补抓 {len(fetched)} 条缺失详情，结果写回 {args.output}")
            return payload

        default_browser_fetch = scraper.browser_fallback_fetch
        for idx, (manufacturer, year) in enumerate(combos):
            expected_listing_url = build_listing_filter_url(args.filter_template, manufacturer, year)
            if args.prefer_browser_for_listings:
                html = await fetch_html_via_browser(
                    expected_listing_url,
                    state_path,
                    headless=args.headless,
                    wait_seconds=args.wait_seconds,
                    manual_confirm=args.manual_confirm,
                )
                rows, notice = parse_listing_document(html, manufacturer, year)
            else:
                if args.browser_fallback_on_fail:
                    async def listing_browser_fetch(_: str) -> str:
                        return await fetch_html_via_browser(
                            expected_listing_url,
                            state_path,
                            headless=args.headless,
                            wait_seconds=args.wait_seconds,
                            manual_confirm=args.manual_confirm,
                        )

                    scraper.browser_fallback_fetch = listing_browser_fetch
                try:
                    rows, notice = await scraper.fetch_listing(manufacturer, year)
                finally:
                    scraper.browser_fallback_fetch = default_browser_fetch

            filter_log.append(
                {
                    "manufacturer": manufacturer,
                    "year": year,
                    "results": len(rows),
                    "notice": notice,
                }
            )
            if notice:
                print(f"[warn] {notice} (manufacturer={manufacturer}, year={year})")
            print(f"[info] {manufacturer} {year}: {len(rows)} 款 GPU")
            listings.extend(rows)
            if args.max_gpus and len(listings) >= args.max_gpus:
                listings = listings[: args.max_gpus]
                print(f"[info] 已达到上限 {args.max_gpus}，停止继续抓取。")
                break
            if args.delay > 0 and idx < len(combos) - 1:
                await asyncio.sleep(args.delay)

        if not args.skip_details and listings:
            urls = [item.detail_url for item in listings]
            detail_map = await fetch_details_concurrently(
                scraper,
                urls,
                args.concurrency,
                args.detail_delay,
                log_progress=args.log_detail_progress,
                failures=failed_details,
            )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_year": args.start_year,
        "end_year": args.end_year,
        "manufacturers": manufacturers,
        "filter_template": args.filter_template,
        "filters": filter_log,
        "count": len(listings),
        "listings": [asdict(item) for item in listings],
        "details": {url: asdict(detail_map[url]) for url in detail_map},
    }
    if not args.skip_details and failed_details:
        payload["failed_details"] = failed_details
        print(f"[warn] 详情抓取失败 {len(failed_details)} 条，已写入 failed_details，可后续补抓。")

    indent = 2 if args.pretty else None
    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    print(f"[done] 共抓取 {len(listings)} 款 GPU，结果写入 {args.output}")
    return payload


async def fetch_details_concurrently(
    scraper: TechPowerUpGPUClient,
    urls: List[str],
    concurrency: int,
    delay_sec: float = 1.0,
    *,
    log_progress: bool = False,
    failures: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, GPUDetail]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: Dict[str, GPUDetail] = {}
    request_count = [0]  # Use list to allow modification in nested function

    async def worker(detail_url: str) -> None:
        async with semaphore:
            # Add delay between requests to avoid rate limiting
            if request_count[0] > 0 and delay_sec > 0:
                await asyncio.sleep(delay_sec)
            request_count[0] += 1
            if log_progress:
                print(f"[detail] Fetching ({request_count[0]}/{len(urls)}): {detail_url}")
            try:
                detail = await scraper.fetch_detail(detail_url)
                results[detail_url] = detail
                if log_progress:
                    print(f"[detail] Done ({request_count[0]}/{len(urls)}): {detail_url}")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                print(f"[warn] 详情抓取失败，已跳过: {detail_url} | {msg}")
                if failures is not None:
                    failures.append({"url": detail_url, "error": msg})

    tasks = [asyncio.create_task(worker(url)) for url in urls]
    await asyncio.gather(*tasks)
    return results


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except FirewallNotCleared as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[warn] 用户中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
