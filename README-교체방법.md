# 한국 기업 실적 캘린더 v3 교체 방법

이번 버전은 Playwright/Chromium을 설치하지 않고 Investing.com의 캘린더 요청을 직접 사용하므로 이전 버전보다 훨씬 가볍게 실행됩니다.

## 교체할 파일

ZIP의 파일을 아래 위치에 덮어씁니다.

1. `update_calendar.py` → 저장소 최상단의 `update_calendar.py`
2. `requirements.txt` → 저장소 최상단의 `requirements.txt`
3. `.github/workflows/update.yml` → 같은 경로의 기존 파일
4. `docs/index.html` → 같은 경로의 기존 파일

## 실행

GitHub에서:

`Actions → 한국 기업 실적 캘린더 갱신 → Run workflow`

## 동작 범위

실행할 때마다 `Asia/Seoul`의 현재 날짜를 구합니다.

- 시작: 서울의 오늘 날짜
- 종료: 시작 날짜에서 정확히 3개월 뒤
- 필터: 국가 한국, 중요도 높음
- 화면 제목: `기업명 실적`

예: 서울 날짜가 2026-07-12이면 2026-07-12부터 2026-10-12까지 조회합니다.

## 자동 실행

매일 한국시간 약 오전 7시 17분에 실행됩니다. 예약 실행은 GitHub 사정에 따라 조금 늦게 시작될 수 있습니다.

## 확인할 파일

- `docs/data/events.json`: 캘린더 일정
- `docs/data/status.json`: 범위, 일정 개수, 실행 시간과 진단 정보
- `docs/earnings.ics`: ICS 캘린더

`events.json`의 각 일정에는 다음 형태의 제목이 있어야 합니다.

```json
{
  "title": "삼성전자 실적",
  "company": "삼성전자",
  "start": "2026-07-30"
}
```
