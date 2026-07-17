from __future__ import annotations

import calendar
import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


VERSION = "v6-tradingview-korea-next-earnings"
SOURCE_NAME = "TradingView Korea Scanner"
SOURCE_URL = "https://scanner.tradingview.com/korea/scan"
SEOUL = ZoneInfo("Asia/Seoul")

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
EVENTS_PATH = DATA_DIR / "events.json"
STATUS_PATH = DATA_DIR / "status.json"
ICS_PATH = DOCS_DIR / "earnings.ics"

COLUMNS = (
    "name",
    "description",
    "exchange",
    "market_cap_basic",
    "earnings_release_next_date",
    "earnings_release_time",
)


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def rolling_range() -> tuple[date, date]:
    start = datetime.now(SEOUL).date()
    return start, add_months(start, 3)


def epoch_at_start(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=SEOUL).timestamp())


def fetch_scanner(start: date, end: date) -> dict[str, Any]:
    body = {
        "filter": [
            {
                "left": "earnings_release_next_date",
                "operation": "in_range",
                "right": [
                    epoch_at_start(start),
                    epoch_at_start(end + timedelta(days=1)),
                ],
            }
        ],
        "options": {"lang": "ko"},
        "markets": ["korea"],
        "symbols": {
            "query": {"types": ["stock"]},
            "tickers": [],
        },
        "columns": list(COLUMNS),
        "sort": {
            "sortBy": "earnings_release_next_date",
            "sortOrder": "asc",
        },
        "range": [0, 2000],
    }

    request = Request(
        SOURCE_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
        },
    )

    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("한국 종목 스캐너 응답 형식이 올바르지 않습니다.")

    return payload


def clean_company(value: Any) -> str:
    company = clean(value)
    company = re.sub(r"^\(주\)\s*", "", company)
    company = re.sub(r"\s*보통주$", "", company)
    company = re.sub(r"\s*Common Stock$", "", company, flags=re.IGNORECASE)
    return clean(company) or "기업"


def release_label(value: Any) -> str:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return "미정"

    return {-1: "장전", 0: "미정", 1: "장후"}.get(code, "미정")


def parse_events(
    payload: dict[str, Any],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    events_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for row in payload.get("data", []):
        if not isinstance(row, dict):
            continue

        values = row.get("d")
        if not isinstance(values, list) or len(values) < len(COLUMNS):
            continue

        ticker = clean(values[0])

        # 한국 보통주·리츠 등 일반 상장사 종목코드는 숫자 6자리입니다.
        # ETF·ETN·우선주성 알파벳 코드는 제외합니다.
        if not re.fullmatch(r"\d{6}", ticker):
            continue

        try:
            raw_epoch = float(values[4])
            if raw_epoch > 10_000_000_000:
                raw_epoch /= 1000
            release_at = datetime.fromtimestamp(
                raw_epoch,
                timezone.utc,
            ).astimezone(SEOUL)
        except (TypeError, ValueError, OSError, OverflowError):
            continue

        event_date = release_at.date()
        if not (start <= event_date <= end):
            continue

        company = clean_company(values[1])
        market_cap = 0
        try:
            market_cap = max(0, int(float(values[3] or 0)))
        except (TypeError, ValueError, OverflowError):
            pass

        event = {
            "id": f"{event_date.isoformat()}-{ticker}",
            "title": f"{company} 실적",
            "company": company,
            "ticker": ticker,
            "start": event_date.isoformat(),
            "allDay": True,
            "url": f"https://kr.tradingview.com/symbols/KRX-{ticker}/",
            "country": "한국",
            "source": SOURCE_NAME,
            "marketCap": market_cap,
            "releaseSession": release_label(values[5]),
        }

        key = (event["start"], company)
        previous = events_by_key.get(key)
        if previous is None or event["marketCap"] > previous["marketCap"]:
            events_by_key[key] = event

    events = list(events_by_key.values())
    events.sort(
        key=lambda item: (
            item["start"],
            -int(item.get("marketCap") or 0),
            item["company"],
        )
    )
    return events


def read_previous(start: date, end: date) -> list[dict[str, Any]]:
    try:
        values = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

    retained = []
    for event in values if isinstance(values, list) else []:
        try:
            event_date = date.fromisoformat(str(event["start"])[:10])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= event_date <= end:
            retained.append(event)
    return retained


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
                f"DESCRIPTION:{SOURCE_NAME} / 한국 상장사 실적 예정일",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    ICS_PATH.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    start, end = rolling_range()
    used_cache = False
    error_message = None
    api_total_count = 0

    try:
        payload = fetch_scanner(start, end)
        api_total_count = int(payload.get("totalCount") or 0)
        events = parse_events(payload, start, end)
        if not events:
            raise RuntimeError("조회 기간의 한국 기업 실적 일정이 비어 있습니다.")
    except Exception as error:
        error_message = f"{type(error).__name__}: {error}"
        events = read_previous(start, end)
        used_cache = bool(events)
        if not events:
            raise

    EVENTS_PATH.write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_ics(events)

    months = dict(
        sorted(Counter(event["start"][:7] for event in events).items())
    )
    status = {
        "version": VERSION,
        "ok": not used_cache,
        "used_cache": used_cache,
        "updated_at": datetime.now(SEOUL).isoformat(timespec="seconds"),
        "timezone": "Asia/Seoul",
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "event_count": len(events),
        "api_total_count": api_total_count,
        "first_event_date": events[0]["start"],
        "last_event_date": events[-1]["start"],
        "events_by_month": months,
        "message": (
            "한국 상장사 다음 실적 예정일 갱신 완료"
            if not used_cache
            else "원본 일시 오류로 직전 정상 일정을 유지"
        ),
        "error": error_message,
    }
    STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"{VERSION} | {start} ~ {end} | {len(events)}건 | "
        f"{SOURCE_NAME} | cache={used_cache}"
    )


if __name__ == "__main__":
    main()

