from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATUS_PATH = ROOT / "docs" / "data" / "status.json"
EVENTS_PATH = ROOT / "docs" / "data" / "events.json"
EXPECTED_VERSION = "v6-tradingview-korea-next-earnings"


def main() -> None:
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))

    assert status.get("version") == EXPECTED_VERSION, status.get("version")
    assert isinstance(events, list) and events, "실적 일정이 비어 있습니다."

    start = date.fromisoformat(status["range"]["from"])
    end = date.fromisoformat(status["range"]["to"])
    grouped: dict[str, list[int]] = defaultdict(list)

    for event in events:
        event_date = date.fromisoformat(event["start"])
        assert start <= event_date <= end, event
        assert event["title"] == f"{event['company']} 실적", event
        if not status.get("used_cache"):
            assert event.get("source") == "TradingView Korea Scanner", event
        grouped[event["start"]].append(int(event.get("marketCap") or 0))

    for event_date, market_caps in grouped.items():
        assert market_caps == sorted(market_caps, reverse=True), event_date

    print(
        f"검증 성공: {status['version']} | "
        f"{start} ~ {end} | {len(events)}건"
    )


if __name__ == "__main__":
    main()
