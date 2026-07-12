from __future__ import annotations

import asyncio
import calendar
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == "scripts" else Path(__file__).resolve().parent
OUT_DIR = ROOT / "docs" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_URLS = (
    "https://www.investing.com/earningscalendar/",
    "https://kr.investing.com/earningscalendar/",
)

MONTHS_AHEAD = 3
WINDOW_DAYS = 7

KOREA_MARKERS = (
    "south korea",
    "south_korea",
    "south-korea",
    "대한민국",
    "한국",
    "country_11",
    "country-11",
)


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_date_text(value: str | None) -> str | None:
    text = clean(value)
    if not text:
        return None

    iso = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            pass

    ko = re.search(r"(?:(20\d{2})년\s*)?(\d{1,2})월\s*(\d{1,2})일", text)
    if ko:
        year = int(ko.group(1) or datetime.now().year)
        try:
            return date(year, int(ko.group(2)), int(ko.group(3))).isoformat()
        except ValueError:
            pass

    try:
        parsed = date_parser.parse(text, fuzzy=True)
        if 2000 <= parsed.year <= 2100:
            return parsed.date().isoformat()
    except Exception:
        return None
    return None


def month_range(months_ahead: int = MONTHS_AHEAD) -> tuple[date, date]:
    today = datetime.now(timezone(timedelta(hours=9))).date()
    start = today.replace(day=1)

    year = start.year
    month = start.month + months_ahead - 1
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    return start, end


def weekly_windows(start: date, end: date):
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=WINDOW_DAYS - 1), end)
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def element_blob(element) -> str:
    parts = [clean(element.get_text(" ", strip=True))]
    for tag in [element, *element.find_all(True)]:
        for key, value in tag.attrs.items():
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
            else:
                parts.append(str(value))
        if tag.name in {"img", "span", "i"}:
            parts.append(str(tag.get("title", "")))
            parts.append(str(tag.get("aria-label", "")))
    return " ".join(parts).lower()


def is_korean_row(row) -> bool:
    blob = element_blob(row)
    if any(marker in blob for marker in KOREA_MARKERS):
        return True

    # Investing.com has historically used KR/KOR and numeric country IDs.
    if re.search(r"(?:flag|country|ceflags)[^\"'> ]{0,20}(?:kr|kor)\b", blob):
        return True
    if re.search(r"(?:data-country|country-id)[=\"']?(?:11|304)\b", blob):
        return True
    return False


def importance_score(row) -> int:
    blob = element_blob(row)

    if re.search(r"\b(high|높음|importance[_ -]?3|importance[=\"']?3)\b", blob):
        return 3

    # Legacy Investing calendar rows contain one, two or three bull icons.
    bull_icons = row.select(
        '[class*="BullishIcon"], [class*="bullishIcon"], '
        '[class*="bullish-icon"], [class*="importance"] i'
    )
    if bull_icons:
        visible = []
        for icon in bull_icons:
            style = clean(icon.get("style")).lower()
            classes = " ".join(icon.get("class", [])).lower()
            hidden = (
                "display:none" in style
                or "display: none" in style
                or "visibility:hidden" in style
                or "visibility: hidden" in style
                or icon.get("aria-hidden") == "true"
                and "gray" in classes
            )
            if not hidden:
                visible.append(icon)
        return min(len(visible), 3)

    # Some versions store the level as a numeric attribute.
    for tag in [row, *row.find_all(True)]:
        for attr in ("data-importance", "data-importance-level", "data-impact", "importance"):
            value = clean(tag.get(attr))
            match = re.search(r"\b([1-3])\b", value)
            if match:
                return int(match.group(1))
    return 0


def extract_company(row) -> tuple[str | None, str | None]:
    selectors = (
        ".earnCalCompanyName a",
        '[data-test="event-name"] a',
        '[data-test="event-name"]',
        'a[href*="/equities/"]',
        'a[href*="-earnings"]',
        "td:nth-of-type(2) a",
    )
    for selector in selectors:
        tag = row.select_one(selector)
        if tag:
            name = clean(tag.get_text(" ", strip=True))
            if name:
                return name, tag.get("href")

    # Fallback for table layouts where the company link is plain text.
    cells = row.find_all("td", recursive=False)
    if len(cells) >= 2:
        name = clean(cells[1].get_text(" ", strip=True))
        name = re.sub(r"\s*\([^)]{1,15}\)\s*$", "", name)
        if name:
            return name, None
    return None, None


def extract_events(html: str, source_url: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    current_date: str | None = None

    stats = {
        "rows": 0,
        "dated_rows": 0,
        "company_rows": 0,
        "korea_rows": 0,
        "high_rows": 0,
    }

    nodes = soup.select(
        "#earningsCalendarData tr, "
        "table.earningsCalendarTbl tr, "
        "table tr, "
        '[data-test="calendar-row"], '
        '[data-test*="earnings"] [role="row"], '
        '[class*="earnings"] [role="row"]'
    )

    seen_node_ids: set[int] = set()
    for row in nodes:
        # The broad selectors can return the same node more than once.
        if id(row) in seen_node_ids:
            continue
        seen_node_ids.add(id(row))
        stats["rows"] += 1

        text = clean(row.get_text(" ", strip=True))
        if not text:
            continue

        date_cell = row.select_one(
            "td.theDay, .theDay, [data-test='date-header'], "
            "[class*='dateHeader'], time[datetime]"
        )
        row_date = (
            parse_date_text(row.get("data-date"))
            or parse_date_text(row.get("datetime"))
            or parse_date_text(date_cell.get("datetime") if date_cell else None)
            or parse_date_text(date_cell.get_text(" ", strip=True) if date_cell else None)
        )

        if row_date:
            current_date = row_date
            stats["dated_rows"] += 1
            # A date heading is not an earnings event.
            if not row.select_one(
                ".earnCalCompanyName, [data-test='event-name'], "
                'a[href*="/equities/"], a[href*="-earnings"]'
            ):
                continue

        company, href = extract_company(row)
        if not company:
            continue
        stats["company_rows"] += 1

        if not is_korean_row(row):
            continue
        stats["korea_rows"] += 1

        score = importance_score(row)
        if score < 3:
            continue
        stats["high_rows"] += 1

        event_date = row_date or current_date
        if not event_date:
            continue

        company = re.sub(r"\s+", " ", company).strip()
        if company.lower() in {"company", "기업", "name", "symbol"}:
            continue

        link = urljoin(source_url, href) if href else source_url
        events.append(
            {
                "title": f"{company} 실적",
                "company": company,
                "start": event_date,
                "allDay": True,
                "url": link,
                "country": "한국",
                "importance": "높음",
                "source": "Investing.com",
            }
        )

    return events, stats


async def dismiss_popups(page: Page) -> None:
    for label in (
        "Accept All",
        "I Accept",
        "Accept",
        "동의",
        "모두 동의",
        "No thanks",
        "닫기",
        "Close",
    ):
        try:
            button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await button.count():
                await button.first.click(timeout=1200)
        except Exception:
            pass


async def fetch_html(page: Page, base_url: str, start: date, end: date) -> tuple[str, str]:
    # Legacy calendar accepts custom ranges through dateFrom/dateTo.
    query_formats = (
        {
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
        },
        {
            "dateFrom": start.strftime("%m/%d/%Y"),
            "dateTo": end.strftime("%m/%d/%Y"),
        },
    )

    best_html = ""
    best_url = ""
    for query in query_formats:
        url = f"{base_url}?{urlencode(query)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await dismiss_popups(page)

        try:
            await page.wait_for_selector(
                "#earningsCalendarData, table.earningsCalendarTbl, "
                '[data-test*="earnings"], table',
                timeout=15000,
            )
        except Exception:
            pass

        await page.wait_for_timeout(2500)
        html = await page.content()
        if len(html) > len(best_html):
            best_html = html
            best_url = page.url

        # Stop when the page visibly includes at least one requested date heading.
        body_text = clean(await page.locator("body").inner_text())
        if (
            start.strftime("%B %-d, %Y") in body_text
            or start.strftime("%Y-%m-%d") in body_text
            or start.strftime("%Y년 %-m월 %-d일") in body_text
        ):
            break

    return best_html, best_url


async def scrape() -> list[dict[str, Any]]:
    start, end = month_range()
    collected: list[dict[str, Any]] = []
    debug: list[dict[str, Any]] = []
    errors: list[str] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-US",
            timezone_id="Asia/Seoul",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1100},
        )

        for base_url in BASE_URLS:
            page = await context.new_page()
            try:
                for window_start, window_end in weekly_windows(start, end):
                    try:
                        html, final_url = await fetch_html(page, base_url, window_start, window_end)
                        events, stats = extract_events(html, final_url or base_url)
                        collected.extend(events)
                        debug.append(
                            {
                                "url": final_url,
                                "window": [window_start.isoformat(), window_end.isoformat()],
                                **stats,
                                "events": len(events),
                            }
                        )

                        if not events and stats["rows"] > 0:
                            debug_html = OUT_DIR / (
                                f"debug-{window_start.isoformat()}-{window_end.isoformat()}.html"
                            )
                            debug_html.write_text(html, encoding="utf-8")
                    except Exception as exc:
                        errors.append(
                            f"{base_url} {window_start}~{window_end}: "
                            f"{type(exc).__name__}: {exc}"
                        )

                if collected:
                    await page.screenshot(
                        path=str(OUT_DIR / "last_success.png"),
                        full_page=True,
                    )
                    break
            finally:
                await page.close()

        await browser.close()

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for event in collected:
        unique[(event["start"], event["company"])] = event
    events = sorted(unique.values(), key=lambda item: (item["start"], item["company"]))

    status = {
        "ok": bool(events),
        "updated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "message": "갱신 완료" if events else "한국·중요도 높음 실적을 찾지 못했습니다.",
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "event_count": len(events),
        "errors": errors,
        "debug": debug,
    }

    events_path = OUT_DIR / "events.json"
    if not events and events_path.exists():
        try:
            previous = json.loads(events_path.read_text(encoding="utf-8"))
        except Exception:
            previous = []
        if previous:
            status["message"] = "이번 실행은 0건이라 기존 정상 데이터를 유지했습니다."
            status["event_count"] = len(previous)
            (OUT_DIR / "status.json").write_text(
                json.dumps(status, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return previous

    (OUT_DIR / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return events


def escape_ics(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def write_outputs(events: list[dict[str, Any]]) -> None:
    (OUT_DIR / "events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
        event_day = date.fromisoformat(event["start"])
        next_day = event_day + timedelta(days=1)
        uid_key = re.sub(r"[^a-zA-Z0-9]", "", f"{event_day}-{event['company']}")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid_key}@github",
                f"DTSTAMP:{now}",
                f"DTSTART;VALUE=DATE:{event_day.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{next_day.strftime('%Y%m%d')}",
                f"SUMMARY:{escape_ics(event['title'])}",
                f"URL:{event.get('url', '')}",
                "DESCRIPTION:Investing.com / 국가: 한국 / 중요도: 높음",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    (ROOT / "docs" / "earnings.ics").write_text(
        "\r\n".join(lines) + "\r\n",
        encoding="utf-8",
    )


async def main() -> None:
    events = await scrape()
    write_outputs(events)
    print(f"Wrote {len(events)} events")


if __name__ == "__main__":
    asyncio.run(main())
