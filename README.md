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

자동 시작(macOS LaunchAgent):

```bash
sed "s|CHANGEME|$USER|g" deploy/com.private-sync.agent.plist \
  > ~/Library/LaunchAgents/com.private-sync.agent.plist
launchctl load ~/Library/LaunchAgents/com.private-sync.agent.plist
```

로그는 `~/Library/Logs/private-sync-agent.log`에 쌓인다. 등록을 해제하려면
`launchctl unload ~/Library/LaunchAgents/com.private-sync.agent.plist`.

## 서버 설정

```bash
ssh dgson@ai
mkdir -p ~/private-sync/store ~/.config/private-sync
# 저장소 코드를 서버에 배치하고 venv 를 만든다 (서버는 Python 3.12.3 이 이미 설치되어 있어
# 별도로 3.11+ 를 준비할 필요가 없다)
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.yaml ~/.config/private-sync/bot.yaml   # store 항목만 남긴다
install -m 600 /dev/null ~/.config/private-sync/bot.env  # 토큰·chat_id·ZIP 암호
cp deploy/private-sync-bot.service ~/.config/systemd/user/
systemctl --user enable --now private-sync-bot
```

`systemctl --user` 를 쓸 수 없으면 대신 crontab에 아래를 넣는다.

```
@reboot cd $HOME && set -a && . $HOME/.config/private-sync/bot.env && set +a && nohup $HOME/private-sync/.venv/bin/private-sync-bot --config $HOME/.config/private-sync/bot.yaml >> $HOME/private-sync-bot.log 2>&1 &
```

**서버 확인 결과**: 이번 구현 세션에서는 샌드박스에서 `ssh dgson@ai`로 사내망
(`192.168.5.78`)에 접속할 수 없어(포트 22 TCP 연결은 되지만 SSH 배너 교환 단계에서
타임아웃) `systemctl --user status` 를 직접 실행해 확인하지 못했다. 실제 배포 시
아래 명령으로 먼저 확인한 뒤 그 결과에 맞는 방식을 쓸 것.

```bash
ssh dgson@ai 'systemctl --user status 2>&1 | head -3'
```

- `Failed to connect to bus` 등의 오류가 나오면 systemd user 인스턴스가 없는 것이므로
  위의 crontab 방식을 쓴다(리부팅 시에만 자동 시작되고, 그 사이 프로세스가 죽으면 수동으로
  다시 띄워야 한다).
- 정상적으로 상태가 출력되면 `systemctl --user enable --now private-sync-bot` 방식을
  그대로 쓴다(재시작 정책 `Restart=always`가 적용되어 더 안전하다).

### 봇 검증 절차 (실제 텔레그램 토큰이 있는 사용자가 직접 수행)

봇 검증에는 실제 텔레그램 봇 토큰과 chat_id가 필요해 구현자가 대신 수행할 수 없다.
아래 순서대로 직접 확인한다.

1. 텔레그램에서 `@BotFather`로 봇을 만들고 토큰을 받는다.
2. 만든 봇에게 아무 메시지나 보낸 뒤 chat_id를 확인한다:
   `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"id":[0-9-]*'`
3. 서버에 `~/.config/private-sync/bot.env`를 `chmod 600`으로 만들고 토큰·chat_id·ZIP 암호를 넣는다.
4. 봇을 포그라운드로 띄운다:
   `set -a && . ~/.config/private-sync/bot.env && set +a && .venv/bin/private-sync-bot --config ~/.config/private-sync/bot.yaml --debug`
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
