from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from dateutil.relativedelta import relativedelta


ROOT = Path(__file__).resolve().parent
STATUS_PATH = ROOT / "docs" / "data" / "status.json"
EVENTS_PATH = ROOT / "docs" / "data" / "events.json"
EXPECTED_VERSION = "v5.1-strict-rolling-3month"


def main() -> None:
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))

    assert status.get("version") == EXPECTED_VERSION, (
        "잘못된 업데이트 스크립트가 실행되었습니다: "
        f"{status.get('version')!r}"
    )

    start = date.fromisoformat(status["range"]["from"])
    end = date.fromisoformat(status["range"]["to"])

    assert end == start + relativedelta(months=3), (
        f"3개월 범위가 아닙니다: {start} ~ {end}"
    )

    assert status["window_count"] >= 6, (
        "3개월을 여러 구간으로 나누지 않았습니다."
    )

    successful_windows = int(status.get("successful_window_count", 0))
    failed_windows = int(status.get("failed_window_count", 0))

    assert successful_windows >= 1, (
        "성공한 날짜 구간이 하나도 없습니다."
    )

    assert successful_windows + failed_windows == int(status["window_count"]), (
        "날짜 구간 집계가 맞지 않습니다."
    )

    grouped: dict[str, list[int]] = defaultdict(list)

    for event in events:
        event_date = date.fromisoformat(event["start"])
        assert start <= event_date <= end, (
            f"범위 밖 이벤트: {event['start']} {event.get('company')}"
        )

        expected_title = f"{event['company']} 실적"
        assert event.get("title") == expected_title, (
            f"일정 제목 오류: {event.get('title')!r}"
        )

        grouped[event["start"]].append(int(event.get("marketCap") or 0))

    for event_date, market_caps in grouped.items():
        assert market_caps == sorted(market_caps, reverse=True), (
            f"{event_date} 시가총액순 정렬 실패"
        )

    print(
        f"검증 성공: {status['version']} | "
        f"{start} ~ {end} | {len(events)}건 | "
        f"{successful_windows}/{status['window_count']}개 구간 성공"
    )


if __name__ == "__main__":
    main()
