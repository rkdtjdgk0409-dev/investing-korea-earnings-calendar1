from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag
from curl_cffi import requests
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
import FinanceDataReader as fdr


# 서울 현재 날짜부터 정확히 미래 3개월까지 조회합니다.
SEOUL = ZoneInfo("Asia/Seoul")
KOREA_COUNTRY_ID = "11"
HIGH_IMPORTANCE_ID = "3"
MAX_PAGES = 20

HOSTS = (
    "https://kr.investing.com",
    "https://www.investing.com",
)

SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parents[1] if SCRIPT_PATH.parent.name == "scripts" else SCRIPT_PATH.parent
DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
EVENTS_PATH = DATA_DIR / "events.json"
STATUS_PATH = DATA_DIR / "status.json"
ICS_PATH = DOCS_DIR / "earnings.ics"


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def rolling_range() -> tuple[date, date]:
    start = datetime.now(SEOUL).date()
    end = start + relativedelta(months=3)
    return start, end


def parse_date_value(value: str | None, default_year: int) -> date | None:
    text = clean(value)
    if not text:
        return None

    # theDay169... 형태의 유닉스 타임스탬프
    timestamp_match = re.search(r"(?:theDay)?(\d{10,13})", text)
    if timestamp_match:
        try:
            timestamp = int(timestamp_match.group(1))
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, timezone.utc).date()
            if 2000 <= parsed.year <= 2100:
                return parsed
        except (OverflowError, OSError, ValueError):
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


def find_date_header(row: Tag, default_year: int) -> date | None:
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

        for candidate in (
            node.get("id"),
            node.get("data-date"),
            node.get("datetime"),
            node.get_text(" ", strip=True),
        ):
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
    compact = clean(value)
    return bool(
        re.fullmatch(r"[A-Z0-9.\-]{1,12}", compact)
        or re.fullmatch(r"\d{5,6}", compact)
    )


def extract_company(row: Tag, base_url: str) -> tuple[str | None, str | None, str]:
    cell = find_company_cell(row)
    if not cell:
        return None, None, base_url

    link = cell.select_one("a[href]") or row.select_one(
        'a[href*="/equities/"], a[href*="-earnings"], a[href*="/stocks/"]'
    )

    ticker = ""
    link_text = ""
    href = base_url

    if link:
        link_text = clean(link.get_text(" ", strip=True))
        if looks_like_ticker(link_text):
            ticker = link_text
        href = urljoin(base_url, link.get("href") or "")

    # data 속성에 기업명이 있는 경우 우선 사용합니다.
    data_name = clean(
        cell.get("data-name")
        or cell.get("data-company-name")
        or row.get("data-name")
        or row.get("data-company-name")
    )

    company = data_name or clean(cell.get_text(" ", strip=True))

    # "삼성전자 (005930)" 또는 "Samsung Electronics (005930)"에서 종목코드를 제거합니다.
    if ticker:
        company = re.sub(
            rf"\(\s*{re.escape(ticker)}\s*\)",
            " ",
            company,
            flags=re.IGNORECASE,
        )
        company = re.sub(
            rf"\b{re.escape(ticker)}\b\s*$",
            " ",
            company,
            flags=re.IGNORECASE,
        )

    company = re.sub(r"\(\s*[A-Z0-9.\-]{1,12}\s*\)\s*$", " ", company)
    company = re.sub(r"\(\s*\d{5,6}\s*\)\s*$", " ", company)
    company = clean(company).strip("-–|")

    # 링크 자체가 기업명이고 별도 셀 텍스트가 없는 신규 레이아웃 대응
    if not company or looks_like_ticker(company):
        if link_text and not looks_like_ticker(link_text):
            company = link_text

    # 셀 안의 span에 실제 기업명이 들어 있는 경우
    if not company or looks_like_ticker(company):
        for span in cell.select("span"):
            candidate = clean(span.get_text(" ", strip=True))
            if candidate and not looks_like_ticker(candidate):
                company = candidate
                break

    if not company or company.lower() in {"company", "기업", "symbol", "종목"}:
        return None, ticker or None, href

    return company, ticker or None, href


def local_importance(row: Tag) -> int:
    for node in (row, *row.find_all(True)):
        for attribute in (
            "data-importance",
            "data-importance-level",
            "data-impact",
            "data-img_key",
            "title",
            "aria-label",
        ):
            value = clean(node.get(attribute)).lower()
            if not value:
                continue
            if value in {"3", "high", "높음"} or "bull3" in value:
                return 3
            if value in {"2", "medium", "보통"} or "bull2" in value:
                return max(2, 0)
            if value in {"1", "low", "낮음"} or "bull1" in value:
                return max(1, 0)

    active_bulls = 0
    for node in row.select('[class*="Bull"], [class*="bull"]'):
        classes = " ".join(node.get("class", [])).lower()
        style = clean(node.get("style")).lower()
        if (
            "gray" not in classes
            and "muted" not in classes
            and "display:none" not in style.replace(" ", "")
            and "visibility:hidden" not in style.replace(" ", "")
        ):
            active_bulls += 1

    return min(active_bulls, 3)


def local_is_korea(row: Tag) -> bool:
    parts: list[str] = [clean(row.get_text(" ", strip=True))]
    for node in (row, *row.find_all(True)):
        for key, value in node.attrs.items():
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
            else:
                parts.append(str(value))

    blob = " ".join(parts).lower()
    markers = (
        "south korea",
        "south_korea",
        "south-korea",
        "대한민국",
        "한국",
        "country_11",
        "country-11",
        'data-country="11"',
        "ceflags south_korea",
    )
    return any(marker in blob for marker in markers)


def parse_calendar_html(
    html: str,
    base_url: str,
    start: date,
    end: date,
    *,
    require_local_high: bool,
    require_local_korea: bool,
) -> tuple[list[dict[str, Any]], int]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr")
    current_date: date | None = None
    events: list[dict[str, Any]] = []

    for row in rows:
        header_date = find_date_header(row, start.year)
        company_cell = find_company_cell(row)

        if header_date and not company_cell:
            current_date = header_date
            continue

        company, ticker, href = extract_company(row, base_url)
        if not company:
            continue

        event_date = header_date or current_date
        if not event_date or event_date < start or event_date > end:
            continue

        if require_local_high and local_importance(row) < 3:
            continue
        if require_local_korea and not local_is_korea(row):
            continue

        event_key = ticker or re.sub(r"[^0-9A-Za-z가-힣]+", "-", company).strip("-")
        events.append(
            {
                "id": f"{event_date.isoformat()}-{event_key}",
                "title": f"{company} 실적",
                "company": company,
                "ticker": ticker,
                "start": event_date.isoformat(),
                "allDay": True,
                "url": href,
                "country": "한국",
                "importance": "높음",
                "source": "Investing.com",
            }
        )

    return events, len(rows)


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

    preview = clean(text[:300])
    raise RuntimeError(
        f"Investing.com 응답에서 캘린더 HTML을 찾지 못했습니다: {preview}"
    )


def fetch_mode(
    session: requests.Session,
    host: str,
    start: date,
    end: date,
    *,
    mode: str,
    attempts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    landing_urls = (
        f"{host}/earningscalendar/",
        f"{host}/earnings-calendar/",
    )
    endpoint = f"{host}/earnings-calendar/Service/getCalendarFilteredData"

    landing_url = landing_urls[0]
    for candidate in landing_urls:
        try:
            landing_response = session.get(
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
            landing_url = str(landing_response.url)
            if landing_response.status_code < 400:
                break
        except Exception:
            continue

    payload: dict[str, Any] = {
        "dateFrom": start.isoformat(),
        "dateTo": end.isoformat(),
        "currentTab": "custom",
        "submitFilters": "1",
        "limit_from": "0",
    }

    require_local_high = False
    require_local_korea = False

    if mode == "server_both":
        payload["country[]"] = KOREA_COUNTRY_ID
        payload["importance[]"] = HIGH_IMPORTANCE_ID
    elif mode == "server_country":
        payload["country[]"] = KOREA_COUNTRY_ID
        require_local_high = True
    elif mode == "server_high":
        payload["importance[]"] = HIGH_IMPORTANCE_ID
        require_local_korea = True
    else:
        raise ValueError(f"지원하지 않는 모드: {mode}")

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
    seen_html: set[str] = set()
    seen_ids: set[str] = set()

    for page_number in range(MAX_PAGES):
        payload["limit_from"] = str(page_number)

        response = session.post(
            endpoint,
            data=payload,
            headers=headers,
            impersonate="chrome",
            timeout=45,
            allow_redirects=True,
        )

        attempt: dict[str, Any] = {
            "host": host,
            "mode": mode,
            "page": page_number,
            "status_code": response.status_code,
            "response_bytes": len(response.content or b""),
        }

        if response.status_code >= 400:
            attempt["error"] = f"HTTP {response.status_code}"
            attempts.append(attempt)
            raise RuntimeError(f"{host} HTTP {response.status_code}")

        html, metadata = response_html(response)
        fingerprint = hashlib.sha1(html.encode("utf-8", errors="ignore")).hexdigest()

        if fingerprint in seen_html:
            attempt["duplicate_page"] = True
            attempts.append(attempt)
            break
        seen_html.add(fingerprint)

        page_events, row_count = parse_calendar_html(
            html,
            host,
            start,
            end,
            require_local_high=require_local_high,
            require_local_korea=require_local_korea,
        )

        new_count = 0
        for event in page_events:
            event_id = event["id"]
            if event_id not in seen_ids:
                seen_ids.add(event_id)
                collected.append(event)
                new_count += 1

        attempt.update(
            {
                "table_rows": row_count,
                "parsed_events": len(page_events),
                "new_events": new_count,
            }
        )
        attempts.append(attempt)

        bind_scroll = metadata.get("bind_scroll_handler")
        no_more_pages = bind_scroll is False or str(bind_scroll).lower() == "false"

        if no_more_pages or row_count == 0 or (page_number > 0 and new_count == 0):
            break

    return collected


def fetch_events(
    start: date,
    end: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], str | None]:
    attempts: list[dict[str, Any]] = []
    errors: list[str] = []

    # 정상 경로는 첫 번째 모드 하나로 끝납니다.
    # 필터 필드가 변경됐을 때만 두 개의 보조 모드를 시도합니다.
    modes = ("server_both", "server_country", "server_high")

    for host in HOSTS:
        session = requests.Session()
        try:
            for mode in modes:
                try:
                    events = fetch_mode(
                        session,
                        host,
                        start,
                        end,
                        mode=mode,
                        attempts=attempts,
                    )
                    if events:
                        unique = {
                            (event["start"], event["company"]): event
                            for event in events
                        }
                        ordered = sorted(
                            unique.values(),
                            key=lambda item: (item["start"], item["company"]),
                        )
                        return ordered, attempts, errors, f"{host}:{mode}"
                except Exception as exc:
                    errors.append(
                        f"{host} / {mode}: {type(exc).__name__}: {exc}"
                    )
        finally:
            session.close()

    return [], attempts, errors, None


def read_previous_events(start: date, end: date) -> list[dict[str, Any]]:
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
            title = clean(event.get("title"))
            company = clean(event.get("company"))
            if not title and company:
                event["title"] = f"{company} 실적"
            retained.append(event)

    return retained



def normalize_company_key(value: str | None) -> str:
    """기업명 비교용 키를 만듭니다."""
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
        if value != value:  # NaN
            return 0
    except Exception:
        pass

    text = clean(str(value)).replace(",", "")
    if not text:
        return 0

    try:
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return 0


def find_dataframe_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    direct = {str(column): str(column) for column in columns}
    for candidate in candidates:
        if candidate in direct:
            return candidate

    normalized = {
        re.sub(r"[^A-Z0-9가-힣]", "", str(column).upper()): str(column)
        for column in columns
    }
    for candidate in candidates:
        key = re.sub(r"[^A-Z0-9가-힣]", "", candidate.upper())
        if key in normalized:
            return normalized[key]

    return None


def load_market_cap_maps() -> tuple[dict[str, int], dict[str, int], str]:
    """
    KRX 전체 종목 시가총액을 한 번에 받아옵니다.
    종목별 개별 요청을 하지 않기 때문에 실행 시간이 크게 늘어나지 않습니다.
    """
    errors: list[str] = []

    for listing_name in ("KRX-MARCAP", "KRX"):
        try:
            dataframe = fdr.StockListing(listing_name)
            columns = [str(column) for column in dataframe.columns]

            symbol_column = find_dataframe_column(
                columns,
                ("Code", "Symbol", "Ticker", "종목코드", "단축코드"),
            )
            name_column = find_dataframe_column(
                columns,
                ("Name", "종목명", "한글종목명"),
            )
            market_cap_column = find_dataframe_column(
                columns,
                ("Marcap", "MarketCap", "MarCap", "시가총액"),
            )
            close_column = find_dataframe_column(
                columns,
                ("Close", "종가", "현재가"),
            )
            stocks_column = find_dataframe_column(
                columns,
                ("Stocks", "Shares", "상장주식수"),
            )

            if not name_column:
                raise RuntimeError(f"종목명 컬럼을 찾지 못했습니다: {columns}")

            by_ticker: dict[str, int] = {}
            by_name: dict[str, int] = {}

            for _, row in dataframe.iterrows():
                company_name = clean(str(row.get(name_column, "")))
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
                    ticker_raw = clean(str(row.get(symbol_column, "")))
                    ticker_raw = re.sub(r"\.0$", "", ticker_raw)
                    ticker_digits = re.sub(r"\D", "", ticker_raw)
                    if ticker_digits:
                        by_ticker[ticker_digits.zfill(6)] = market_cap

                name_key = normalize_company_key(company_name)
                if name_key:
                    previous = by_name.get(name_key, 0)
                    by_name[name_key] = max(previous, market_cap)

            if by_ticker or by_name:
                return by_ticker, by_name, listing_name

            raise RuntimeError("유효한 시가총액 데이터가 없습니다.")
        except Exception as exc:
            errors.append(f"{listing_name}: {type(exc).__name__}: {exc}")

    raise RuntimeError(" / ".join(errors))


def attach_market_caps(
    events: list[dict[str, Any]],
) -> tuple[int, str | None, str | None]:
    """
    일정에 marketCap 값을 붙입니다.
    종목코드 매칭을 우선하고, 코드가 없으면 기업명으로 다시 매칭합니다.
    """
    try:
        by_ticker, by_name, source = load_market_cap_maps()
    except Exception as exc:
        # KRX 조회가 일시적으로 실패하면 기존 저장값으로 정렬합니다.
        matched = sum(1 for event in events if safe_int(event.get("marketCap")) > 0)
        for event in events:
            event["marketCap"] = safe_int(event.get("marketCap"))
        return matched, None, f"{type(exc).__name__}: {exc}"

    matched = 0

    for event in events:
        ticker_raw = clean(str(event.get("ticker") or ""))
        ticker_digits = re.sub(r"\D", "", ticker_raw)
        ticker = ticker_digits.zfill(6) if ticker_digits else ""

        market_cap = by_ticker.get(ticker, 0) if ticker else 0

        if market_cap <= 0:
            company_key = normalize_company_key(event.get("company"))
            market_cap = by_name.get(company_key, 0)

        # 매칭 실패 시 이전 실행에서 저장한 시가총액을 보조값으로 유지합니다.
        if market_cap <= 0:
            market_cap = safe_int(event.get("marketCap"))

        event["marketCap"] = market_cap

        if market_cap > 0:
            matched += 1

    return matched, source, None

def escape_ics(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
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
    ICS_PATH.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def main() -> None:
    started = time.monotonic()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    start, end = rolling_range()
    fresh_events, attempts, errors, source_mode = fetch_events(start, end)

    used_cache = False
    if fresh_events:
        events = fresh_events
        ok = True
        message = "한국·중요도 높음 실적 캘린더 갱신 완료"
    else:
        events = read_previous_events(start, end)
        used_cache = bool(events)
        ok = False
        message = (
            "새 데이터를 얻지 못해 현재 3개월 범위에 해당하는 기존 데이터를 유지했습니다."
            if used_cache
            else "한국·중요도 높음 실적 데이터를 찾지 못했습니다."
        )

    # 제목이 비어 있으면 화면에서 반드시 '기업명 실적'으로 복구합니다.
    for event in events:
        company = clean(event.get("company"))
        if company:
            event["title"] = f"{company} 실적"

    market_cap_matched, market_cap_source, market_cap_error = attach_market_caps(events)

    # 같은 날짜 안에서는 시가총액이 큰 기업부터 정렬합니다.
    # 시가총액을 확인하지 못한 기업은 해당 날짜의 맨 아래로 갑니다.
    events = sorted(
        events,
        key=lambda item: (
            str(item.get("start", "")),
            -safe_int(item.get("marketCap")),
            str(item.get("company", "")),
        ),
    )

    EVENTS_PATH.write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_ics(events)

    status = {
        "ok": ok,
        "used_cache": used_cache,
        "updated_at": datetime.now(SEOUL).isoformat(timespec="seconds"),
        "timezone": "Asia/Seoul",
        "range": {
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
        "event_count": len(events),
        "source_mode": source_mode,
        "market_cap_source": market_cap_source,
        "market_cap_matched": market_cap_matched,
        "market_cap_unmatched": max(0, len(events) - market_cap_matched),
        "market_cap_error": market_cap_error,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "message": message,
        "errors": errors[-10:],
        "attempts": attempts[-30:],
    }

    STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"{message} | {start} ~ {end} | "
        f"{len(events)}건 | {status['elapsed_seconds']}초"
    )


if __name__ == "__main__":
    main()
