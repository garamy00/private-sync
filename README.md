# private-sync

노트북의 지정된 파일·디렉토리를 사내 DGX 서버로 자동 동기화하고, 텔레그램 봇으로
목록을 탐색해 암호 ZIP으로 내려받는다.

**왜 필요한가**: 사내 정책상 이 업무에는 외부 클라우드 저장소(구글 드라이브, 드롭박스 등)를
쓸 수 없다. 그렇다고 폰에서 문서를 꺼내 볼 방법이 없으면 곤란하다. private-sync는
노트북에서 만든 파일을 회사 내부망의 DGX 서버로만 옮기고, 그 서버에 이미 열려 있는
텔레그램 봇 채널을 통해 필요한 파일만 골라 암호 ZIP으로 받아보게 해준다. 외부 클라우드는
전혀 거치지 않는다.

설계 문서: [docs/superpowers/specs/2026-07-29-private-sync-design.md](docs/superpowers/specs/2026-07-29-private-sync-design.md)

## 구조

- `agent` (노트북): watchdog으로 변경을 감지해 3초 디바운스 후 rsync over SSH로 업로드한다.
  삭제는 전파하지 않는다.
- `bot` (DGX 서버): 텔레그램 `getUpdates` 롱폴링으로 명령을 받는다. 서버는 아웃바운드
  연결만 사용하므로 인바운드 포트를 열지 않는다.

두 프로세스는 서로 직접 통신하지 않는다. `agent`는 SSH/rsync로 서버의 저장소 디렉토리에
파일을 쓰기만 하고, `bot`은 그 저장소 디렉토리를 읽어 텔레그램에 응답할 뿐이다.

## 설치

**Python 3.11 이상이 반드시 필요하다.** 이 프로젝트는 `pyproject.toml`에
`requires-python = ">=3.11"`로 고정되어 있다. macOS 기본 `python3`(예: 이 문서를 쓴
환경에서는 3.9.6)는 요건을 만족하지 못하므로 **`python3` 명령을 그대로 쓰지 말 것**.

```bash
git clone <repo> private-sync && cd private-sync

# 시스템 python3 가 3.11 미만이면 venv 생성이 실패하거나 조용히 낮은 버전으로 만들어진다.
# 반드시 3.11+ 인터프리터를 명시해서 venv 를 만든다.
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`python3.11` 명령이 없다면(`command not found`) 아래 중 하나로 설치한다.

```bash
# macOS (Homebrew)
brew install python@3.11
/opt/homebrew/bin/python3.11 -m venv .venv   # Apple Silicon 기본 경로

# 또는 pyenv 사용 시
pyenv install 3.11.9
pyenv local 3.11.9
python3.11 -m venv .venv
```

설치 후 버전을 확인해 3.11 이상인지 확인한다.

```bash
.venv/bin/python --version
```

**rsync 참고**: 노트북(macOS) 기본 `rsync`는 Apple의 openrsync(프로토콜 29)이며 GNU
rsync 3.x가 아니다. 이 도구는 openrsync 기준으로 동작하도록 만들어져 있어 별도 조치가
필요 없다(Homebrew rsync가 PATH 앞쪽에 있어도 무방하다). 서버(DGX)에는 Python 3.12.3,
rsync 3.2.7이 이미 설치되어 있고 `/` 기준 746G 여유 공간을 확인했다.

## 노트북 설정

```bash
mkdir -p ~/.config/private-sync
cp config.example.yaml ~/.config/private-sync/agent.yaml
# agent.yaml 의 remote 와 sources 를 편집한다
.venv/bin/private-sync-agent --config ~/.config/private-sync/agent.yaml --debug
```

**`exclude`는 그것을 선언한 source 안에서만 적용된다.** 같은 경로가 `exclude` 없이
다른 source에도 등록되어 있으면, 그 경로는 다른 라벨 아래에서는 그대로 동기화된다.
하나의 경로를 정말로 제외하고 싶다면 그 경로를 참조하는 모든 source의 `exclude`에
같은 패턴을 넣어야 한다.

자동 시작(macOS LaunchAgent). **아래 `sed` 명령은 반드시 저장소 루트(clone한
디렉토리)에서 실행한다** — plist의 `CHANGEME` 경로를 실제 설치 경로로 바꿔치기하기
위해 현재 디렉토리(`$PWD`)를 사용하기 때문이다. 어디에 clone했든 그대로 동작한다.

```bash
sed -e "s|/Users/CHANGEME/source/python/private-sync|$PWD|g" -e "s|CHANGEME|$USER|g" \
  deploy/com.private-sync.agent.plist > ~/Library/LaunchAgents/com.private-sync.agent.plist
launchctl load ~/Library/LaunchAgents/com.private-sync.agent.plist
launchctl list | grep private-sync   # 로드되어 PID가 찍히는지 확인
```

로그는 `~/Library/Logs/private-sync-agent.log`에 쌓인다. 이 로그에 `Agent started
with N watch targets` 가 찍혀야 실제로 시작된 것이다. 등록을 해제하려면
`launchctl unload ~/Library/LaunchAgents/com.private-sync.agent.plist`.

**실제 업로드 검증 결과**: 공백과 한글이 섞인 라벨(`SKT 검증`)로 디렉토리 하나와
파일 하나를 함께 등록해 실제 DGX 서버에 대해 확인했다.

- watchdog이 변경을 감지하고 ~3초 디바운스 후 업로드했다(로그
  `Uploaded .../ps-test under label SKT 검증`, `Uploaded .../quote.xlsx under label SKT 검증`).
- 노트북과 서버의 파일 sha256이 세 파일 모두 정확히 일치했다.
- 디렉토리로 등록한 경로는 하위 구조를 그대로 유지한다(`SKT 검증/ps-test/sub/b.md`).
  파일 하나만 등록한 경우에는 라벨 바로 아래에 놓인다(`SKT 검증/quote.xlsx`).
- `.DS_Store`는 기본 제외 목록대로 서버에 하나도 올라오지 않았다.

## 서버 설정

`deploy/private-sync-bot.service`의 `ExecStart`가 `%h/private-sync/.venv/bin/...`
(즉 정확히 `~/private-sync`)를 가리키므로, 저장소는 반드시 그 경로에 clone한다.

```bash
ssh dgson@ai
git clone <repo> ~/private-sync
cd ~/private-sync
mkdir -p ~/private-sync/store ~/.config/private-sync ~/.config/systemd/user

# 서버는 Python 3.12.3, rsync 3.2.7 이 이미 설치되어 있고 / 기준 746G 여유가 있어
# 별도 준비 없이 바로 쓸 수 있다
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`bot.yaml`은 `store` 항목 하나만 있으면 된다. 아래처럼 완성된 내용으로 만든다.

```bash
cat > ~/.config/private-sync/bot.yaml <<'EOF'
store: ~/private-sync/store
EOF
```

`bot.env`는 `chmod 600`으로 만들고, 아래 세 줄을 실제 값으로 채워 넣는다
(`load_bot_config`가 정확히 이 세 이름을 읽는다).

```bash
install -m 600 /dev/null ~/.config/private-sync/bot.env
cat > ~/.config/private-sync/bot.env <<'EOF'
PRIVATE_SYNC_BOT_TOKEN=...
PRIVATE_SYNC_CHAT_ID=...
PRIVATE_SYNC_ZIP_PASSWORD=...
EOF
```

```bash
# 세션이 끊겨도 user systemd 인스턴스가 살아 있어야 봇이 계속 떠 있다.
# enable-linger 없이 등록하면 SSH 연결이 끊기는 순간 봇도 함께 죽는다.
loginctl enable-linger dgson
loginctl show-user dgson -p Linger   # Linger=yes 가 나와야 한다

cp deploy/private-sync-bot.service ~/.config/systemd/user/
systemctl --user enable --now private-sync-bot
```

**서버 확인 결과**: `ssh dgson@ai 'systemctl --user status'`가 정상 동작함을 확인했다
(호스트 `aitopatom-b476`, 유닛 478개 로드됨). 따라서 위의 `systemctl --user` 방식이
이 서버에서 쓸 수 있는 정식 방법이다. 다만 `loginctl show-user dgson -p Linger`가
기본값 `Linger=no`였으므로, **`enable-linger` 없이 그냥 서비스만 등록하면 SSH 세션이
끊기자마자 봇 프로세스도 함께 종료된다** — 이 프로젝트가 애초에 피하려던 실패 양상과
같다. 위 순서대로 `enable-linger`를 먼저 적용하고 `Linger=yes`로 바뀐 것을 확인한 뒤
서비스를 등록할 것.

사내 정책상 `enable-linger`가 거부되는 계정이라면(권한 부족 등), 아래 crontab 방식을
대신 쓴다. 이 경우 재부팅 시에만 자동 시작되고, 그 사이 프로세스가 죽으면 수동으로
다시 띄워야 한다.

```
@reboot cd $HOME && set -a && . $HOME/.config/private-sync/bot.env && set +a && nohup $HOME/private-sync/.venv/bin/private-sync-bot --config $HOME/.config/private-sync/bot.yaml >> $HOME/private-sync-bot.log 2>&1 &
```

### 봇 검증 절차 (실제 텔레그램 토큰이 있는 사용자가 직접 수행)

봇 검증에는 실제 텔레그램 봇 토큰과 chat_id가 필요해 구현자가 대신 수행할 수 없다.
아래 순서대로 직접 확인한다.

1. 텔레그램에서 `@BotFather`로 봇을 만들고 토큰을 받는다.
2. 만든 봇에게 아무 메시지나 보낸 뒤 chat_id를 확인한다:
   `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"id":[0-9-]*'`
3. 서버에 `~/.config/private-sync/bot.env`를 `chmod 600`으로 만들고
   `PRIVATE_SYNC_BOT_TOKEN`, `PRIVATE_SYNC_CHAT_ID`, `PRIVATE_SYNC_ZIP_PASSWORD`
   세 줄을 채운다(위 "서버 설정"의 예시 참고).
4. 봇을 포그라운드로 띄운다(저장소 루트인 `~/private-sync`에서 실행해야
   `.venv/bin/...` 상대 경로가 맞는다):
   `cd ~/private-sync && set -a && . ~/.config/private-sync/bot.env && set +a && .venv/bin/private-sync-bot --config ~/.config/private-sync/bot.yaml --debug`
5. 폰에서 확인한다: `/start`로 라벨 버튼이 보이는지 → 폴더를 타고 들어가 파일을 탭 →
   도착한 `.zip`을 압축 앱에서 비밀번호로 여는지 → `/find <키워드>`로 검색되는지.

## 봇 사용법

- `/start` — 저장소 목록을 버튼으로 표시
- `/find <키워드>` — 파일명 부분일치 검색
- 파일 버튼 탭 — AES-256 암호 ZIP으로 받는다. 폰 압축 앱에서 비밀번호를 넣고 열면 된다.
- 45MB를 넘으면 `.partNN` 으로 나눠 오므로 PC에서 `cat 이름.zip.part* > 이름.zip` 으로
  합친다. **분할된 파일은 폰에서 바로 열 수 없다.** 반드시 PC(또는 파트를 모두 합칠 수 있는
  환경)에서 먼저 합친 뒤 열어야 한다.

## 보안

- 봇 토큰, chat_id, ZIP 암호는 환경변수로만 읽는다. YAML과 코드에 넣지 않는다.
- 텔레그램에는 항상 암호 ZIP만 나간다. 서버 저장소에는 평문으로 둔다(사내 서버는 규정상
  허용 저장소).
- 서버의 저장소 디렉토리(`~/private-sync/store`)는 git 저장소 clone 내부에 있다.
  `.gitignore`의 `store/` 항목이 실수로 `git add`되는 것은 막아주지만, **`git clean -fdx`는
  ignore된 파일까지 지운다.** 따라서 `~/private-sync`에서는 어떤 경우에도
  `git clean -fdx`를 실행하지 않는다.
- 등록된 chat_id 외의 입력은 응답 없이 무시한다. 다른 사람이 봇을 알아내 말을 걸어도
  아무 반응이 없다(에러 메시지조차 보내지 않는다).
- 임시 ZIP은 전송 성공·실패와 무관하게 삭제된다.

**보호 범위를 정확히 알아야 한다**: ZIP은 **파일 내용만** AES-256으로 암호화한다.
**파일명은 암호화되지 않는다.** 텔레그램 서버와 대화 내용을 볼 수 있는 사람에게는
어떤 이름의 파일을 주고받았는지 그대로 보인다 — 애초에 봇의 목록 보기와 `/find` 검색
결과 자체가 파일명을 채팅으로 그대로 전송한다. 즉 "전송이 완전히 비공개"라고 오해하면
안 되며, 민감한 정보를 파일명에 담지 않는 것이 좋다. 내용 자체는 ZIP 암호 없이는
열 수 없다.

## 알아두어야 할 동작상의 한계

- **삭제는 절대 전파되지 않는다.** 노트북에서 파일을 지워도 서버 저장소에는 그대로
  남는다. 서버 쪽 정리는 수동으로 해야 한다.
- **`exclude`는 선언된 source에만 적용된다.** 위 "노트북 설정" 절 참고.
- **45MB 초과 파일은 `.partNN`으로 분할되어 도착하며, 분할 파일은 폰에서 열 수 없다.**
  PC에서 `cat 이름.zip.part* > 이름.zip`으로 합친 뒤 열어야 한다.
- **종료 시 최대 약 10분까지 걸릴 수 있다.** SIGTERM은 이미 실행 중인 rsync를 즉시
  중단시키지 못한다 — 진행 중인 전송이 끝나야 프로세스가 멈춘다. rsync가 `--partial`
  옵션으로 동작하므로 중단되더라도 이어받기(resume)로 처리되어 데이터가 손실되지는
  않는다.
- **등록된 `chat_id`가 아니면 무응답으로 무시된다.** 오작동이 아니라 의도된 동작이다.

## 개발

```bash
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```
