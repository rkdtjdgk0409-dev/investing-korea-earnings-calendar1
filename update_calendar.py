from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import FinanceDataReader as fdr
from bs4 import BeautifulSoup, Tag
from curl_cffi import requests
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta


VERSION = "v5.1-strict-rolling-3month"
SEOUL = ZoneInfo("Asia/Seoul")

# Investing.com 필터 값
KOREA_COUNTRY_ID = "11"
HIGH_IMPORTANCE_ID = "3"

# 긴 기간 요청이 현재/다음 주 데이터로 되돌아오는 현상을 막기 위해
# 14일 단위로 나누고, 응답 날짜가 요청 구간과 실제로 겹치는지 검증합니다.
WINDOW_DAYS = 14
MAX_PAGES_PER_WINDOW = 10

HOSTS = (
    "https://kr.investing.com",
    "https://www.investing.com",
)

DATE_FORMATS = (
    "iso",       # 2026-07-12
    "us",        # 07/12/2026
    "day_first", # 12/07/2026
)

SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parent
DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
EVENTS_PATH = DATA_DIR / "events.json"
STATUS_PATH = DATA_DIR / "status.json"
ICS_PATH = DOCS_DIR / "earnings.ics"
MARKET_CAP_CACHE_PATH = DATA_DIR / "market_caps.json"


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def rolling_range() -> tuple[date, date]:
    start = datetime.now(SEOUL).date()
    end = start + relativedelta(months=3)
    return start, end


def make_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cursor = start

    while cursor <= end:
        window_end = min(cursor + timedelta(days=WINDOW_DAYS - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)

    return windows


def format_request_date(value: date, mode: str) -> str:
    if mode == "iso":
        return value.isoformat()
    if mode == "us":
        return value.strftime("%m/%d/%Y")
    if mode == "day_first":
        return value.strftime("%d/%m/%Y")
    raise ValueError(f"지원하지 않는 날짜 형식: {mode}")


def parse_date_value(value: Any, default_year: int) -> date | None:
    text = clean(value)
    if not text:
        return None

    # Investing.com의 날짜 구분행 ID: theDay169...
    timestamp_match = re.search(r"(?:theDay)?(\d{10,13})", text)
    if timestamp_match:
        try:
            timestamp = int(timestamp_match.group(1))
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, timezone.utc).date()
            if 2000 <= parsed.year <= 2100:
                return parsed
        except (ValueError, OSError, OverflowError):
            pass

    iso_match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if iso_match:
        try:
            return date(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            )
        except ValueError:
            pass

    korean_match = re.search(
        r"(?:(20\d{2})년\s*)?(\d{1,2})월\s*(\d{1,2})일",
        text,
    )
    if korean_match:
        try:
            return date(
                int(korean_match.group(1) or default_year),
                int(korean_match.group(2)),
                int(korean_match.group(3)),
            )
        except ValueError:
            pass

    try:
        parsed = date_parser.parse(
            text,
            fuzzy=True,
            default=datetime(default_year, 1, 1),
        )
        if 2000 <= parsed.year <= 2100:
            return parsed.date()
    except (ValueError, TypeError, OverflowError):
        pass

    return None


def row_date(row: Tag, default_year: int) -> date | None:
    selectors = (
        'td[id^="theDay"]',
        '[id^="theDay"]',
        "td.theDay",
        ".theDay",
        '[data-test="date-header"]',
        '[class*="dateHeader"]',
        "time[datetime]",
    )

    for selector in selectors:
        node = row.select_one(selector)
        if not node:
            continue

        candidates = (
            node.get("id"),
            node.get("data-date"),
            node.get("datetime"),
            node.get_text(" ", strip=True),
        )
        for candidate in candidates:
            parsed = parse_date_value(candidate, default_year)
            if parsed:
                return parsed

    for attribute in ("data-date", "datetime", "data-event-datetime"):
        parsed = parse_date_value(row.get(attribute), default_year)
        if parsed:
            return parsed

    return None


def find_company_cell(row: Tag) -> Tag | None:
    selectors = (
        ".earnCalCompanyName",
        '[class*="earnCalCompanyName"]',
        '[data-test="event-name"]',
        '[data-column="company"]',
        'td[class*="company"]',
    )

    for selector in selectors:
        node = row.select_one(selector)
        if node:
            return node

    link = row.select_one(
        'a[href*="/equities/"], '
        'a[href*="-earnings"], '
        'a[href*="/stocks/"]'
    )
    if link:
        return link.find_parent("td") or link.parent

    return None


def looks_like_ticker(value: str) -> bool:
    value = clean(value)
    return bool(
        re.fullmatch(r"[A-Z0-9.\-]{1,12}", value)
        or re.fullmatch(r"\d{5,6}", value)
    )


def extract_company(
    row: Tag,
    base_url: str,
) -> tuple[str | None, str | None, str]:
    cell = find_company_cell(row)
    if not cell:
        return None, None, base_url

    link = cell.select_one("a[href]") or row.select_one(
        'a[href*="/equities/"], '
        'a[href*="-earnings"], '
        'a[href*="/stocks/"]'
    )

    ticker = ""
    link_text = ""
    href = base_url

    if link:
        link_text = clean(link.get_text(" ", strip=True))
        href = urljoin(base_url, link.get("href") or "")
        if looks_like_ticker(link_text):
            ticker = link_text

    company = clean(
        cell.get("data-name")
        or cell.get("data-company-name")
        or row.get("data-name")
        or row.get("data-company-name")
        or cell.get_text(" ", strip=True)
    )

    # 셀 끝의 종목코드를 제거합니다.
    ticker_in_parentheses = re.search(
        r"\(\s*([A-Z0-9.\-]{1,12}|\d{5,6})\s*\)\s*$",
        company,
        re.IGNORECASE,
    )
    if ticker_in_parentheses and not ticker:
        ticker = ticker_in_parentheses.group(1)

    company = re.sub(
        r"\(\s*([A-Z0-9.\-]{1,12}|\d{5,6})\s*\)\s*$",
        "",
        company,
        flags=re.IGNORECASE,
    )

    if ticker:
        company = re.sub(
            rf"\b{re.escape(ticker)}\b\s*$",
            "",
            company,
            flags=re.IGNORECASE,
        )

    company = clean(company).strip("-–|")

    if not company or looks_like_ticker(company):
        if link_text and not looks_like_ticker(link_text):
            company = link_text

    if not company or company.lower() in {"company", "기업", "symbol", "종목"}:
        return None, ticker or None, href

    return company, ticker or None, href


def parse_calendar_html(
    html: str,
    base_url: str,
    requested_start: date,
    requested_end: date,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr")

    current_date: date | None = None
    all_header_dates: list[date] = []
    events: list[dict[str, Any]] = []
    company_rows = 0

    for row in rows:
        detected_date = row_date(row, requested_start.year)
        company_cell = find_company_cell(row)

        if detected_date:
            current_date = detected_date
            all_header_dates.append(detected_date)

        if not company_cell:
            continue

        company_rows += 1
        company, ticker, href = extract_company(row, base_url)
        if not company or not current_date:
            continue

        # 응답이 요청 구간을 무시하고 현재/다음 주를 돌려준 경우,
        # 범위 밖 이벤트는 저장하지 않습니다.
        if not (requested_start <= current_date <= requested_end):
            continue

        event_key = ticker or re.sub(
            r"[^0-9A-Za-z가-힣]+",
            "-",
            company,
        ).strip("-")

        events.append(
            {
                "id": f"{current_date.isoformat()}-{event_key}",
                "title": f"{company} 실적",
                "company": company,
                "ticker": ticker,
                "start": current_date.isoformat(),
                "allDay": True,
                "url": href,
                "country": "한국",
                "importance": "높음",
                "source": "Investing.com",
            }
        )

    unique_dates = sorted(set(all_header_dates))
    overlapping_dates = [
        item
        for item in unique_dates
        if requested_start <= item <= requested_end
    ]

    # 날짜 구분행이 존재하지만 요청 기간과 하나도 겹치지 않으면
    # 서버가 사용자 지정 기간을 무시한 응답입니다.
    ignored_range = bool(unique_dates) and not overlapping_dates

    return {
        "events": events,
        "row_count": len(rows),
        "company_rows": company_rows,
        "header_dates": [item.isoformat() for item in unique_dates],
        "overlapping_dates": [item.isoformat() for item in overlapping_dates],
        "ignored_range": ignored_range,
        "no_results": company_rows == 0,
    }


def response_html(response: Any) -> tuple[str, dict[str, Any]]:
    metadata: dict[str, Any] = {}

    try:
        payload = response.json()
        if isinstance(payload, dict):
            metadata = payload
            html = payload.get("data")
            if isinstance(html, str):
                return html, metadata
    except Exception:
        pass

    text = response.text or ""
    if "<tr" in text.lower() or "<table" in text.lower():
        return text, metadata

    raise RuntimeError(
        "캘린더 HTML을 찾지 못했습니다: "
        + clean(text[:250])
    )


def open_landing_page(
    session: requests.Session,
    host: str,
) -> str:
    candidates = (
        f"{host}/earnings-calendar/",
        f"{host}/earningscalendar/",
    )

    last_url = candidates[0]
    for candidate in candidates:
        try:
            response = session.get(
                candidate,
                impersonate="chrome",
                timeout=30,
                allow_redirects=True,
                headers={
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
            )
            last_url = str(response.url)
            if response.status_code < 400:
                return last_url
        except Exception:
            continue

    return last_url


def request_window_once(
    session: requests.Session,
    host: str,
    landing_url: str,
    window_start: date,
    window_end: date,
    date_format: str,
    diagnostics: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    endpoint = f"{host}/earnings-calendar/Service/getCalendarFilteredData"

    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": host,
        "Referer": landing_url,
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_pages: set[str] = set()
    accepted_response = False

    for page_number in range(MAX_PAGES_PER_WINDOW):
        # list로 전달해야 country[]와 importance[]가 정확히 폼 필드로 전송됩니다.
        form_data = [
            ("dateFrom", format_request_date(window_start, date_format)),
            ("dateTo", format_request_date(window_end, date_format)),
            ("currentTab", "custom"),
            ("submitFilters", "1"),
            ("limit_from", str(page_number)),
            ("country[]", KOREA_COUNTRY_ID),
            ("importance[]", HIGH_IMPORTANCE_ID),
        ]

        response = session.post(
            endpoint,
            data=form_data,
            headers=headers,
            impersonate="chrome",
            timeout=45,
            allow_redirects=True,
        )

        record: dict[str, Any] = {
            "host": host,
            "window": [
                window_start.isoformat(),
                window_end.isoformat(),
            ],
            "date_format": date_format,
            "page": page_number,
            "status_code": response.status_code,
            "response_bytes": len(response.content or b""),
        }

        if response.status_code >= 400:
            record["accepted"] = False
            record["reason"] = f"HTTP {response.status_code}"
            diagnostics.append(record)
            raise RuntimeError(record["reason"])

        html, metadata = response_html(response)
        fingerprint = hashlib.sha1(
            html.encode("utf-8", errors="ignore")
        ).hexdigest()

        if fingerprint in seen_pages:
            record["accepted"] = accepted_response
            record["reason"] = "duplicate_page"
            diagnostics.append(record)
            break
        seen_pages.add(fingerprint)

        parsed = parse_calendar_html(
            html,
            host,
            window_start,
            window_end,
        )

        record.update(
            {
                "row_count": parsed["row_count"],
                "company_rows": parsed["company_rows"],
                "header_dates": parsed["header_dates"][:8],
                "overlapping_dates": parsed["overlapping_dates"][:8],
                "parsed_events": len(parsed["events"]),
            }
        )

        if parsed["ignored_range"]:
            record["accepted"] = False
            record["reason"] = "server_ignored_requested_range"
            diagnostics.append(record)
            return [], False

        # 날짜가 겹치는 정상 응답 또는 명시적인 빈 결과만 허용합니다.
        response_is_valid = bool(parsed["overlapping_dates"]) or parsed["no_results"]

        if not response_is_valid:
            record["accepted"] = False
            record["reason"] = "unverifiable_response_range"
            diagnostics.append(record)
            return [], False

        accepted_response = True
        record["accepted"] = True
        record["reason"] = "ok"
        diagnostics.append(record)

        for event in parsed["events"]:
            if event["id"] not in seen_ids:
                seen_ids.add(event["id"])
                collected.append(event)

        bind_scroll = metadata.get("bind_scroll_handler")
        no_more_pages = (
            bind_scroll is False
            or str(bind_scroll).lower() == "false"
        )

        if no_more_pages or parsed["no_results"]:
            break

        # 다음 페이지가 실제로 없는데 같은 HTML이 돌아오는 경우는
        # 다음 반복에서 fingerprint로 중단됩니다.

    return collected, accepted_response


def fetch_window(
    window_start: date,
    window_end: date,
    diagnostics: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    errors: list[str] = []

    # 호스트와 날짜 형식을 바꿔가며 시도하되,
    # 요청 구간과 응답 날짜가 겹친 경우만 성공으로 인정합니다.
    for host in HOSTS:
        session = requests.Session()
        try:
            landing_url = open_landing_page(session, host)

            for date_format in DATE_FORMATS:
                try:
                    events, accepted = request_window_once(
                        session,
                        host,
                        landing_url,
                        window_start,
                        window_end,
                        date_format,
                        diagnostics,
                    )
                    if accepted:
                        return events, f"{host}:{date_format}"
                except Exception as exc:
                    errors.append(
                        f"{host}/{date_format}: "
                        f"{type(exc).__name__}: {exc}"
                    )
        finally:
            session.close()

    raise RuntimeError(" | ".join(errors[-6:]) or "검증 가능한 응답 없음")


def read_previous_events(
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    if not EVENTS_PATH.exists():
        return []

    try:
        previous = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    retained: list[dict[str, Any]] = []
    for event in previous:
        try:
            event_date = date.fromisoformat(str(event["start"])[:10])
        except (KeyError, TypeError, ValueError):
            continue

        if start <= event_date <= end:
            retained.append(event)

    return retained


def normalize_company_key(value: Any) -> str:
    text = clean(value).upper()
    for token in (
        "주식회사",
        "(주)",
        "㈜",
        "CO.,LTD.",
        "CO., LTD.",
        "CO LTD",
        "CORPORATION",
        "CORP.",
        "INC.",
    ):
        text = text.replace(token, "")
    return re.sub(r"[^0-9A-Z가-힣]", "", text)


def safe_int(value: Any) -> int:
    if value is None:
        return 0

    try:
        if value != value:
            return 0
    except Exception:
        pass

    text = clean(value).replace(",", "")
    if not text:
        return 0

    try:
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return 0


def find_column(
    columns: list[str],
    candidates: tuple[str, ...],
) -> str | None:
    normalized = {
        re.sub(r"[^A-Z0-9가-힣]", "", column.upper()): column
        for column in columns
    }

    for candidate in candidates:
        key = re.sub(r"[^A-Z0-9가-힣]", "", candidate.upper())
        if key in normalized:
            return normalized[key]

    return None


def read_market_cap_cache() -> tuple[dict[str, int], dict[str, int]] | None:
    if not MARKET_CAP_CACHE_PATH.exists():
        return None

    try:
        cache = json.loads(
            MARKET_CAP_CACHE_PATH.read_text(encoding="utf-8")
        )
        updated_at = datetime.fromisoformat(cache["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=SEOUL)

        if datetime.now(SEOUL) - updated_at > timedelta(hours=24):
            return None

        return (
            {key: safe_int(value) for key, value in cache["by_ticker"].items()},
            {key: safe_int(value) for key, value in cache["by_name"].items()},
        )
    except Exception:
        return None


def load_market_caps() -> tuple[dict[str, int], dict[str, int], str]:
    cached = read_market_cap_cache()
    if cached:
        return cached[0], cached[1], "KRX-MARCAP cache"

    errors: list[str] = []

    for listing_name in ("KRX-MARCAP", "KRX"):
        try:
            frame = fdr.StockListing(listing_name)
            columns = [str(column) for column in frame.columns]

            symbol_column = find_column(
                columns,
                ("Code", "Symbol", "Ticker", "종목코드", "단축코드"),
            )
            name_column = find_column(
                columns,
                ("Name", "종목명", "한글종목명"),
            )
            market_cap_column = find_column(
                columns,
                ("Marcap", "MarketCap", "MarCap", "시가총액"),
            )
            close_column = find_column(
                columns,
                ("Close", "종가", "현재가"),
            )
            stocks_column = find_column(
                columns,
                ("Stocks", "Shares", "상장주식수"),
            )

            if not name_column:
                raise RuntimeError(f"종목명 컬럼 없음: {columns}")

            by_ticker: dict[str, int] = {}
            by_name: dict[str, int] = {}

            for _, row in frame.iterrows():
                company_name = clean(row.get(name_column))
                if not company_name:
                    continue

                market_cap = (
                    safe_int(row.get(market_cap_column))
                    if market_cap_column
                    else 0
                )

                if market_cap <= 0 and close_column and stocks_column:
                    market_cap = (
                        safe_int(row.get(close_column))
                        * safe_int(row.get(stocks_column))
                    )

                if market_cap <= 0:
                    continue

                if symbol_column:
                    ticker_digits = re.sub(
                        r"\D",
                        "",
                        clean(row.get(symbol_column)),
                    )
                    if ticker_digits:
                        by_ticker[ticker_digits.zfill(6)] = market_cap

                name_key = normalize_company_key(company_name)
                if name_key:
                    by_name[name_key] = max(
                        by_name.get(name_key, 0),
                        market_cap,
                    )

            if not by_ticker and not by_name:
                raise RuntimeError("유효한 시가총액 데이터 없음")

            MARKET_CAP_CACHE_PATH.write_text(
                json.dumps(
                    {
                        "updated_at": datetime.now(SEOUL).isoformat(),
                        "by_ticker": by_ticker,
                        "by_name": by_name,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            return by_ticker, by_name, listing_name
        except Exception as exc:
            errors.append(
                f"{listing_name}: {type(exc).__name__}: {exc}"
            )

    raise RuntimeError(" | ".join(errors))


def attach_market_caps(
    events: list[dict[str, Any]],
) -> tuple[int, str | None, str | None]:
    try:
        by_ticker, by_name, source = load_market_caps()
    except Exception as exc:
        for event in events:
            event["marketCap"] = safe_int(event.get("marketCap"))
        matched = sum(
            1 for event in events if event["marketCap"] > 0
        )
        return matched, None, f"{type(exc).__name__}: {exc}"

    matched = 0

    for event in events:
        ticker_digits = re.sub(
            r"\D",
            "",
            clean(event.get("ticker")),
        )
        ticker = ticker_digits.zfill(6) if ticker_digits else ""

        market_cap = by_ticker.get(ticker, 0) if ticker else 0

        if market_cap <= 0:
            market_cap = by_name.get(
                normalize_company_key(event.get("company")),
                0,
            )

        if market_cap <= 0:
            market_cap = safe_int(event.get("marketCap"))

        event["marketCap"] = market_cap
        if market_cap > 0:
            matched += 1

    return matched, source, None


def escape_ics(value: Any) -> str:
    return (
        clean(value)
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def write_ics(events: list[dict[str, Any]]) -> None:
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//GitHub//Korea Earnings Calendar//KO",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:한국 기업 실적",
        "X-WR-TIMEZONE:Asia/Seoul",
    ]

    for event in events:
        event_day = date.fromisoformat(event["start"])
        next_day = event_day + timedelta(days=1)

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{escape_ics(event['id'])}@github",
                f"DTSTAMP:{now_utc}",
                f"DTSTART;VALUE=DATE:{event_day.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{next_day.strftime('%Y%m%d')}",
                f"SUMMARY:{escape_ics(event['title'])}",
                f"URL:{event.get('url', '')}",
                "DESCRIPTION:Investing.com / 국가: 한국 / 중요도: 높음",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    ICS_PATH.write_text(
        "\r\n".join(lines) + "\r\n",
        encoding="utf-8",
    )


def main() -> None:
    started_at = time.monotonic()

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    start, end = rolling_range()
    windows = make_windows(start, end)

    diagnostics: list[dict[str, Any]] = []
    window_results: list[dict[str, Any]] = []
    collected: list[dict[str, Any]] = []
    failed_windows: list[dict[str, Any]] = []

    for window_start, window_end in windows:
        try:
            window_events, source = fetch_window(
                window_start,
                window_end,
                diagnostics,
            )

            collected.extend(window_events)
            window_results.append(
                {
                    "from": window_start.isoformat(),
                    "to": window_end.isoformat(),
                    "ok": True,
                    "event_count": len(window_events),
                    "source": source,
                }
            )
        except Exception as exc:
            failure = {
                "from": window_start.isoformat(),
                "to": window_end.isoformat(),
                "ok": False,
                "event_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
            window_results.append(failure)
            failed_windows.append(failure)

    unique = {
        (event["start"], event["company"]): event
        for event in collected
    }
    fresh_events = list(unique.values())

    previous_events = read_previous_events(start, end)
    previous_by_key = {
        (event.get("start"), event.get("company")): event
        for event in previous_events
    }

    # 실패한 구간은 기존 정상 데이터로만 보완합니다.
    failed_ranges = [
        (
            date.fromisoformat(item["from"]),
            date.fromisoformat(item["to"]),
        )
        for item in failed_windows
    ]

    for key, event in previous_by_key.items():
        try:
            event_date = date.fromisoformat(str(event["start"])[:10])
        except (KeyError, TypeError, ValueError):
            continue

        if any(
            range_start <= event_date <= range_end
            for range_start, range_end in failed_ranges
        ):
            unique.setdefault(key, event)

    events = list(unique.values())

    for event in events:
        company = clean(event.get("company"))
        if company:
            event["title"] = f"{company} 실적"

    market_cap_matched, market_cap_source, market_cap_error = (
        attach_market_caps(events)
    )

    events.sort(
        key=lambda item: (
            str(item.get("start", "")),
            -safe_int(item.get("marketCap")),
            str(item.get("company", "")),
        )
    )

    EVENTS_PATH.write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_ics(events)

    events_by_month = dict(
        sorted(
            Counter(
                str(event["start"])[:7]
                for event in events
            ).items()
        )
    )

    all_windows_ok = not failed_windows
    status = {
        "version": VERSION,
        "ok": all_windows_ok,
        "used_cache_for_failed_windows": bool(failed_windows),
        "updated_at": datetime.now(SEOUL).isoformat(timespec="seconds"),
        "timezone": "Asia/Seoul",
        "range": {
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
        "window_days": WINDOW_DAYS,
        "window_count": len(windows),
        "successful_window_count": len(windows) - len(failed_windows),
        "failed_window_count": len(failed_windows),
        "event_count": len(events),
        "fresh_event_count": len(fresh_events),
        "first_event_date": events[0]["start"] if events else None,
        "last_event_date": events[-1]["start"] if events else None,
        "events_by_month": events_by_month,
        "market_cap_source": market_cap_source,
        "market_cap_matched": market_cap_matched,
        "market_cap_unmatched": max(0, len(events) - market_cap_matched),
        "market_cap_error": market_cap_error,
        "elapsed_seconds": round(time.monotonic() - started_at, 2),
        "message": (
            "미래 3개월 전체 구간 갱신 완료"
            if all_windows_ok
            else "일부 구간 수집 실패: 해당 구간의 기존 데이터만 유지"
        ),
        "windows": window_results,
        "diagnostics": diagnostics[-80:],
    }

    STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"{VERSION} | {start} ~ {end} | "
        f"{len(events)}건 | "
        f"구간 {len(windows) - len(failed_windows)}/{len(windows)} 성공 | "
        f"{status['elapsed_seconds']}초"
    )

    # Investing.com은 아직 등록되지 않은 먼 미래 구간에 대해
    # 요청 범위를 무시하거나 검증 가능한 빈 응답을 주지 않을 수 있습니다.
    # 이 경우 성공한 구간과 기존 캐시를 그대로 저장하고, status.json에
    # 실패 구간을 기록합니다. 따라서 GitHub Actions 전체를 실패시키지 않습니다.
    if not all_windows_ok:
        print(
            f"주의: {len(failed_windows)}개 미래 구간은 검증하지 못했습니다. "
            "성공한 구간과 기존 데이터는 정상 저장합니다."
        )


if __name__ == "__main__":
    main()
