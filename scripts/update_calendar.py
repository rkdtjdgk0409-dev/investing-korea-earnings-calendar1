from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

URLS = [
    "https://www.investing.com/earnings-calendar/",
    "https://kr.investing.com/earnings-calendar/",
]

KOREA_WORDS = ("south korea", "korea", "대한민국", "한국")
HIGH_IMPORTANCE_WORDS = ("high", "높음", "3")
DATE_PATTERNS = (
    "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%m/%d/%Y",
    "%Y년 %m월 %d일", "%m월 %d일",
)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date(text: str, base_year: int | None = None) -> str | None:
    text = clean(text)
    if not text:
        return None

    # ISO-like date inside attributes/text
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        except ValueError:
            pass

    # Korean date without year
    m = re.search(r"(?:(20\d{2})년\s*)?(\d{1,2})월\s*(\d{1,2})일", text)
    if m:
        year = int(m.group(1)) if m.group(1) else (base_year or datetime.now().year)
        try:
            dt = datetime(year, int(m.group(2)), int(m.group(3))).date()
            # Around year-end, a January date shown in December is usually next year.
            today = datetime.now().date()
            if not m.group(1) and dt < today - timedelta(days=180):
                dt = dt.replace(year=year + 1)
            return dt.isoformat()
        except ValueError:
            pass

    try:
        dt = date_parser.parse(text, fuzzy=True, default=datetime(base_year or datetime.now().year, 1, 1))
        return dt.date().isoformat()
    except Exception:
        return None


def is_korea(row) -> bool:
    blob = " ".join([
        clean(row.get_text(" ", strip=True)),
        " ".join(clean(v) for v in row.attrs.values() if isinstance(v, str)),
        " ".join(
            clean(str(v))
            for tag in row.find_all(True)
            for v in tag.attrs.values()
            if isinstance(v, str)
        ),
    ]).lower()

    if any(word in blob for word in KOREA_WORDS):
        return True

    # Investing commonly uses country flag keys/codes.
    return bool(re.search(r"(flag|country)[^ ]*(kr|kor|11)\b", blob))


def importance_score(row) -> int:
    blob = str(row).lower()

    # Common Investing layouts use bull icons. Count visible/active bull icons.
    active_bulls = len(re.findall(r"(?:grayFullBullishIcon|fullBullishIcon|bull\d*.*active)", blob, flags=re.I))
    if active_bulls:
        return min(active_bulls, 3)

    # aria-label/title/data attributes.
    for attr in ("data-importance", "data-importance-level", "aria-label", "title"):
        for tag in [row, *row.find_all(True)]:
            value = clean(tag.get(attr)).lower()
            if value:
                if any(word in value for word in ("high", "높음")):
                    return 3
                m = re.search(r"\b([1-3])\b", value)
                if m:
                    return int(m.group(1))

    # Some pages render three bull icons, with gray icons for inactive levels.
    bull_tags = row.select('[class*="bull"], [class*="Bull"]')
    non_gray = [
        x for x in bull_tags
        if "gray" not in " ".join(x.get("class", [])).lower()
        and "muted" not in " ".join(x.get("class", [])).lower()
    ]
    return min(len(non_gray), 3)


def extract_company(row) -> tuple[str | None, str | None]:
    selectors = [
        'a[href*="/equities/"]',
        'a[href*="-earnings"]',
        '[data-test="event-name"]',
        ".earnCalCompanyName",
        "td:nth-of-type(2) a",
    ]
    for selector in selectors:
        tag = row.select_one(selector)
        if tag and clean(tag.get_text(" ", strip=True)):
            return clean(tag.get_text(" ", strip=True)), tag.get("href")

    cells = [clean(td.get_text(" ", strip=True)) for td in row.find_all(["td", "div"], recursive=False)]
    candidates = [x for x in cells if x and not re.fullmatch(r"[-+/%\d.,BTMK\s]+", x)]
    return (candidates[0], None) if candidates else (None, None)


def extract_events_from_html(html: str, source_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    current_date: str | None = None

    # Earnings calendar has changed markup several times; scan both rows and row-like divs.
    nodes = soup.select(
        "table tr, "
        '[data-test*="earnings"] [role="row"], '
        '[data-test="calendar-row"], '
        '[class*="earnings"] [class*="row"]'
    )

    for node in nodes:
        text = clean(node.get_text(" ", strip=True))
        if not text:
            continue

        # Date header or date-bearing row.
        date_candidates = [
            node.get("data-date"),
            node.get("datetime"),
            node.get("title"),
            text,
        ]
        detected_date = next((parse_date(x) for x in date_candidates if x and parse_date(x)), None)

        has_company_link = bool(node.select_one('a[href*="/equities/"], a[href*="-earnings"]'))
        if detected_date and not has_company_link and len(text) < 80:
            current_date = detected_date
            continue

        company, href = extract_company(node)
        if not company:
            continue
        if not is_korea(node):
            continue
        if importance_score(node) < 3:
            continue

        event_date = (
            parse_date(node.get("data-date") or "")
            or parse_date(node.get("datetime") or "")
            or detected_date
            or current_date
        )
        if not event_date:
            continue

        # Avoid accidental header words.
        if company.lower() in {"company", "기업", "name", "symbol"}:
            continue

        link = href or source_url
        if link.startswith("/"):
            link = "https://www.investing.com" + link

        events.append({
            "title": f"{company} 실적",
            "company": company,
            "start": event_date,
            "allDay": True,
            "url": link,
            "country": "한국",
            "importance": "높음",
            "source": "Investing.com",
        })

    return events


async def dismiss_popups(page: Page) -> None:
    labels = [
        "Accept All", "I Accept", "Accept", "동의", "모두 동의",
        "No thanks", "닫기", "Close",
    ]
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await button.count():
                await button.first.click(timeout=1500)
        except Exception:
            pass


async def set_filters(page: Page) -> None:
    """Best-effort UI filtering. Final HTML parser filters again, so UI changes are not trusted."""
    await dismiss_popups(page)

    # Open Filters.
    for name in ("Filters", "필터", "Filter"):
        try:
            target = page.get_by_text(name, exact=True)
            if await target.count():
                await target.first.click(timeout=2500)
                await page.wait_for_timeout(700)
                break
        except Exception:
            pass

    # Country: South Korea / 한국.
    for label in ("South Korea", "Korea", "한국", "대한민국"):
        try:
            item = page.get_by_text(label, exact=True)
            if await item.count():
                await item.first.click(timeout=2000)
                break
        except Exception:
            pass

    # Importance: High / 높음.
    for label in ("High", "높음"):
        try:
            item = page.get_by_text(label, exact=True)
            if await item.count():
                await item.first.click(timeout=2000)
                break
        except Exception:
            pass

    # Apply if present.
    for label in ("Apply", "적용", "Done", "완료"):
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I))
            if await button.count():
                await button.first.click(timeout=2000)
                break
        except Exception:
            pass

    await page.wait_for_timeout(2500)


async def scrape() -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    errors: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1100},
        )

        for url in URLS:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(4000)
                await set_filters(page)

                # Collect the default period and the "next week" view when available.
                html = await page.content()
                collected.extend(extract_events_from_html(html, page.url))

                for label in ("Next Week", "다음 주"):
                    try:
                        next_week = page.get_by_text(label, exact=True)
                        if await next_week.count():
                            await next_week.first.click(timeout=3000)
                            await page.wait_for_timeout(3000)
                            html = await page.content()
                            collected.extend(extract_events_from_html(html, page.url))
                            break
                    except Exception:
                        pass

                if collected:
                    await page.screenshot(path=str(OUT_DIR / "last_success.png"), full_page=True)
                    break
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                try:
                    await page.screenshot(path=str(OUT_DIR / "last_error.png"), full_page=True)
                except Exception:
                    pass
            finally:
                await page.close()

        await browser.close()

    # De-duplicate.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for event in collected:
        unique[(event["start"], event["company"])] = event

    events = sorted(unique.values(), key=lambda x: (x["start"], x["company"]))

    # Do not erase good data just because Investing blocked one scheduled run.
    events_path = OUT_DIR / "events.json"
    if not events and events_path.exists():
        old = json.loads(events_path.read_text(encoding="utf-8"))
        if old:
            (OUT_DIR / "status.json").write_text(json.dumps({
                "ok": False,
                "updated_at": datetime.now().astimezone().isoformat(),
                "message": "이번 실행에서 새 데이터를 얻지 못해 기존 데이터를 유지했습니다.",
                "errors": errors,
                "event_count": len(old),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            return old

    (OUT_DIR / "status.json").write_text(json.dumps({
        "ok": bool(events),
        "updated_at": datetime.now().astimezone().isoformat(),
        "message": "갱신 완료" if events else "데이터를 찾지 못했습니다.",
        "errors": errors,
        "event_count": len(events),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return events


def escape_ics(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def write_outputs(events: list[dict[str, Any]]) -> None:
    (OUT_DIR / "events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//GitHub//Investing Korea Earnings//KO",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:한국 주요 기업 실적",
        "X-WR-TIMEZONE:Asia/Seoul",
    ]
    for event in events:
        day = event["start"].replace("-", "")
        next_day = (datetime.fromisoformat(event["start"]).date() + timedelta(days=1)).strftime("%Y%m%d")
        uid = re.sub(r"[^a-zA-Z0-9]", "", f'{day}-{event["company"]}') + "@github"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{day}",
            f"DTEND;VALUE=DATE:{next_day}",
            f"SUMMARY:{escape_ics(event['title'])}",
            f"URL:{event.get('url', '')}",
            "DESCRIPTION:Investing.com / 국가: 한국 / 중요도: 높음",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    (ROOT / "docs" / "earnings.ics").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


async def main() -> None:
    events = await scrape()
    write_outputs(events)
    print(f"Wrote {len(events)} events")


if __name__ == "__main__":
    asyncio.run(main())
