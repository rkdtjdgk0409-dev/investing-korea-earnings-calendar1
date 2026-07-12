수정 방법

1. 저장소 최상단의 update_calendar.py를 이 파일로 교체합니다.
2. .github/workflows/update.yml도 함께 교체합니다.
3. Actions → 한국 기업 실적 캘린더 갱신 → Run workflow를 실행합니다.
4. 실행이 끝나면 docs/data/status.json의 event_count를 확인합니다.

이번 수정은:
- Investing.com의 레거시 earningscalendar 경로 사용
- 현재 달부터 3개월 범위를 7일 단위로 조회
- 한국 국가표시와 중요도 3단계 행만 저장
- 0건일 때 기존 정상 데이터 유지
- 실패 분석용 debug 정보와 HTML 저장
