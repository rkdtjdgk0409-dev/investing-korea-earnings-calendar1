# 미래 3개월 전체 조회 수정본

이 수정본은 긴 3개월 기간을 Investing.com에 한 번에 요청하지 않습니다.

서울 현재 날짜부터 정확히 3개월 뒤까지를 14일 단위로 나눠 요청한 후,
모든 구간의 결과를 합칩니다. 따라서 현재 주와 다음 주 데이터만 남는
문제를 방지합니다.

## 반드시 교체할 파일 4개

1. 저장소 최상단 `update_calendar.py`
2. 저장소 최상단 `requirements.txt`
3. `.github/workflows/update.yml`
4. `docs/index.html`

`docs/data/events.json`과 `docs/data/status.json`은 직접 교체하지 마세요.

## 교체 후

Actions → 한국 기업 실적 캘린더 갱신 → Run workflow

이전 실행의 Re-run jobs가 아니라 새 Run workflow를 실행하세요.

## 정상 확인

`docs/data/status.json`에서 다음을 확인합니다.

- `range.from`: 서울 현재 날짜
- `range.to`: 현재 날짜에서 정확히 3개월 뒤
- `date_window_count`: 약 7개
- `first_event_date`
- `last_event_date`
- `event_count`

캘린더는 월 단위 화면이므로 8월과 9월은 상단 오른쪽 화살표로 이동해
확인합니다. 데이터 조회 범위 자체는 상단 상태 문구에 표시됩니다.
