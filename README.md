# Investing.com 한국 주요 기업 실적 캘린더

Investing.com 실적 캘린더에서 아래 조건만 남겨 GitHub Pages 캘린더로 보여줍니다.

- 국가: **한국**
- 중요도: **높음**
- 캘린더에 표시되는 문구: **`기업명 실적`**
- 갱신: GitHub Actions가 하루 4회 실행
- 출력: `docs/data/events.json`, `docs/earnings.ics`, GitHub Pages 웹 캘린더

> Investing.com은 화면 구조와 봇 차단 방식을 바꿀 수 있습니다. 스크립트는 UI 필터와 HTML 재검증을 함께 사용하고, 일시적으로 수집에 실패하면 기존 정상 데이터를 지우지 않도록 구성했습니다.

## 1. GitHub에 올리기

1. GitHub에서 새 저장소를 만듭니다.
2. 압축을 풀고 **숨김 폴더 `.github`까지 포함해** 전체 파일을 저장소 최상단에 업로드합니다.
3. 저장소의 **Actions** 탭에서 `한국 기업 실적 캘린더 갱신`을 선택합니다.
4. `Run workflow`를 눌러 처음 한 번 수동 실행합니다.
5. 실행이 끝난 뒤 `docs/data/events.json`에 데이터가 들어왔는지 확인합니다.

## 2. GitHub Pages 켜기

저장소에서:

`Settings → Pages → Build and deployment`

- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/docs`
- Save

잠시 뒤 주소가 생성됩니다.

`https://본인아이디.github.io/저장소이름/`

## 3. 노션에 넣기

노션 페이지에서 `/임베드` 또는 `/embed`를 입력한 뒤, 위 GitHub Pages 주소를 붙여 넣습니다.

이 방식은 **노션 데이터베이스 캘린더가 아니라 자동 갱신되는 임베드 캘린더**입니다. 화면에는 각 일정이 `기업명 실적` 형식으로만 표시됩니다. EPS, 매출, 예상치 같은 상세 숫자는 표시하지 않습니다.

## 4. 노션 데이터베이스 캘린더로 직접 만들고 싶은 경우

ICS 주소는 아래 형식입니다.

`https://본인아이디.github.io/저장소이름/earnings.ics`

다만 Notion 기본 데이터베이스는 외부 ICS를 지속적으로 자동 동기화하지 않습니다. 따라서 별도 Notion API 자동화 없이 계속 갱신하려면 GitHub Pages 임베드 방식을 사용해야 합니다.

## 5. 갱신 시간 변경

`.github/workflows/update.yml`의 cron을 수정합니다. GitHub Actions cron은 **UTC 기준**입니다.

현재 설정:

```yaml
- cron: "17 22,4,10,16 * * *"
```

한국시간 기준 약 01:17, 07:17, 13:17, 19:17입니다. GitHub 사정으로 몇 분 늦게 시작될 수 있습니다.

## 6. 오류 확인

- `docs/data/status.json`: 마지막 실행 결과
- `docs/data/last_success.png`: 마지막 정상 화면
- `docs/data/last_error.png`: 오류가 난 경우 화면

Investing.com의 구조가 변경되면 `scripts/update_calendar.py`의 선택자를 조정해야 할 수 있습니다.
