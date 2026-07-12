# v5.1 완전 교체 안내

이번 수정은 이전 파일 위에 일부만 덧붙이는 패치가 아닙니다. 아래 파일을 **정확한 경로에 전부 교체**해야 합니다.

## 현재 문제가 반복된 이유

저장소에서 실제 실행되는 파일은 루트의 `update_calendar.py`이고, 실제 Pages 화면은 `docs/index.html`입니다. 이전 교체 과정에서 일부 파일만 바뀌면서:

- 상태 파일에는 3개월 범위라고 기록됨
- 실제 요청은 한 번만 실행됨
- Investing.com이 현재·다음 주 응답을 돌려줘도 성공 처리됨
- 캘린더 화면은 이전 배포본을 계속 표시함

상태가 섞였습니다.

v5.1은 3개월을 14일 단위로 나누고, 각 응답의 날짜가 요청 구간과 실제로 겹치는지 확인합니다. 서버가 다른 주간 데이터를 반환하면 성공으로 저장하지 않습니다.

## 교체할 파일

| ZIP 내부 | GitHub 경로 |
|---|---|
| `update_calendar.py` | 저장소 최상단 |
| `validate_output.py` | 저장소 최상단 |
| `requirements.txt` | 저장소 최상단 |
| `.github/workflows/update.yml` | 동일 경로 |
| `docs/index.html` | 동일 경로 |
| `docs/.nojekyll` | 동일 경로 |

`docs/data/events.json`과 `docs/data/status.json`은 직접 덮어쓰지 마세요.

## 권장: 혼동을 주는 옛 파일 삭제

다음 파일은 새 실행에 쓰이지 않으므로 삭제해도 됩니다.

- 저장소 최상단의 `update.yml`
- 저장소 최상단의 `index.html`
- 저장소 최상단의 `events.json`
- 저장소 최상단의 `status.json`
- `scripts/update_calendar.py`
- `download`

## 실행

1. 모든 파일 교체 후 Commit
2. Actions
3. `한국 기업 실적 캘린더 갱신 v5.1`
4. Run workflow
5. 기존 실행의 Re-run jobs가 아니라 새 Run workflow 실행

## 성공 확인

`docs/data/status.json`에서 반드시 다음이 보여야 합니다.

```json
{
  "version": "v5.1-strict-rolling-3month",
  "window_count": 7,
  "failed_window_count": 0
}
```

화면 상단에도 `v5.1-strict-rolling-3month`가 표시됩니다. 이 문구가 없으면 이전 Pages 파일을 보고 있는 것입니다.

## 화면

기본 화면은 3개월 달력입니다. 폭이 좁은 노션 임베드에서는 3개월이 세로로 쌓입니다. 각 날짜 안에서는 시가총액이 큰 기업부터 표시됩니다.
