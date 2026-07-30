# private-sync 설계

작성일: 2026-07-29

## 배경과 목적

회사 보안 지침에 따라 업무 자료를 외부 저장소(Google Docs, Office365, 개인 웹드라이브)에
두는 것이 금지되었다. 사내 NAS와 개인 노트북만 허용된다.

지금까지 외부 저장소가 담당하던 "노트북 자료를 사내 서버에 올려두고, 외부에서 필요할 때 폰으로
꺼내 본다"는 흐름을 규정을 지키면서 대체하는 것이 목적이다.

핵심 제약은 네트워크다. 사내 DGX 서버(`user@sync-server`)는 외부 인터넷으로
나갈 수는 있지만 외부에서 들어올 수는 없다. 따라서 서버가 텔레그램 쪽으로 나가서 명령을 받아오는
롱폴링(`getUpdates`) 방식을 쓴다. 서버에 인바운드 포트를 열지 않는다.

### 필요 환경

- DGX 서버: Python 3.11 이상, `rsync` 존재, 저장소가 쌓일 여유 공간이 있는 루트 파티션
- `ssh user@sync-server` SSH 키 인증으로 비밀번호 없이 접속 가능해야 한다 (`BatchMode=yes`로 확인)
- 참고 구현: `nol-booking/telegram_control.py` (requests + getUpdates 롱폴링, 순수 함수 dispatch)

## 결정 사항

브레인스토밍에서 확정한 선택지와 근거다.

| 항목 | 결정 | 근거 |
|---|---|---|
| 텔레그램 경유 전송 | 암호화 후 전송 | 텔레그램 클라우드에 평문이 남지 않게 한다 |
| 복호화 지점 | 폰에서 바로 열 수 있어야 함 → 암호 걸린 ZIP(AES-256) | 폰 압축 앱에서 비밀번호로 열 수 있고 PC에서도 별도 도구가 필요 없다 |
| 서버 보관 형태 | 평문 보관, 텔레그램 전송 시에만 암호화 | 사내 서버는 규정상 허용 저장소다. 평문이면 사내 PC에서 바로 쓰고 검색도 된다 |
| 동기화 트리거 | 백그라운드 자동 감시 (watchdog) | 사용자가 올리는 것을 잊지 않게 한다 |
| 보관 정책 | 단방향 누적 (삭제 전파 없음) | 자동 감시와 삭제 전파를 결합하면 실수 삭제가 즉시 서버까지 번진다 |
| 봇 UX | 인라인 키보드 버튼 탐색 + `/find` 검색 | 폰에서 오타 없이 조작할 수 있다 |
| 접근 통제 | chat_id 화이트리스트 | 단순함 우선. ZIP 비밀번호가 2차 방어선 역할을 한다 |
| 전송 수단 | rsync over SSH | 이미 동작하는 키 인증을 재사용하고 증분 전송·재개·무결성 검증을 공짜로 얻는다 |
| 경로 지정 | 디렉토리와 개별 파일 혼합, `exclude` 패턴만 | "이 폴더에서 이 파일만" 같은 요구를 지원한다 |

### 보안 판단 근거

chat_id 화이트리스트만으로는 폰을 분실했을 때 봇 조작 자체를 막지 못한다. 그러나 전송되는 파일이
전부 AES-256 암호 ZIP이므로, 비밀번호를 모르면 내용을 볼 수 없다. 세션 패스코드를 도입하지 않은
대신 이 성질에 의존한다는 점을 명시해 둔다.

## 시스템 구조

두 개의 독립 프로세스이며 서로 직접 통신하지 않는다. 유일한 접점은 DGX 서버의 저장소 디렉토리다.

```
[노트북]                            [DGX 서버]                      [텔레그램]
 agent 데몬                          bot 데몬
 ├ watchdog 폴더 감시                 ├ getUpdates 롱폴링 (아웃바운드) ←→ 텔레그램 서버
 ├ exclude 필터 + 3초 디바운스         ├ 저장소 트리 탐색 / 파일명 검색
 └ rsync -az over SSH ──→ ~/private-sync/store/ ──→ AES-256 ZIP 포장 ──→ sendDocument
                          (평문 보관)              (임시파일, 전송 후 삭제)      ↓
                                                                             폰
```

## 설정

agent와 bot은 서로 다른 장비에서 돌기 때문에 설정 파일도 각각 둔다. 아래는 두 파일의 내용을
한자리에 보인 것이다. 비밀값은 전부 환경변수이며 YAML에는 넣지 않는다.

```yaml
# 노트북: agent.yaml
remote:
  host: user@sync-server
  store: ~/private-sync/store

sources:
  - label: 업무 문서
    paths:
      - ~/Documents/work/            # 디렉토리 전체
      - ~/work/견적서_v3.xlsx        # 파일 하나만
      - ~/work/회의록.md
    exclude: ["*.tmp"]              # 내장 기본 제외 목록에 추가된다

  - label: 개인 메모
    paths:
      - ~/notes/

# DGX 서버: bot.yaml
store: ~/private-sync/store
```

내장 기본 제외 목록: `.DS_Store`, `~$*`, `*.swp`, `.git/`

환경변수:

| 변수 | 사용처 | 설명 |
|---|---|---|
| `PRIVATE_SYNC_BOT_TOKEN` | bot | 텔레그램 봇 토큰 |
| `PRIVATE_SYNC_CHAT_ID` | bot | 허용할 단일 chat_id |
| `PRIVATE_SYNC_ZIP_PASSWORD` | bot | 전송용 ZIP 암호 |

### 설정 검증

시작 시 다음을 확인하고, 실패하면 명확한 메시지와 함께 즉시 종료한다.

- `paths`의 각 경로가 존재하는지
- 라벨이 중복되지 않는지
- 같은 라벨 안에서 서버 저장 경로가 충돌하지 않는지 (서로 다른 디렉토리의 동명 파일을 개별
  지정하면 나중 것이 덮어쓴다. 이 경우 에러로 알린다)
- 필수 환경변수가 설정되어 있는지

## 데이터 흐름

### 업로드 (자동)

1. watchdog이 감시 대상 변경을 감지한다. 개별 파일 항목은 watchdog이 파일을 직접 감시할 수 없으므로
   부모 디렉토리를 감시하고 해당 경로만 통과시키는 필터를 건다.
2. exclude 패턴에 걸리면 버린다.
3. 3초 디바운스로 묶는다. 에디터의 연속 저장과 임시파일 생성·삭제를 한 번의 전송으로 합치기 위함이다.
4. `rsync -az --partial` 로 `user@sync-server:~/private-sync/store/<라벨>/` 에 전송한다.
   `shell=True` 없이 인수 배열로 호출한다.
5. 삭제는 전파하지 않는다(`--delete` 미사용).

서버 저장 위치는 `store/<라벨>/` 아래에, 디렉토리는 원본 상대경로를 유지하고 개별 파일은 파일명
그대로 놓는다.

### 사내망 밖일 때

rsync가 연결에 실패하면 지수 백오프(3초에서 시작해 최대 5분)로 재시도하고, 미전송 항목을 로컬
상태 파일(JSON)에 보존한다. 데몬을 재시작해도 목록이 유지되며, 사내망에 복귀하면 밀린 항목부터
올라간다.

연결 실패가 아닌 권한·디스크 오류는 재시도하지 않는다. ERROR로 로깅하고 해당 항목을 격리해
무한 재시도를 막는다.

### 다운로드 (봇)

1. `/start` → 라벨 목록을 인라인 키보드 버튼으로 표시
2. 버튼 탭 → 하위 디렉토리·파일 버튼 표시, `⬆️ 상위` 버튼으로 복귀
3. `/find <키워드>` → 파일명 부분일치 결과를 버튼으로 표시
4. 파일 버튼 탭 → AES-256 ZIP 생성 → `sendDocument` → 임시파일 삭제
5. ZIP이 50MB(텔레그램 봇 전송 한도)를 넘으면 완성된 ZIP 파일을 바이트 단위로 45MB씩 잘라
   `<이름>.zip.part01`, `.part02` … 로 순서대로 전송하고, `cat <이름>.zip.part* > <이름>.zip`
   결합 명령을 안내 메시지로 첨부한다. 분할된 경우 폰에서 바로 열 수 없고 PC에서 결합해야 한다

## 모듈 구성

설정 스키마를 공유하므로 한 패키지에 두 진입점을 둔다. 서버에는 `bot` 쪽만 배포한다.

```
private-sync/
├── src/private_sync/
│   ├── config.py          # YAML 로드 + 검증
│   ├── errors.py          # AppBaseError 계층
│   ├── agent/
│   │   ├── watcher.py     # watchdog 이벤트 → exclude 필터 → 디바운스 큐
│   │   ├── uploader.py    # rsync 인수 조립 및 실행
│   │   ├── pending.py     # 미전송 목록 영속화 (JSON)
│   │   └── main.py
│   └── bot/
│       ├── telegram.py    # Bot API 래퍼
│       ├── store.py       # 저장소 트리 탐색·검색, 경로 안전 검증
│       ├── packer.py      # AES-256 ZIP 생성 + 45MB 분할
│       ├── handlers.py    # 명령·콜백 디스패치 (순수 로직, I/O 없음)
│       └── main.py        # 롱폴링 루프
├── tests/
├── config.example.yaml
├── pyproject.toml
└── README.md
```

`handlers.py`는 I/O 없는 순수 로직으로 유지한다. 입력은 파싱된 update와 저장소 스냅샷, 출력은
"무엇을 보낼지"를 나타내는 값이다. 텔레그램 API 없이 명령 처리 전체를 테스트할 수 있게 하기
위함이며, `nol-booking/telegram_control.py`의 `dispatch()`와 같은 구조다.

`telegram.py`가 감싸는 Bot API 메서드: `getUpdates`, `sendMessage`, `sendDocument`,
`editMessageText`, `answerCallbackQuery`.

## 의존성

- `watchdog` — 파일 변경 감시 (agent)
- `pyzipper` — AES-256 ZIP 생성 (bot). 표준 `zipfile`은 AES를 지원하지 않는다
- `requests` — 텔레그램 Bot API (bot). 기존 프로젝트와 동일하게 raw HTTP를 쓰며
  `python-telegram-bot`은 도입하지 않는다
- `PyYAML` — 설정 로드
- 개발: `pytest`, `ruff`

## 보안

- 비밀값은 전부 환경변수로 받는다. YAML에는 경로 설정만 들어간다.
- 예외 메시지에 URL이 포함되면 토큰이 노출될 수 있으므로, 텔레그램 관련 예외는
  `type(exc).__name__`만 로깅한다.
- 콜백 데이터에는 실제 경로 대신 세션 맵의 짧은 ID만 담는다. 텔레그램 `callback_data`가 64바이트
  제한이라 어차피 필요하며, 동시에 경로 조작을 차단한다. 최종 경로가 저장소 루트 하위인지
  `Path.resolve()`로 한 번 더 확인한다.
- 임시 ZIP은 `mkdtemp`로 만들고 전송 성공·실패와 무관하게 `finally`에서 삭제한다.
- rsync는 인수 배열로 호출한다 (`shell=True` 금지).

## 에러 처리

| 상황 | 동작 |
|---|---|
| rsync 연결 실패 (사내망 밖) | 지수 백오프 재시도, 미전송 목록 유지, WARNING |
| rsync 권한·디스크 오류 | 재시도 없음, ERROR, 항목 격리 |
| 텔레그램 API 일시 실패 | 재시도, 롱폴링 루프는 죽지 않음 |
| 잘못된 형식의 update | 방어적으로 무시, WARNING |
| 등록되지 않은 chat_id | 무시하고 응답도 하지 않음, WARNING |
| ZIP이 50MB 초과 | 45MB 단위 분할 전송 + 결합 명령 안내 |
| 요청 파일이 서버에 없음 | "동기화 대기 중이거나 삭제됨" 안내 |

## 테스트

커버리지 수치를 위한 형식적 테스트는 만들지 않는다. 실제로 깨질 수 있는 것만 검증한다.

- `packer`: AES ZIP을 실제로 만들어 비밀번호로 열고 원본 바이트와 일치하는지 확인.
  50MB 초과 파일의 분할과 재결합 검증
- `handlers`: 등록되지 않은 chat_id 거부, 디렉토리 진입과 상위 이동, 검색 결과, 없는 파일 요청
- `store`: 조작된 ID로 저장소 밖 파일에 접근할 수 없는지
- `uploader`: 디렉토리와 개별 파일 각각의 rsync 인수 조립, 실패 시 미전송 목록 유지
- `watcher`: 디바운스가 연속 저장을 한 번으로 묶는지, exclude 패턴이 걸러지는지
- `config`: 라벨 중복과 저장 경로 충돌을 시작 시점에 잡아내는지

## 배포와 실행

- 노트북 agent: macOS LaunchAgent(plist)로 로그인 시 자동 시작
- 서버 bot: `systemd --user` 서비스로 등록한다. DGX에서 user 단위 systemd를 쓸 수 없으면
  `@reboot` cron + `nohup`으로 대체한다. 어느 쪽이 가능한지는 구현 단계에서 확인한다.

## 범위에서 제외

다음은 이번 범위에 넣지 않는다. 필요해지면 별도 사이클로 다룬다.

- 웹 UI
- 서버 → 노트북 역방향 동기화
- 다중 사용자 지원
- 파일 버전 이력 보관
- 봇을 통한 업로드
