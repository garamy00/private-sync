# 폴더 압축 다운로드와 대형 목록 처리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 항목이 많은 폴더를 탐색할 수 있게 하고, 폴더를 통째로 압축해 받을 수 있게 한다.

**Architecture:** 세 덩어리를 순서대로 쌓는다. 먼저 `_browse`의 버튼을 페이지로 나누고 전달 실패를 사용자에게 알린다(지금 막혀 있는 것). 다음으로 텔레그램 API 베이스 URL을 설정으로 빼 로컬 Bot API 서버를 쓸 수 있게 한다. 마지막으로 폴더 크기 확인 화면과 압축 전송을 얹는다.

**Tech Stack:** Python 3.11+, requests, pyzipper, pytest, ruff

**Spec:** [docs/superpowers/specs/2026-08-03-folder-download-design.md](../specs/2026-08-03-folder-download-design.md)

## Global Constraints

- Python `requires-python = ">=3.11"`. 타입 힌트는 `X | None` 형식을 쓴다 (`Optional[X]` 금지).
- ruff `line-length = 88`. 커밋 전 `ruff format` 과 `ruff check` 를 통과해야 한다. `pyproject.toml` 을 수정하지 않는다 (ruff 기본 규칙 유지).
- 새 `# noqa` 를 추가하지 않는다. `bot/main.py` 의 기존 `# noqa: BLE001` 한 줄은 그대로 둔다.
- 모든 public 함수에 타입 힌트와 Google Style docstring을 작성한다. 1줄 docstring은 명령형으로 쓰고 마침표로 끝낸다.
- 로그는 `logging` 모듈만 사용한다. 로그 호출은 lazy args 를 쓴다: `logger.info("msg %s", value)`.
- **로그가 아닌 문자열은 f-string 으로 만든다.** `%` 연산자는 쓰지 않는다.
- 로그 메시지는 영문, 주석과 docstring은 한국어로 작성한다.
- 예외는 구체적 타입만 잡는다. bare `except:` 와 `except Exception:` 금지.
- 구조 있는 데이터는 `dict` 대신 dataclass를 쓴다.
- 파라미터는 3개 이하로 유지한다(`self` 제외). 초과하면 dataclass로 묶는다.
- **비밀값은 환경변수로만 읽는다.** 봇 토큰과 ZIP 암호를 로그·예외 메시지에 넣지 않는다.
- `.venv/bin/pytest` 와 `.venv/bin/ruff` 를 쓴다. venv를 새로 만들거나 `pip install` 을 실행하지 않는다.
- **네트워크·SSH 접근을 시도하지 않는다.** 텔레그램 세션은 항상 가짜를 주입한다.
- 커밋 메시지는 `<type>: <요약>` 형식. 본문 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` 를 넣는다.

---

### Task 1: 목록 페이지네이션

**Files:**
- Modify: `src/private_sync/bot/handlers.py` (`TokenMap`, `_browse`, `_handle_message`, `_handle_callback`)
- Test: `tests/test_handlers.py`

**Interfaces:**
- Consumes: `private_sync.bot.store.Entry`, `private_sync.bot.store.parent_rel`
- Produces: `_PAGE_SIZE: int`, `BrowseView(rel: str, page: int = 0, edit: bool = False)` (frozen dataclass), `TokenMap.put(kind: str, rel: str, page: int = 0) -> str`, `TokenMap.get(token: str) -> tuple[str, str, int] | None`, `_browse(ctx: Context, view: BrowseView) -> SendText`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_handlers.py` 의 기존 `_ctx` 헬퍼는 그대로 두고, 아래를 `test_token_map_evicts_oldest_beyond_limit` 바로 앞에 넣는다.

```python
def _many(count):
    return [
        Entry(name=f"{i:03d}.mp3", rel=f"음악/{i:03d}.mp3", is_dir=False, size=10)
        for i in range(count)
    ]


def test_large_directory_is_split_into_pages():
    ctx = _ctx(listing={"음악": _many(45)})

    first = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    labels = [label for label, _ in first.buttons]
    # 한 화면에 45개를 다 실으면 텔레그램이 400 으로 거부한다
    assert sum(1 for label in labels if label.startswith("📄")) == _PAGE_SIZE
    assert "1/3" in labels
    assert "다음 ▶" in labels
    assert "◀ 이전" not in labels


def test_middle_page_has_both_arrows_and_parent():
    ctx = _ctx(listing={"음악": _many(45)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악", 1),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    labels = [label for label, _ in action.buttons]
    assert "◀ 이전" in labels
    assert "다음 ▶" in labels
    assert "2/3" in labels
    # 3페이지에서도 되돌아갈 수 있어야 한다
    assert labels[0] == "⬆️ 상위"


def test_last_page_holds_the_remainder():
    ctx = _ctx(listing={"음악": _many(45)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악", 2),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    labels = [label for label, _ in action.buttons]
    assert sum(1 for label in labels if label.startswith("📄")) == 5
    assert "다음 ▶" not in labels


def test_page_beyond_the_end_is_clamped():
    ctx = _ctx(listing={"음악": _many(45)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악", 99),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert "3/3" in [label for label, _ in action.buttons]


def test_exact_multiple_of_page_size_has_no_empty_page():
    ctx = _ctx(listing={"음악": _many(40)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악", 0),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert "1/2" in [label for label, _ in action.buttons]


def test_small_directory_has_no_pager():
    action = handle(_message("/start"), _ctx())

    labels = [label for label, _ in action.buttons]
    # 페이지가 하나뿐이면 n/N 표시도 화살표도 없어야 한다
    assert not any(label[0].isdigit() for label in labels)
    assert "다음 ▶" not in labels


def test_page_number_never_reaches_callback_data():
    ctx = _ctx(listing={"음악": _many(45)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    # 경로도 페이지도 callback_data 로 새면 안 된다
    for _label, data in action.buttons:
        assert "음악" not in data
        assert data.isascii()
```

`_ctx` 헬퍼가 `listing` 인자를 받도록 시그니처를 바꾼다. 기본값이 있으므로 기존 호출은 그대로 동작한다.

```python
def _ctx(chat_id="123", listing=None, results=None):
    tree = {"": ROOT, "SKT 문서": SKT} if listing is None else listing
    return Context(
        chat_id=chat_id,
        tokens=TokenMap(),
        lister=lambda rel: tree.get(rel, []),
        searcher=lambda kw: results or [],
    )
```

임포트에 `_PAGE_SIZE` 를 더한다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_handlers.py -v`
Expected: FAIL — `ImportError: cannot import name '_PAGE_SIZE'`

- [ ] **Step 3: 구현**

`handlers.py` 상단 상수에 추가한다.

```python
# 한 화면에 담을 항목 수. 텔레그램은 버튼이 너무 많은 reply_markup 을 400 으로
# 거부하며, 그 실패는 사용자에게 "눌러도 반응 없음" 으로 보인다.
_PAGE_SIZE = 20
```

`TokenMap` 을 페이지까지 담도록 넓힌다.

```python
    def __init__(self, limit: int = 500) -> None:
        self._limit = limit
        self._entries: OrderedDict[str, tuple[str, str, int]] = OrderedDict()

    def put(self, kind: str, rel: str, page: int = 0) -> str:
        """(kind, rel, page)에 대한 토큰을 발급한다."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._entries[token] = (kind, rel, page)
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return token

    def get(self, token: str) -> tuple[str, str, int] | None:
        """토큰에 해당하는 (kind, rel, page)를 반환한다. 없으면 None."""
        return self._entries.get(token)
```

`Context` 정의 바로 뒤에 화면 좌표를 넣는다.

```python
@dataclass(frozen=True)
class BrowseView:
    """탐색 화면 한 장을 가리키는 좌표."""

    rel: str
    page: int = 0
    edit: bool = False
```

`_browse` 를 교체한다.

```python
def _browse(ctx: Context, view: BrowseView) -> SendText:
    """디렉토리 내용을 한 페이지 분량의 버튼 목록으로 만든다."""
    entries = ctx.lister(view.rel)
    total_pages = max(1, (len(entries) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(view.page, total_pages - 1))
    start = page * _PAGE_SIZE

    buttons: list[tuple[str, str]] = []
    parent = parent_rel(view.rel)
    if parent is not None:
        # 몇 페이지에 있든 되돌아갈 수 있어야 한다
        buttons.append(("⬆️ 상위", ctx.tokens.put("dir", parent)))

    buttons += [
        _entry_button(entry, ctx.tokens)
        for entry in entries[start : start + _PAGE_SIZE]
    ]
    buttons += _pager_buttons(ctx, view.rel, PageState(page, total_pages))

    title = f"📂 /{view.rel}" if view.rel else "📂 저장소"
    if not entries:
        title = f"{title}\n(비어 있습니다)"
    return SendText(text=title, buttons=tuple(buttons), edit=view.edit)
```

`_browse` 바로 위에 페이지 버튼을 만드는 조각을 둔다.

```python
@dataclass(frozen=True)
class PageState:
    """현재 페이지와 전체 페이지 수."""

    page: int
    total: int


def _pager_buttons(
    ctx: Context, rel: str, state: PageState
) -> list[tuple[str, str]]:
    """페이지 이동 버튼을 만든다. 한 페이지뿐이면 아무것도 만들지 않는다."""
    if state.total <= 1:
        return []

    buttons: list[tuple[str, str]] = []
    if state.page > 0:
        buttons.append(("◀ 이전", ctx.tokens.put("dir", rel, state.page - 1)))
    buttons.append(
        (f"{state.page + 1}/{state.total}", ctx.tokens.put("dir", rel, state.page))
    )
    if state.page < state.total - 1:
        buttons.append(("다음 ▶", ctx.tokens.put("dir", rel, state.page + 1)))
    return buttons
```

호출부 두 곳을 고친다. `_handle_message` 의 `/start`·`/ls` 분기:

```python
    if command in ("/start", "/ls"):
        return _browse(ctx, BrowseView(rel=""))
```

`_handle_callback`:

```python
    kind, rel, page = resolved
    if kind == "dir":
        return _browse(ctx, BrowseView(rel=rel, page=page, edit=True))
    return SendFile(rel=rel, caption=rel.rsplit("/", 1)[-1])
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_handlers.py -v`
Expected: PASS (19 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/handlers.py tests/test_handlers.py
git commit -m "feat: 목록을 페이지로 나눠 큰 폴더도 탐색 가능하게"
```

---

### Task 2: 전달 실패를 사용자에게 알림

**Files:**
- Modify: `src/private_sync/bot/main.py` (`_DELIVERY_FAILED_MESSAGE`, `Deliverer._send_text`)
- Test: `tests/test_bot_main.py`

**Interfaces:**
- Consumes: `Deliverer.notify(text: str) -> None` (기존)
- Produces: `_DELIVERY_FAILED_MESSAGE: str`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_bot_main.py` 의 `_SpyClient.edit_message_text` 에 실패 주입을 더한다.

```python
    def edit_message_text(self, chat_id, message_id, text, buttons=()):
        if self._fail_on == "edit":
            raise TelegramError("editMessageText returned status 400")
        self.edits.append((chat_id, message_id, text, buttons))
```

그리고 아래 두 테스트를 `_start_update` 바로 앞에 넣는다.

```python
def test_failed_edit_tells_the_user_instead_of_going_silent(config):
    client = _SpyClient(fail_on="edit")

    Deliverer(client, config).run(
        SendText(text="목록", buttons=(("A", "t"),), edit=True), _callback()
    )

    # 로그만 남기면 폰에서는 "눌러도 아무 반응 없음" 으로 보인다
    assert client.messages
    assert "표시할 수 없습니다" in client.messages[-1][1]


def test_failure_notice_that_also_fails_stays_quiet(config):
    client = _SpyClient(fail_on="all")

    # 알림마저 실패해도 예외가 밖으로 나오면 안 된다
    Deliverer(client, config).run(
        SendText(text="목록", buttons=(("A", "t"),), edit=True), _callback()
    )

    # 빈 messages 만 보면 "알림을 아예 안 보낸 것" 과 구분되지 않는다.
    # 시도했는지를 함께 본다.
    assert client.messages == []
    assert any("표시할 수 없습니다" in text for text in client.attempts)
```

`_SpyClient.__init__` 에 `self.attempts = []` 를 더하고, `send_message` 가 실패하더라도
시도 자체는 기록하게 한다. 성공한 전송만 보면 "보내려다 실패한 것" 과 "아예 안 보낸 것" 을
구분할 수 없다.

```python
    def send_message(self, chat_id, text, buttons=()):
        # 실패하더라도 시도했다는 사실은 남긴다
        self.attempts.append(text)
        if self._fail_on in ("message", "all"):
            raise TelegramError("sendMessage returned status 500")
        self.messages.append((chat_id, text, buttons))
```

`edit_message_text` 도 마찬가지로 `("edit", "all")` 로 넓힌다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_bot_main.py -v`
Expected: FAIL — `assert client.messages` 가 빈 리스트라 실패

- [ ] **Step 3: 구현**

`bot/main.py` 상수에 추가한다.

```python
_DELIVERY_FAILED_MESSAGE = (
    "목록을 표시할 수 없습니다. 항목이 너무 많거나 일시적인 오류입니다.\n"
    "/find <키워드> 로 찾아보세요."
)
```

`_send_text` 의 except 절에서 안내를 보낸다.

```python
        except TelegramError as exc:
            logger.error("Failed to deliver text response: %s", exc)
            # 침묵하면 사용자에게는 버튼이 죽은 것으로 보인다. notify 는 자체적으로
            # TelegramError 를 삼키므로 여기서 다시 감싸지 않는다.
            self.notify(_DELIVERY_FAILED_MESSAGE)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_bot_main.py -v`
Expected: PASS (17 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/main.py tests/test_bot_main.py
git commit -m "feat: 전달 실패 시 사용자에게 안내"
```

---

### Task 3: API 베이스 URL 설정과 시작 시 확인

**Files:**
- Modify: `src/private_sync/config.py` (`BotConfig`, `load_bot_config`)
- Modify: `src/private_sync/bot/telegram.py` (`_DEFAULT_API_BASE`, `TelegramClient.__init__`, `_url`, `get_me`)
- Modify: `src/private_sync/bot/main.py` (`main` 의 시작 확인)
- Test: `tests/test_config.py`, `tests/test_telegram.py`, `tests/test_bot_main.py`

**Interfaces:**
- Consumes: `private_sync.errors.TelegramError`, `private_sync.errors.ConfigError`
- Produces: `telegram._DEFAULT_API_BASE: str`, `TelegramClient(token: str, session=None, api_base: str = _DEFAULT_API_BASE)`, `TelegramClient.get_me() -> dict`, `BotConfig` 에 필드 `api_base: str` 과 `max_part_bytes: int` 추가

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_config.py` 에 추가한다.

```python
def test_bot_config_defaults_to_the_public_api(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    conf = load_bot_config(
        cfg,
        env={
            "PRIVATE_SYNC_BOT_TOKEN": "tok",
            "PRIVATE_SYNC_CHAT_ID": "123",
            "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
        },
    )

    # 미설정이면 지금과 동일하게 동작해야 한다
    assert conf.api_base == "https://api.telegram.org"
    assert conf.max_part_bytes == 45 * 1024 * 1024


def test_bot_config_reads_local_api_settings(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    conf = load_bot_config(
        cfg,
        env={
            "PRIVATE_SYNC_BOT_TOKEN": "tok",
            "PRIVATE_SYNC_CHAT_ID": "123",
            "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
            "PRIVATE_SYNC_API_BASE": "http://127.0.0.1:8081/",
            "PRIVATE_SYNC_MAX_PART_MB": "1900",
        },
    )

    # 끝의 슬래시는 URL 조립에서 중복되므로 떼어둔다
    assert conf.api_base == "http://127.0.0.1:8081"
    assert conf.max_part_bytes == 1900 * 1024 * 1024


def test_bot_config_rejects_non_numeric_part_size(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    # "②" 는 str.isdigit() 이 True 지만 int() 가 거부한다. 0 과 음수도 함께 본다.
    for bad in ("많이", "②", "0", "-5"):
        with pytest.raises(ConfigError, match="PRIVATE_SYNC_MAX_PART_MB"):
            load_bot_config(
                cfg,
                env={
                    "PRIVATE_SYNC_BOT_TOKEN": "tok",
                    "PRIVATE_SYNC_CHAT_ID": "123",
                    "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
                    "PRIVATE_SYNC_MAX_PART_MB": bad,
                },
            )
```

`tests/test_telegram.py` 에 추가한다.

```python
def test_default_base_builds_the_public_url():
    session = _FakeSession()
    client = TelegramClient("tok", session=session)

    client.get_updates(offset=None)

    _, url, _ = session.calls[0]
    assert url == "https://api.telegram.org/bottok/getUpdates"


def test_custom_base_is_used_verbatim():
    session = _FakeSession()
    client = TelegramClient("tok", session=session, api_base="http://127.0.0.1:8081")

    client.get_updates(offset=None)

    _, url, _ = session.calls[0]
    assert url == "http://127.0.0.1:8081/bottok/getUpdates"


def test_get_me_returns_the_result_payload():
    session = _FakeSession(
        _FakeResponse({"ok": True, "result": {"username": "mybot"}})
    )
    client = TelegramClient("tok", session=session)

    assert client.get_me() == {"username": "mybot"}
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_config.py tests/test_telegram.py -v`
Expected: FAIL — `AttributeError: 'BotConfig' object has no attribute 'api_base'`

- [ ] **Step 3: 구현**

`config.py` 의 `BotConfig` 에 필드를 더한다.

```python
@dataclass(frozen=True)
class BotConfig:
    """서버 bot 설정. 비밀값은 환경변수에서 온다."""

    store: Path
    token: str
    chat_id: str
    zip_password: str
    api_base: str
    max_part_bytes: int
```

`load_bot_config` 의 반환 직전에 두 값을 읽는다. 비밀값 검사 뒤에 놓는다.

```python
    api_base = env.get("PRIVATE_SYNC_API_BASE", "https://api.telegram.org").rstrip("/")

    # str.isdigit() 은 "②" 같은 유니코드 숫자에도 True 라 int() 가 뒤에서 터진다.
    # 시작 경로는 ConfigError 만 잡으므로 그대로 두면 트레이스백과 함께 죽는다.
    raw_part_mb = env.get("PRIVATE_SYNC_MAX_PART_MB", "45")
    try:
        part_mb = int(raw_part_mb)
    except ValueError:
        part_mb = 0

    if part_mb <= 0:
        raise ConfigError(
            f"PRIVATE_SYNC_MAX_PART_MB must be a positive integer, got {raw_part_mb!r}"
        )

    return BotConfig(
        store=store,
        token=secrets["PRIVATE_SYNC_BOT_TOKEN"],
        chat_id=secrets["PRIVATE_SYNC_CHAT_ID"],
        zip_password=secrets["PRIVATE_SYNC_ZIP_PASSWORD"],
        api_base=api_base,
        max_part_bytes=part_mb * 1024 * 1024,
    )
```

`telegram.py` 에서 `_API` 상수를 베이스와 분리한다.

```python
_DEFAULT_API_BASE = "https://api.telegram.org"
```

`_API` 상수는 제거하고 `TelegramClient` 를 고친다.

```python
    def __init__(
        self,
        token: str,
        session: requests.Session | None = None,
        api_base: str = _DEFAULT_API_BASE,
    ) -> None:
        self._token = token
        self._session = session or requests.Session()
        self._api_base = api_base

    def _url(self, method: str) -> str:
        """메서드 호출 URL을 만든다. 이 문자열은 어떤 예외·로그에도 넣지 않는다."""
        return f"{self._api_base}/bot{self._token}/{method}"
```

`get_updates` 아래에 추가한다.

```python
    def get_me(self) -> dict:
        """봇 자신의 정보를 가져온다. 시작 시 API 도달 확인에 쓴다.

        Raises:
            TelegramError: 네트워크 오류 또는 비정상 응답.
        """
        response = self._get("getMe", {}, _POST_TIMEOUT_SEC)
        result = response.get("result")
        if not isinstance(result, dict):
            raise TelegramError("getMe returned unexpected payload")
        return result
```

`bot/main.py` 의 `main()` 에서 클라이언트를 만든 뒤 도달 확인을 넣는다.

```python
    client = TelegramClient(config.token, api_base=config.api_base)
    try:
        client.get_me()
    except TelegramError as exc:
        # 로컬 API 서버를 쓰는 경우 그것이 죽어 있으면 봇이 통째로 먹통이 된다.
        # 조용히 도는 것보다 시작 시 분명히 멈추는 편이 낫다.
        logger.critical("Cannot reach the Telegram API at %s: %s", config.api_base, exc)
        return 1
```

`tests/test_bot_main.py` 에는 `main()` 을 부르는 테스트가 없으므로 이 분기는 수동 확인 대상이다. Step 6 에서 다룬다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest -q`
Expected: 160 passed

- [ ] **Step 5: config.example.yaml 과 README 갱신**

`config.example.yaml` 의 서버 주석 블록에 두 변수를 더한다.

```
#   PRIVATE_SYNC_API_BASE       (선택) 로컬 Bot API 서버 주소. 미설정 시 공개 API
#   PRIVATE_SYNC_MAX_PART_MB    (선택) 분할 단위 MB. 미설정 시 45
```

README 서버 설정 절에 로컬 Bot API 서버 항목을 더한다. 표준 API만 쓸 거면 건너뛰어도 된다는 점, `api_id`/`api_hash` 는 `my.telegram.org` 에서 받고 봇 토큰과 다른 종류의 비밀값이라는 점, 로컬 서버가 죽으면 봇도 못 뜬다는 점을 적는다.

- [ ] **Step 6: 시작 확인 분기를 수동으로 검증**

도달 불가능한 베이스를 주고 `main()` 이 1을 반환하는지 본다. 네트워크에 나가지 않는 주소를 쓴다.

```bash
mkdir -p /tmp/apibase && printf 'store: /tmp/apibase\n' > /tmp/apibase/bot.yaml
PRIVATE_SYNC_BOT_TOKEN=x PRIVATE_SYNC_CHAT_ID=1 PRIVATE_SYNC_ZIP_PASSWORD=p \
PRIVATE_SYNC_API_BASE=http://127.0.0.1:9 \
.venv/bin/private-sync-bot --config /tmp/apibase/bot.yaml --debug; echo "exit=$?"
```

Expected: `CRITICAL Cannot reach the Telegram API at http://127.0.0.1:9` 와 `exit=1`

정리: `rm -r /tmp/apibase`

- [ ] **Step 7: 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/config.py src/private_sync/bot/telegram.py \
        src/private_sync/bot/main.py tests/ config.example.yaml README.md
git commit -m "feat: API 베이스 URL 설정과 시작 시 도달 확인"
```

---

### Task 4: 로컬 경로 업로드

**Files:**
- Modify: `src/private_sync/bot/telegram.py` (`send_document`)
- Modify: `src/private_sync/bot/main.py` (`Deliverer` 가 `max_part_bytes` 를 쓰도록)
- Test: `tests/test_telegram.py`, `tests/test_bot_main.py`

**Interfaces:**
- Consumes: `TelegramClient._api_base`, `_DEFAULT_API_BASE`, `BotConfig.max_part_bytes`
- Produces: 없음 (기존 `send_document` 시그니처 유지)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_telegram.py` 에 추가한다.

```python
def test_public_api_uploads_the_file_body(tmp_path):
    document = tmp_path / "a.zip"
    document.write_bytes(b"zip")
    session = _FakeSession()
    client = TelegramClient("tok", session=session)

    client.send_document("123", document, caption="a")

    _, _, data = session.calls[0]
    assert "document" not in data


def test_local_api_sends_the_path_instead_of_the_body(tmp_path):
    document = tmp_path / "a.zip"
    document.write_bytes(b"zip")
    session = _FakeSession()
    client = TelegramClient("tok", session=session, api_base="http://127.0.0.1:8081")

    client.send_document("123", document, caption="a")

    _, _, data = session.calls[0]
    # 같은 장비이므로 본문을 HTTP 로 밀어 넣을 이유가 없다
    assert data["document"] == f"file://{document}"


def test_local_api_upload_does_not_open_the_file(tmp_path):
    missing = tmp_path / "없는파일.zip"
    session = _FakeSession()
    client = TelegramClient("tok", session=session, api_base="http://127.0.0.1:8081")

    # 파일을 읽지 않으므로 존재하지 않아도 요청은 나간다. 판단은 서버가 한다.
    client.send_document("123", missing, caption="a")

    assert session.calls
```

`_FakeSession.post` 가 `files` 를 기록하도록 시그니처를 확인한다. 이미 `files=None` 을 받으므로 변경은 필요 없다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_telegram.py -v`
Expected: FAIL — `KeyError: 'document'`

- [ ] **Step 3: 구현**

`telegram.py` 의 `send_document` 를 교체한다.

```python
    def send_document(self, chat_id: str, path: Path, caption: str = "") -> None:
        """파일을 문서로 보낸다.

        로컬 Bot API 서버를 쓸 때는 본문을 HTTP 로 싣지 않고 경로만 넘긴다. API
        서버가 같은 장비에서 파일을 직접 읽으므로 큰 파일에서 복사 한 번이 사라진다.

        Raises:
            TelegramError: 파일을 열 수 없거나 전송이 실패했을 때.
        """
        data: dict[str, object] = {"chat_id": chat_id, "caption": caption}

        if self._api_base != _DEFAULT_API_BASE:
            data["document"] = f"file://{path}"
            self._post("sendDocument", _PostBody(data=data, timeout=_UPLOAD_TIMEOUT_SEC))
            return

        try:
            with path.open("rb") as handle:
                self._post(
                    "sendDocument",
                    _PostBody(
                        data=data,
                        files={"document": (path.name, handle)},
                        timeout=_UPLOAD_TIMEOUT_SEC,
                    ),
                )
        except OSError as exc:
            # OSError 에는 토큰이 없으므로 체인을 유지해 디버깅 정보를 남긴다
            raise TelegramError(
                f"cannot read document {path.name}: {exc.strerror}"
            ) from exc
```

`bot/main.py` 의 `_send_file` 이 설정된 분할 단위를 쓰게 한다.

```python
            parts = pack_for_send(
                source, workdir, self._config.zip_password, self._config.max_part_bytes
            )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest -q`
Expected: 163 passed

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/telegram.py src/private_sync/bot/main.py tests/
git commit -m "feat: 로컬 API 서버에서 파일 경로로 업로드"
```

---

### Task 5: 폴더 크기 계산과 확인 화면

**Files:**
- Modify: `src/private_sync/bot/store.py` (`DirStats`, `directory_stats`)
- Modify: `src/private_sync/bot/handlers.py` (`SendFolder`, `Context.stats`, `_browse` 의 압축 버튼, `_confirm_folder`, `_handle_callback`)
- Modify: `src/private_sync/bot/main.py` (`_build_context` 에 `stats` 주입)
- Test: `tests/test_store.py`, `tests/test_handlers.py`

**Interfaces:**
- Consumes: `store._resolves_inside`, `store.resolve_safe`
- Produces: `store.DirStats(files: int, total_bytes: int)` (frozen), `store.directory_stats(root: Path, rel: str) -> DirStats`, `handlers.SendFolder(rel: str)` (frozen), `Context.stats: Callable[[str], DirStats]`, `Action` 에 `SendFolder` 추가

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_store.py` 에 추가한다.

```python
def test_directory_stats_counts_files_and_bytes(store):
    stats = directory_stats(store, "SKT 문서")

    # 계약서.docx(1) + sub/회의록.md(1)
    assert stats.files == 2
    assert stats.total_bytes == 2


def test_directory_stats_skips_escaping_symlinks(store, tmp_path):
    outside = tmp_path / "big.bin"
    outside.write_bytes(b"x" * 500)
    (store / "메모" / "link.bin").symlink_to(outside)

    stats = directory_stats(store, "메모")

    # 저장소 밖 파일은 크기에도 개수에도 들어가면 안 된다
    assert stats.files == 1
    assert stats.total_bytes == 1


def test_directory_stats_on_empty_directory(store):
    (store / "빈폴더").mkdir()

    stats = directory_stats(store, "빈폴더")

    assert stats == DirStats(files=0, total_bytes=0)
```

`tests/test_handlers.py` 에 추가한다. `_ctx` 헬퍼에 `stats` 주입을 더한다.

```python
def _ctx(chat_id="123", listing=None, results=None, stats=None):
    tree = {"": ROOT, "SKT 문서": SKT} if listing is None else listing
    return Context(
        chat_id=chat_id,
        tokens=TokenMap(),
        lister=lambda rel: tree.get(rel, []),
        searcher=lambda kw: results or [],
        stats=stats or (lambda rel: DirStats(files=3, total_bytes=1024)),
    )
```

```python
def test_directory_view_offers_a_folder_download():
    ctx = _ctx()

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "SKT 문서"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert any(label.startswith("📦") for label, _ in action.buttons)


def test_root_view_has_no_folder_download():
    action = handle(_message("/start"), _ctx())

    # 저장소 전체를 통째로 받는 것은 의도한 동작이 아니다
    assert not any(label.startswith("📦") for label, _ in action.buttons)


def test_folder_download_asks_before_starting():
    ctx = _ctx(stats=lambda rel: DirStats(files=100, total_bytes=843 * 1024 * 1024))

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("zipask", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert isinstance(action, SendText)
    assert "843.0 MB" in action.text
    assert "100" in action.text
    labels = [label for label, _ in action.buttons]
    assert "받기" in labels
    assert "취소" in labels


def test_confirming_starts_the_folder_download():
    ctx = _ctx()

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("zipgo", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert action == SendFolder(rel="음악")


def test_cancelling_returns_to_the_listing():
    ctx = _ctx(listing={"음악": []})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert isinstance(action, SendText)
    assert action.edit is True
```

임포트에 `SendFolder` 와 `from private_sync.bot.store import DirStats, Entry` 를 더한다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_store.py tests/test_handlers.py -v`
Expected: FAIL — `ImportError: cannot import name 'DirStats'`

- [ ] **Step 3: store.py 에 크기 계산 추가**

`Entry` 정의 뒤에 넣는다.

```python
@dataclass(frozen=True)
class DirStats:
    """디렉토리 하나의 크기 요약."""

    files: int
    total_bytes: int
```

`parent_rel` 앞에 넣는다.

```python
def directory_stats(root: Path, rel: str) -> DirStats:
    """디렉토리 하위의 파일 수와 총 바이트를 센다.

    저장소 밖으로 풀리는 심볼릭 링크와 상태를 읽을 수 없는 항목은 제외한다.
    목록·검색과 같은 규칙이라 확인 화면의 숫자와 실제 전송 내용이 어긋나지 않는다.

    Raises:
        StoreError: 경로가 루트를 벗어나거나 디렉토리가 아닐 때.
    """
    base = root.resolve()
    target = resolve_safe(root, rel)
    if not target.is_dir():
        raise StoreError(f"path {rel!r} is not a directory")

    files = 0
    total = 0
    for path in target.rglob("*"):
        if not _resolves_inside(base, path):
            continue
        try:
            if not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            logger.warning("Skipping unreadable entry while sizing %s", rel)
            continue
        files += 1

    return DirStats(files=files, total_bytes=total)
```

- [ ] **Step 4: handlers.py 에 확인 화면 추가**

`SendFile` 정의 뒤에 넣는다.

```python
@dataclass(frozen=True)
class SendFolder:
    """폴더를 통째로 압축해 보내라는 지시."""

    rel: str
```

`Action` 별칭을 넓힌다.

```python
Action = SendText | SendFile | SendFolder | None
```

`Context` 에 필드를 더한다.

```python
    stats: Callable[[str], DirStats]
```

임포트에 `DirStats` 를 더한다: `from private_sync.bot.store import DirStats, Entry, parent_rel`

`_browse` 의 버튼 조립에서 상위 버튼 다음에 압축 버튼을 넣는다. 루트에서는 넣지 않는다.

```python
    if view.rel:
        buttons.append(
            ("📦 이 폴더 통째로 받기", ctx.tokens.put("zipask", view.rel))
        )
```

`_find` 앞에 확인 화면을 만든다.

```python
def _confirm_folder(ctx: Context, rel: str) -> SendText:
    """폴더 압축 전에 크기를 보여주고 확인을 받는다.

    843MB 짜리 전송을 실수로 시작하면 되돌릴 수 없다.
    """
    stats = ctx.stats(rel)
    return SendText(
        text=(
            f"📦 /{rel}\n"
            f"파일 {stats.files}개, {format_size(stats.total_bytes)}\n"
            "압축해서 보낼까요?"
        ),
        buttons=(
            ("받기", ctx.tokens.put("zipgo", rel)),
            ("취소", ctx.tokens.put("dir", rel)),
        ),
        edit=True,
    )
```

`_handle_callback` 의 분기를 넓힌다.

```python
    kind, rel, page = resolved
    if kind == "dir":
        return _browse(ctx, BrowseView(rel=rel, page=page, edit=True))
    if kind == "zipask":
        return _confirm_folder(ctx, rel)
    if kind == "zipgo":
        return SendFolder(rel=rel)
    return SendFile(rel=rel, caption=rel.rsplit("/", 1)[-1])
```

`bot/main.py` 의 `_build_context` 에 주입을 더한다.

```python
def _build_context(config: BotConfig, tokens: TokenMap) -> Context:
    """저장소 조회를 주입한 핸들러 컨텍스트를 만든다."""
    return Context(
        chat_id=config.chat_id,
        tokens=tokens,
        lister=lambda rel: store.list_dir(config.store, rel),
        searcher=lambda keyword: store.search(config.store, keyword),
        stats=lambda rel: store.directory_stats(config.store, rel),
    )
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/pytest -q`
Expected: 171 passed

`Deliverer.run` 이 아직 `SendFolder` 를 모르므로 이 시점에는 폴더 버튼을 눌러도 아무 일도 하지 않는다. Task 6 에서 잇는다.

- [ ] **Step 6: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/store.py src/private_sync/bot/handlers.py \
        src/private_sync/bot/main.py tests/
git commit -m "feat: 폴더 크기 확인 화면 추가"
```

---

### Task 6: 폴더 압축 전송

**Files:**
- Modify: `src/private_sync/bot/packer.py` (`pack_dir_for_send`)
- Modify: `src/private_sync/bot/main.py` (`Deliverer._send_folder`, `run` 의 분기)
- Test: `tests/test_packer.py`, `tests/test_bot_main.py`

**Interfaces:**
- Consumes: `packer.split_file`, `store.resolve_safe`, `store.directory_stats`, `handlers.SendFolder`
- Produces: `packer.pack_dir_for_send(src_dir: Path, dest_dir: Path, password: str, max_bytes: int = MAX_PART_BYTES) -> list[Path]`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_packer.py` 에 추가한다.

```python
def test_directory_zip_keeps_relative_structure(tmp_path):
    src = tmp_path / "음악"
    (src / "하위").mkdir(parents=True)
    (src / "a.mp3").write_bytes(b"aaa")
    (src / "하위" / "b.mp3").write_bytes(b"bbb")
    dest = tmp_path / "out"
    dest.mkdir()

    parts = pack_dir_for_send(src, dest, password="pw", max_bytes=1024 * 1024)

    assert len(parts) == 1
    with pyzipper.AESZipFile(parts[0]) as zf:
        zf.setpassword(b"pw")
        assert sorted(zf.namelist()) == ["음악/a.mp3", "음악/하위/b.mp3"]
        assert zf.read("음악/하위/b.mp3") == b"bbb"


def test_directory_zip_splits_when_over_limit(tmp_path):
    src = tmp_path / "음악"
    src.mkdir()
    (src / "big.bin").write_bytes(bytes(range(256)) * 40)
    dest = tmp_path / "out"
    dest.mkdir()

    parts = pack_dir_for_send(src, dest, password="pw", max_bytes=512)

    assert len(parts) > 1
    assert all(p.stat().st_size <= 512 for p in parts)


def test_directory_zip_rejects_a_file(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"a")
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(PackError, match="not a directory"):
        pack_dir_for_send(src, dest, password="pw")
```

`tests/test_bot_main.py` 에 추가한다.

```python
def test_folder_download_reports_progress_and_sends(config, monkeypatch):
    import private_sync.bot.main as bot_main

    (config.store / "메모" / "하위").mkdir(parents=True, exist_ok=True)
    (config.store / "메모" / "하위" / "b.txt").write_bytes(b"bb")
    client = _SpyClient()

    Deliverer(client, config).run(SendFolder(rel="메모"), _callback())

    assert len(client.documents) == 1
    progress = [text for _chat, message_id, text, _b in client.edits]
    assert any("압축 중" in text for text in progress)
    assert any("전송 중" in text for text in progress)
    assert any("완료" in text for text in progress)
    assert bot_main is not None


def test_folder_download_refuses_when_disk_is_short(config, monkeypatch):
    import private_sync.bot.main as bot_main

    # shutil._ntuple_diskusage 는 private API 다. 필요한 필드만 흉내낸다.
    Usage = namedtuple("Usage", "total used free")

    def tiny_disk(_path):
        return Usage(total=100, used=99, free=1)

    monkeypatch.setattr(bot_main.shutil, "disk_usage", tiny_disk)
    client = _SpyClient()

    Deliverer(client, config).run(SendFolder(rel="메모"), _callback())

    assert client.documents == []
    assert "공간" in client.messages[-1][1]


def test_folder_download_cleans_up_on_pack_failure(config, monkeypatch):
    import private_sync.bot.main as bot_main

    created = []
    original = bot_main.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = original(*args, **kwargs)
        created.append(Path(path))
        return path

    def exploding_pack(*_args, **_kwargs):
        raise PackError("cannot read source directory")

    monkeypatch.setattr(bot_main.tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(bot_main, "pack_dir_for_send", exploding_pack)
    client = _SpyClient()

    Deliverer(client, config).run(SendFolder(rel="메모"), _callback())

    assert created and not created[0].exists()
    assert "포장" in client.messages[-1][1]
```

임포트에 `import shutil`, `from collections import namedtuple`, `from private_sync.bot.handlers import SendFolder`, `from private_sync.errors import PackError` 를 더한다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_packer.py tests/test_bot_main.py -v`
Expected: FAIL — `ImportError: cannot import name 'pack_dir_for_send'`

- [ ] **Step 3: packer.py 에 디렉토리 압축 추가**

`pack_for_send` 뒤에 넣는다.

```python
def _make_encrypted_dir_zip(src_dir: Path, dest_dir: Path, password: str) -> Path:
    """디렉토리 하위 전체를 AES-256 암호 ZIP으로 포장한다.

    아카이브 안의 경로는 대상 디렉토리 이름부터 시작하므로, 풀면 폴더 하나가
    통째로 나온다.

    Raises:
        PackError: 읽기·쓰기에 실패했을 때.
    """
    archive = dest_dir / (src_dir.name + ".zip")
    try:
        with pyzipper.AESZipFile(
            archive,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            for path in sorted(src_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, arcname=str(Path(src_dir.name) / path.relative_to(src_dir)))
    except OSError as exc:
        archive.unlink(missing_ok=True)
        raise PackError(
            f"cannot read or write while packing {src_dir.name}: {exc.strerror}"
        ) from exc

    logger.info("Packed directory %s into %d bytes", src_dir.name, archive.stat().st_size)
    return archive


def pack_dir_for_send(
    src_dir: Path,
    dest_dir: Path,
    password: str,
    max_bytes: int = MAX_PART_BYTES,
) -> list[Path]:
    """폴더를 압축해 전송할 파일 목록을 만든다.

    Raises:
        PackError: 대상이 디렉토리가 아니거나 포장에 실패했을 때.
    """
    if not src_dir.is_dir():
        raise PackError(f"source {src_dir.name} is not a directory")

    archive = _make_encrypted_dir_zip(src_dir, dest_dir, password)
    if archive.stat().st_size <= max_bytes:
        return [archive]
    return split_file(archive, max_bytes)
```

- [ ] **Step 4: bot/main.py 에 전송 흐름 추가**

상수에 추가한다.

```python
_DISK_HEADROOM = 1.1
_NO_SPACE_MESSAGE = "서버에 압축할 공간이 부족합니다. 관리자에게 문의하세요."
```

임포트에 `pack_dir_for_send` 와 `SendFolder` 를 더한다.

`run` 의 분기를 넓힌다.

```python
        if isinstance(action, SendText):
            self._send_text(action, incoming)
            return
        if isinstance(action, SendFolder):
            self._send_folder(action, incoming)
            return

        self._send_file(action, incoming)
```

`_send_file` 뒤에 넣는다.

```python
    def _progress(self, incoming: Incoming, text: str) -> None:
        """같은 메시지를 고쳐 진행 상황을 알린다. 새 메시지를 쌓지 않는다."""
        if incoming.message_id is None:
            self.notify(text)
            return
        try:
            self._client.edit_message_text(
                self._config.chat_id, incoming.message_id, text
            )
        except TelegramError as exc:
            logger.warning("Progress update failed: %s", exc)

    def _send_folder(self, action: SendFolder, incoming: Incoming) -> None:
        """폴더를 압축해 보낸다. 진행 상황을 메시지로 갱신한다."""
        try:
            source = store.resolve_safe(self._config.store, action.rel)
            stats = store.directory_stats(self._config.store, action.rel)
        except StoreError as exc:
            logger.warning("Rejected folder request %r: %s", action.rel, exc)
            self.notify(_MISSING_FILE_MESSAGE)
            return

        workdir = Path(tempfile.mkdtemp(prefix="private-sync-"))
        sent = 0
        try:
            if shutil.disk_usage(workdir).free < stats.total_bytes * _DISK_HEADROOM:
                logger.error("Not enough disk space to pack %s", action.rel)
                self.notify(_NO_SPACE_MESSAGE)
                return

            self._progress(incoming, f"압축 중… ({format_size(stats.total_bytes)})")
            parts = pack_dir_for_send(
                source, workdir, self._config.zip_password, self._config.max_part_bytes
            )

            self._progress(incoming, f"전송 중… (파트 {len(parts)}개)")
            for part in parts:
                self._client.send_document(
                    self._config.chat_id, part, caption=part.name
                )
                sent += 1

            if len(parts) > 1:
                archive = source.name + ".zip"
                self.notify(_SPLIT_NOTICE.format(count=len(parts), archive=archive))
            self._progress(incoming, f"완료 ({format_size(stats.total_bytes)})")
            logger.info("Delivered folder %s as %d part(s)", action.rel, len(parts))
        except PackError as exc:
            logger.error("Packing failed for folder %s: %s", action.rel, exc)
            self.notify("폴더를 포장하는 중 오류가 발생했습니다.")
        except (TelegramError, OSError) as exc:
            logger.error(
                "Sending failed for folder %s after %d part(s): %s",
                action.rel,
                sent,
                type(exc).__name__,
            )
            if sent:
                self.notify(_PARTIAL_SEND_MESSAGE.format(sent=sent, total=len(parts)))
            else:
                self.notify("파일 전송에 실패했습니다. 다시 시도해 주세요.")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
```

`format_size` 를 `handlers` 에서 가져온다: `from private_sync.bot.handlers import format_size` 를 기존 임포트에 더한다.

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/pytest -q`
Expected: 177 passed

- [ ] **Step 6: 실제 저장소로 확인**

가짜 텔레그램 클라이언트로 전체 흐름을 돌린다. 네트워크에 나가지 않는다.

```bash
.venv/bin/python - <<'PY'
import tempfile, pathlib, pyzipper
from private_sync.bot.main import Deliverer, _build_context, _handle_one
from private_sync.bot.handlers import TokenMap
from private_sync.config import BotConfig

store = pathlib.Path(tempfile.mkdtemp()) / "store"
(store / "음악" / "하위").mkdir(parents=True)
for i in range(45):
    (store / "음악" / f"{i:03d}.mp3").write_bytes(b"x" * 100)
(store / "음악" / "하위" / "note.txt").write_bytes(b"note")

cfg = BotConfig(store=store, token="t", chat_id="1", zip_password="pw",
                api_base="https://api.telegram.org", max_part_bytes=45 * 1024 * 1024)
sent, edits = [], []
class C:
    def send_message(self, c, t, buttons=()): sent.append(("msg", t, buttons))
    def edit_message_text(self, c, m, t, buttons=()): edits.append(t); sent.append(("edit", t, buttons))
    def send_document(self, c, p, caption=""): sent.append(("doc", pathlib.Path(p).name, pathlib.Path(p).read_bytes()))
    def answer_callback(self, i): pass

tokens = TokenMap(); ctx = _build_context(cfg, tokens); d = Deliverer(C(), cfg)
_handle_one({"message": {"text": "/start", "chat": {"id": 1}, "message_id": 1}}, ctx, d)
tok = [dd for ll, dd in sent[-1][2] if "음악" in ll][0]
_handle_one({"callback_query": {"id": "c", "data": tok, "message": {"chat": {"id": 1}, "message_id": 2}}}, ctx, d)
labels = [l for l, _ in sent[-1][2]]
print("  페이지 버튼:", [l for l in labels if "/" in l or "▶" in l or "◀" in l])
print("  압축 버튼:", [l for l in labels if l.startswith("📦")])
ask = [dd for ll, dd in sent[-1][2] if ll.startswith("📦")][0]
_handle_one({"callback_query": {"id": "c", "data": ask, "message": {"chat": {"id": 1}, "message_id": 3}}}, ctx, d)
print("  확인 화면:", sent[-1][1].replace("\n", " / "))
go = [dd for ll, dd in sent[-1][2] if ll == "받기"][0]
_handle_one({"callback_query": {"id": "c", "data": go, "message": {"chat": {"id": 1}, "message_id": 4}}}, ctx, d)
print("  진행 표시:", edits[-3:])
kind, name, blob = sent[-1]
tmp = pathlib.Path(tempfile.mkdtemp()) / "a.zip"; tmp.write_bytes(blob)
with pyzipper.AESZipFile(tmp) as zf:
    zf.setpassword(b"pw")
    names = zf.namelist()
    print(f"  받은 파일: {name} | 항목 {len(names)}개 | 첫 항목 {names[0]}")
PY
```

Expected: 페이지 버튼과 압축 버튼이 보이고, 확인 화면에 파일 46개와 크기가 나오며, 진행 표시가 `압축 중…` → `전송 중…` → `완료` 로 이어지고, 받은 ZIP 안에 `음악/` 로 시작하는 항목 46개가 들어 있다.

- [ ] **Step 7: README 갱신과 커밋**

README 의 봇 사용법 절에 폴더 다운로드를 더한다: 폴더 화면의 `📦` 버튼을 누르면 크기를 먼저 보여주고 확인 후 압축해 보낸다는 것, 큰 폴더는 분할되어 오며 PC 에서 `cat` 으로 합쳐야 한다는 것(로컬 API 서버를 쓰면 대개 한 파일로 온다는 것)을 적는다.

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/packer.py src/private_sync/bot/main.py tests/ README.md
git commit -m "feat: 폴더를 통째로 압축해 전송"
```

---

## Self-Review

**1. 스펙 커버리지**

| 스펙 요구사항 | 구현 태스크 |
|---|---|
| 페이지네이션 (20개, 이전/다음, n/N) | Task 1 |
| 모든 페이지에 `⬆️ 상위` | Task 1 |
| `TokenMap` 을 `(kind, rel, page)` 로 확장 | Task 1 |
| `callback_data` 에 경로·페이지 미노출 | Task 1 |
| 전송 실패를 사용자에게 알림 | Task 2 |
| 알림마저 실패하면 로그만 | Task 2 (`notify` 가 이미 삼킴) |
| `PRIVATE_SYNC_API_BASE`, 기본값 공개 API | Task 3 |
| `PRIVATE_SYNC_MAX_PART_MB`, 기본 45 | Task 3 |
| 시작 시 `getMe` 로 도달 확인, 실패 시 종료 | Task 3 |
| 로컬 서버일 때 `file://` 경로 업로드 | Task 4 |
| 기본 베이스면 기존 multipart 유지 | Task 4 |
| `📦 이 폴더 통째로 받기` 버튼 | Task 5 |
| 확인 화면 (크기·개수·받기·취소) | Task 5 |
| 폴더 크기 계산 시 심볼릭 링크 제외 | Task 5 |
| 진행 상황을 메시지 편집으로 갱신 | Task 6 |
| 디스크 여유 1.1배 확인 | Task 6 |
| 임시 zip 을 `finally` 에서 정리 | Task 6 |
| 폴더 압축 시 AES-256 유지 | Task 6 |
| 분할 시 몇 번째에서 끊겼는지 알림 | Task 6 (`_PARTIAL_SEND_MESSAGE` 재사용) |

누락 없음.

**2. 플레이스홀더 스캔**

TBD·TODO·"적절히 처리" 류 표현 없음. 모든 코드 스텝에 실제 코드가, 모든 실행 스텝에 명령과 기대 결과가 있다.

**3. 타입 일관성**

- `TokenMap.put(kind, rel, page=0)` / `get -> (kind, rel, page) | None` 이 Task 1 정의와 Task 5 의 `zipask`/`zipgo` 사용에서 일치
- `BrowseView(rel, page, edit)` 가 Task 1 의 두 호출부에서 동일
- `DirStats(files, total_bytes)` 가 Task 5 의 `store` 정의와 `handlers` 사용, Task 6 의 `_send_folder` 사용에서 동일
- `SendFolder(rel)` 가 Task 5 정의와 Task 6 의 `isinstance` 분기에서 동일
- `pack_dir_for_send(src_dir, dest_dir, password, max_bytes)` 가 Task 6 정의와 호출부에서 동일
- `BotConfig` 의 `api_base`·`max_part_bytes` 가 Task 3 정의와 Task 4·6 사용에서 동일
- `format_size` 는 `handlers` 에 이미 있는 것을 Task 6 이 임포트해 쓴다 — 새로 만들지 않는다

불일치 없음.
