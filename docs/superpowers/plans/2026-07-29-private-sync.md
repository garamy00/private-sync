# private-sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 노트북의 지정된 파일·디렉토리를 사내 DGX 서버로 자동 동기화하고, 텔레그램 봇으로 목록을 탐색해 암호 ZIP으로 내려받는 시스템을 만든다.

**Architecture:** 두 개의 독립 프로세스다. 노트북의 `agent`는 watchdog으로 변경을 감지해 디바운스 후 rsync over SSH로 서버에 올린다. DGX 서버의 `bot`은 텔레그램 `getUpdates` 롱폴링으로 명령을 받아(아웃바운드 연결만 사용) 저장소를 탐색하고, 요청받은 파일을 AES-256 암호 ZIP으로 포장해 전송한다. 둘은 서로 통신하지 않고 서버의 저장소 디렉토리만 공유한다.

**Tech Stack:** Python 3.11+, watchdog, pyzipper, requests, PyYAML, pytest, ruff

**Spec:** [docs/superpowers/specs/2026-07-29-private-sync-design.md](../specs/2026-07-29-private-sync-design.md)

## Global Constraints

모든 태스크의 요구사항에 아래가 암묵적으로 포함된다.

- Python `requires-python = ">=3.11"`. 타입 힌트는 `X | None` 형식을 쓴다 (`Optional[X]` 금지).
- `src/` 레이아웃. 패키지는 `src/private_sync/`, 테스트는 `tests/`.
- ruff `line-length = 88`. 커밋 전 `ruff format` 과 `ruff check` 를 통과해야 한다.
- 모든 public 함수에 타입 힌트와 Google Style docstring을 작성한다. 1줄 docstring은 명령형으로 쓰고 마침표로 끝낸다.
- 로그는 `logging` 모듈만 사용한다. `print()` 금지. 로그 호출은 lazy args를 쓴다: `logger.info("Uploaded %s", path)`. 이때 `%` 연산자를 직접 쓰지 않고 인수로 넘긴다.
- **로그가 아닌 문자열(예외 메시지, 반환 문자열)은 f-string으로 만든다.** `"text %s" % value` 형태의 `%` 연산자는 쓰지 않는다 — ruff `UP031`에 걸린다. 아래 태스크의 코드 예시가 예외 메시지에 `%` 연산자를 쓰고 있으면 같은 문면을 유지한 f-string으로 바꿔 작성하라 (테스트가 매칭하는 메시지 텍스트는 그대로 유지해야 한다).
- `pyproject.toml` 에 `[tool.ruff.lint]` `select` 를 추가하지 않는다. ruff 0.16의 기본 규칙 415개를 그대로 적용한다 (사용자 결정, 2026-07-30).
- **로그 메시지는 영문**, **주석과 docstring은 한국어**로 작성한다.
- 예외는 구체적 타입만 잡는다. bare `except:` 와 `except Exception:` 금지. 체이닝은 `raise NewError(...) from exc`.
- 비밀값(`PRIVATE_SYNC_BOT_TOKEN`, `PRIVATE_SYNC_CHAT_ID`, `PRIVATE_SYNC_ZIP_PASSWORD`)은 환경변수로만 읽는다. YAML·코드·로그에 넣지 않는다.
- 텔레그램 관련 예외는 예외 메시지에 토큰이 섞인 URL이 들어갈 수 있으므로 `type(exc).__name__` 만 로깅한다.
- `subprocess` 는 `shell=True` 없이 인수 배열로 호출한다.
- 구조 있는 데이터는 `dict` 대신 dataclass를 쓴다.
- 커밋 메시지는 `<type>: <요약>` 형식 (feat / fix / refactor / test / docs / chore).
- 기본 제외 패턴: `.DS_Store`, `~$*`, `*.swp`, `.git/`
- 텔레그램 봇 파일 전송 한도는 50MB이므로 분할 단위는 45MB로 둔다.

---

### Task 1: 프로젝트 스캐폴딩과 설정 로더

**Files:**
- Create: `pyproject.toml`
- Create: `src/private_sync/__init__.py`
- Create: `src/private_sync/errors.py`
- Create: `src/private_sync/config.py`
- Create: `src/private_sync/agent/__init__.py`
- Create: `src/private_sync/bot/__init__.py`
- Create: `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces:
  - `private_sync.errors`: `PrivateSyncError`, `ConfigError`, `UploadError`, `RetryableUploadError`, `PackError`, `StoreError`, `TelegramError`
  - `private_sync.config`: `DEFAULT_EXCLUDES: tuple[str, ...]`, `Source(label: str, paths: tuple[Path, ...], exclude: tuple[str, ...])`, `RemoteConfig(host: str, store: str)`, `AgentConfig(remote: RemoteConfig, sources: tuple[Source, ...])`, `BotConfig(store: Path, token: str, chat_id: str, zip_password: str)`, `normalize_remote_store(raw: str) -> str`, `load_agent_config(path: Path) -> AgentConfig`, `load_bot_config(path: Path, env: Mapping[str, str] | None = None) -> BotConfig`

- [ ] **Step 1: 프로젝트 골격과 가상환경 만들기**

`pyproject.toml`:

```toml
[project]
name = "private-sync"
version = "0.1.0"
description = "노트북 파일을 사내 서버로 동기화하고 텔레그램 봇으로 내려받는다"
requires-python = ">=3.11"
dependencies = [
    "PyYAML>=6.0",
    "requests>=2.31",
    "watchdog>=4.0",
    "pyzipper>=0.3.6",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "ruff>=0.5"]

[project.scripts]
private-sync-agent = "private_sync.agent.main:main"
private-sync-bot = "private_sync.bot.main:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 88

[tool.pytest.ini_options]
pythonpath = ["src"]
```

빈 `__init__.py` 세 개를 만든다. `src/private_sync/__init__.py` 에는 `"""노트북과 사내 서버 간 파일 동기화."""` 만 넣고, `agent/__init__.py` 와 `bot/__init__.py` 는 각각 `"""노트북 측 동기화 에이전트."""`, `"""서버 측 텔레그램 봇."""` 을 넣는다.

Run:
```bash
python3 -m venv .venv
.venv/bin/pip install -q -e ".[dev]"
```
Expected: 설치 성공. `pyzipper` 설치 실패 시 `pip install -q --upgrade pip setuptools wheel` 후 재시도.

- [ ] **Step 2: errors.py 작성**

```python
"""private-sync 도메인 예외 계층."""


class PrivateSyncError(Exception):
    """모든 private-sync 예외의 기반."""


class ConfigError(PrivateSyncError):
    """설정 파일 또는 환경변수가 잘못됐다."""


class UploadError(PrivateSyncError):
    """업로드가 실패했고 재시도해도 소용없다."""


class RetryableUploadError(UploadError):
    """연결 실패 등 일시적 원인으로 업로드가 실패했다."""


class PackError(PrivateSyncError):
    """전송용 ZIP 생성에 실패했다."""


class StoreError(PrivateSyncError):
    """저장소 경로 접근이 거부됐거나 대상이 없다."""


class TelegramError(PrivateSyncError):
    """텔레그램 Bot API 호출이 실패했다."""
```

- [ ] **Step 3: 실패하는 설정 테스트 작성**

`tests/test_config.py`:

```python
from pathlib import Path

import pytest

from private_sync.config import (
    DEFAULT_EXCLUDES,
    load_agent_config,
    load_bot_config,
    normalize_remote_store,
)
from private_sync.errors import ConfigError


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_agent_config_loads_directory_and_file_paths(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    quote = _write(tmp_path / "quote.xlsx", "x")

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: ~/private-sync/store
sources:
  - label: SKT 문서
    paths:
      - {docs}
      - {quote}
    exclude: ["*.tmp"]
""",
    )

    conf = load_agent_config(cfg)

    assert conf.remote.host == "dgson@ai"
    assert conf.remote.store == "private-sync/store"
    assert len(conf.sources) == 1
    assert conf.sources[0].paths == (docs, quote)
    # 사용자 지정 패턴은 내장 기본 제외 목록에 더해진다
    assert conf.sources[0].exclude == DEFAULT_EXCLUDES + ("*.tmp",)


def test_agent_config_rejects_missing_path(tmp_path):
    cfg = _write(
        tmp_path / "agent.yaml",
        """
remote:
  host: dgson@ai
  store: store
sources:
  - label: 문서
    paths:
      - /nonexistent/place/xyz
""",
    )

    with pytest.raises(ConfigError, match="does not exist"):
        load_agent_config(cfg)


def test_agent_config_rejects_duplicate_labels(tmp_path):
    first = tmp_path / "a"
    first.mkdir()
    second = tmp_path / "b"
    second.mkdir()

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: 문서
    paths: [{first}]
  - label: 문서
    paths: [{second}]
""",
    )

    with pytest.raises(ConfigError, match="duplicate source labels"):
        load_agent_config(cfg)


def test_agent_config_rejects_colliding_store_names(tmp_path):
    left = tmp_path / "left"
    left.mkdir()
    right = tmp_path / "right"
    right.mkdir()
    _write(left / "notes.md", "l")
    _write(right / "notes.md", "r")

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: 문서
    paths:
      - {left / "notes.md"}
      - {right / "notes.md"}
""",
    )

    with pytest.raises(ConfigError, match="conflicting store names"):
        load_agent_config(cfg)


def test_agent_config_rejects_label_with_path_separator(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: a/b
    paths: [{docs}]
""",
    )

    with pytest.raises(ConfigError, match="must not contain"):
        load_agent_config(cfg)


def test_normalize_remote_store_strips_home_prefix():
    assert normalize_remote_store("~/private-sync/store/") == "private-sync/store"
    assert normalize_remote_store("/srv/store/") == "/srv/store"


def test_bot_config_reads_secrets_from_env(tmp_path):
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

    assert conf.store == store
    assert conf.token == "tok"
    assert conf.chat_id == "123"
    assert conf.zip_password == "pw"


def test_bot_config_reports_all_missing_env_vars(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    with pytest.raises(ConfigError, match="PRIVATE_SYNC_ZIP_PASSWORD"):
        load_bot_config(cfg, env={"PRIVATE_SYNC_BOT_TOKEN": "tok"})
```

- [ ] **Step 4: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.config'`

- [ ] **Step 5: config.py 구현**

```python
"""YAML 설정을 읽어 검증된 dataclass로 변환한다."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from private_sync.errors import ConfigError

# macOS·오피스·에디터가 만드는 잡파일은 설정과 무관하게 항상 제외한다
DEFAULT_EXCLUDES: tuple[str, ...] = (".DS_Store", "~$*", "*.swp", ".git/")

# 라벨은 서버 저장소의 디렉토리명이 되므로 경로 조작 문자를 허용하지 않는다
_FORBIDDEN_IN_LABEL = ("/", "\\", "..", "\n")


@dataclass(frozen=True)
class Source:
    """봇 화면의 한 라벨과 그에 속한 동기화 경로들."""

    label: str
    paths: tuple[Path, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class RemoteConfig:
    """SSH 대상과 원격 저장소 경로."""

    host: str
    store: str


@dataclass(frozen=True)
class AgentConfig:
    """노트북 agent 설정."""

    remote: RemoteConfig
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class BotConfig:
    """서버 bot 설정. 비밀값은 환경변수에서 온다."""

    store: Path
    token: str
    chat_id: str
    zip_password: str


def normalize_remote_store(raw: str) -> str:
    """원격 저장소 경로를 정규화한다.

    ssh·rsync가 원격 경로의 `~` 를 확장해주지 않는 경우가 있어, 홈 기준 상대
    경로로 바꿔 원격 CWD(홈)에 의존하게 만든다.
    """
    if raw.startswith("~/"):
        return raw[2:].rstrip("/")
    return raw.rstrip("/")


def _read_yaml(path: Path) -> dict:
    """YAML 파일을 매핑으로 읽는다."""
    try:
        with path.open(encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc.strerror)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError("invalid YAML in %s" % path) from exc

    if not isinstance(data, dict):
        raise ConfigError("config %s must be a mapping" % path)
    return data


def _validate_label(label: str) -> None:
    """라벨이 디렉토리명으로 안전한지 확인한다."""
    if not label.strip():
        raise ConfigError("source label must not be empty")
    for bad in _FORBIDDEN_IN_LABEL:
        if bad in label:
            raise ConfigError("source label %r must not contain %r" % (label, bad))


def _build_source(raw: dict) -> Source:
    """sources 항목 하나를 검증해 Source로 만든다."""
    label = str(raw.get("label", ""))
    _validate_label(label)

    raw_paths = raw.get("paths") or []
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ConfigError("source %r must define a non-empty paths list" % label)

    paths: list[Path] = []
    for item in raw_paths:
        path = Path(str(item)).expanduser()
        if not path.exists():
            raise ConfigError("source %r path does not exist: %s" % (label, path))
        paths.append(path)

    # 저장 이름이 겹치면 서버에서 서로 덮어쓰므로 시작 시점에 잡는다
    names = [p.name for p in paths]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(
            "source %r has conflicting store names: %s" % (label, duplicates)
        )

    exclude = tuple(str(x) for x in (raw.get("exclude") or []))
    return Source(label=label, paths=tuple(paths), exclude=DEFAULT_EXCLUDES + exclude)


def load_agent_config(path: Path) -> AgentConfig:
    """노트북 agent 설정을 읽고 검증한다.

    Raises:
        ConfigError: 필수 항목 누락, 없는 경로, 라벨 중복, 저장 이름 충돌.
    """
    data = _read_yaml(path)

    remote_raw = data.get("remote") or {}
    host = str(remote_raw.get("host", ""))
    store = str(remote_raw.get("store", ""))
    if not host or not store:
        raise ConfigError("remote.host and remote.store are required")

    raw_sources = data.get("sources") or []
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("at least one source is required")

    sources = tuple(_build_source(item) for item in raw_sources)

    labels = [s.label for s in sources]
    if len(set(labels)) != len(labels):
        raise ConfigError("duplicate source labels: %s" % sorted(labels))

    return AgentConfig(
        remote=RemoteConfig(host=host, store=normalize_remote_store(store)),
        sources=sources,
    )


def load_bot_config(path: Path, env: Mapping[str, str] | None = None) -> BotConfig:
    """서버 bot 설정을 읽고 비밀값은 환경변수에서 가져온다.

    Raises:
        ConfigError: store 누락·부재 또는 환경변수 누락.
    """
    env = os.environ if env is None else env
    data = _read_yaml(path)

    raw_store = str(data.get("store", ""))
    if not raw_store:
        raise ConfigError("store is required")
    store = Path(raw_store).expanduser()
    if not store.is_dir():
        raise ConfigError("store directory does not exist: %s" % store)

    secrets = {
        name: env.get(name, "")
        for name in (
            "PRIVATE_SYNC_BOT_TOKEN",
            "PRIVATE_SYNC_CHAT_ID",
            "PRIVATE_SYNC_ZIP_PASSWORD",
        )
    }
    missing = [name for name, value in secrets.items() if not value]
    if missing:
        raise ConfigError("missing environment variables: %s" % ", ".join(missing))

    return BotConfig(
        store=store,
        token=secrets["PRIVATE_SYNC_BOT_TOKEN"],
        chat_id=secrets["PRIVATE_SYNC_CHAT_ID"],
        zip_password=secrets["PRIVATE_SYNC_ZIP_PASSWORD"],
    )
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: PASS (8 passed)

- [ ] **Step 7: config.example.yaml 작성**

```yaml
# 노트북: agent.yaml 로 복사해서 쓴다
remote:
  host: dgson@ai
  store: ~/private-sync/store

sources:
  - label: SKT 문서
    paths:
      - ~/Documents/skt/          # 디렉토리 전체
      - ~/work/견적서_v3.xlsx      # 파일 하나만
    exclude: ["*.tmp"]            # 내장 기본 제외 목록에 추가된다

  - label: 개인 메모
    paths:
      - ~/notes/

# DGX 서버: bot.yaml 로 복사해서 쓴다
# store: ~/private-sync/store
#
# 서버에서 필요한 환경변수:
#   PRIVATE_SYNC_BOT_TOKEN, PRIVATE_SYNC_CHAT_ID, PRIVATE_SYNC_ZIP_PASSWORD
```

- [ ] **Step 8: 린트와 커밋**

Run:
```bash
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```
Expected: 포맷 완료, 린트 통과, 8 passed

```bash
git add pyproject.toml config.example.yaml src tests
git commit -m "feat: 프로젝트 스캐폴딩과 설정 로더 추가"
```

---

### Task 2: 미전송 목록 영속화

**Files:**
- Create: `src/private_sync/agent/pending.py`
- Test: `tests/test_pending.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `private_sync.agent.pending`: `PendingItem(label: str, path: str)` (frozen, order=True), `PendingStore(path: Path)` with `load() -> None`, `items() -> list[PendingItem]`, `add(item: PendingItem) -> None`, `discard(item: PendingItem) -> None`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_pending.py`:

```python
from private_sync.agent.pending import PendingItem, PendingStore


def test_pending_items_survive_restart(tmp_path):
    state = tmp_path / "pending.json"
    store = PendingStore(state)
    store.load()
    store.add(PendingItem(label="문서", path="/Users/me/a.md"))
    store.add(PendingItem(label="문서", path="/Users/me/b.md"))

    # 데몬 재시작을 흉내낸다
    reopened = PendingStore(state)
    reopened.load()

    assert reopened.items() == [
        PendingItem(label="문서", path="/Users/me/a.md"),
        PendingItem(label="문서", path="/Users/me/b.md"),
    ]


def test_discard_removes_item_from_disk(tmp_path):
    state = tmp_path / "pending.json"
    store = PendingStore(state)
    store.load()
    item = PendingItem(label="메모", path="/Users/me/n.md")
    store.add(item)

    store.discard(item)

    reopened = PendingStore(state)
    reopened.load()
    assert reopened.items() == []


def test_add_is_idempotent(tmp_path):
    store = PendingStore(tmp_path / "pending.json")
    store.load()
    item = PendingItem(label="메모", path="/Users/me/n.md")

    store.add(item)
    store.add(item)

    assert store.items() == [item]


def test_corrupt_state_file_starts_empty(tmp_path):
    state = tmp_path / "pending.json"
    state.write_text("{ not json", encoding="utf-8")
    store = PendingStore(state)

    store.load()

    assert store.items() == []


def test_missing_state_file_starts_empty(tmp_path):
    store = PendingStore(tmp_path / "never-written.json")

    store.load()

    assert store.items() == []
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_pending.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.agent.pending'`

- [ ] **Step 3: pending.py 구현**

```python
"""업로드하지 못한 항목을 디스크에 보존한다."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class PendingItem:
    """업로드 대기 중인 동기화 단위."""

    label: str
    path: str


class PendingStore:
    """미전송 항목 집합을 JSON 파일로 영속화한다."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: set[PendingItem] = set()

    def load(self) -> None:
        """저장된 목록을 읽는다. 파일이 없거나 깨졌으면 빈 목록으로 시작한다."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._items = set()
            return
        except (OSError, ValueError) as exc:
            # 상태 파일 손상으로 데몬이 못 뜨는 것보다 빈 목록으로 시작하는 게 낫다
            logger.warning(
                "Pending state unreadable, starting empty: %s", type(exc).__name__
            )
            self._items = set()
            return

        if not isinstance(raw, list):
            logger.warning("Pending state is not a list, starting empty")
            self._items = set()
            return

        self._items = {
            PendingItem(label=str(entry["label"]), path=str(entry["path"]))
            for entry in raw
            if isinstance(entry, dict) and "label" in entry and "path" in entry
        }

    def items(self) -> list[PendingItem]:
        """대기 항목을 정렬된 목록으로 반환한다."""
        return sorted(self._items)

    def add(self, item: PendingItem) -> None:
        """항목을 추가하고 디스크에 반영한다."""
        self._items.add(item)
        self._flush()

    def discard(self, item: PendingItem) -> None:
        """항목을 제거하고 디스크에 반영한다."""
        self._items.discard(item)
        self._flush()

    def _flush(self) -> None:
        """임시 파일에 쓴 뒤 원자적으로 교체해 중간 상태를 남기지 않는다."""
        payload = json.dumps(
            [{"label": item.label, "path": item.path} for item in self.items()],
            ensure_ascii=False,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_pending.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/agent/pending.py tests/test_pending.py
git commit -m "feat: 미전송 목록 영속화 추가"
```

---

### Task 3: rsync 업로더

**Files:**
- Create: `src/private_sync/agent/uploader.py`
- Test: `tests/test_uploader.py`

**Interfaces:**
- Consumes: `private_sync.config.RemoteConfig`, `private_sync.errors.UploadError`, `private_sync.errors.RetryableUploadError`
- Produces:
  - `private_sync.agent.uploader`: `RETRYABLE_EXIT_CODES: frozenset[int]`, `remote_dir(remote: RemoteConfig, label: str) -> str`, `build_rsync_args(remote: RemoteConfig, label: str, path: Path, exclude: tuple[str, ...]) -> list[str]`, `build_mkdir_args(remote: RemoteConfig, label: str) -> list[str]`, `upload(remote: RemoteConfig, label: str, path: Path, exclude: tuple[str, ...], runner: Runner = run_command) -> None`
  - `Runner` 는 `Callable[[list[str], int], subprocess.CompletedProcess]` 타입 별칭이다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_uploader.py`:

```python
import subprocess
from pathlib import Path

import pytest

from private_sync.agent.uploader import (
    build_mkdir_args,
    build_rsync_args,
    upload,
)
from private_sync.config import RemoteConfig
from private_sync.errors import RetryableUploadError, UploadError

REMOTE = RemoteConfig(host="dgson@ai", store="private-sync/store")


def _ok(_args, _timeout):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _failing(code, stderr=""):
    def runner(_args, _timeout):
        return subprocess.CompletedProcess(
            args=[], returncode=code, stdout="", stderr=stderr
        )

    return runner


def test_rsync_args_keep_directory_name_and_apply_excludes():
    args = build_rsync_args(
        REMOTE, "SKT 문서", Path("/Users/me/Documents/skt"), (".DS_Store", ".git/")
    )

    assert args[0] == "rsync"
    # -s 없이는 공백 있는 원격 경로가 원격 셸에서 쪼개진다
    assert "-s" in args
    assert "--exclude=.DS_Store" in args
    assert "--exclude=.git/" in args
    # 트레일링 슬래시가 없어야 원격에 skt/ 디렉토리째로 생성된다
    assert args[-2] == "/Users/me/Documents/skt"
    assert args[-1] == "dgson@ai:private-sync/store/SKT 문서/"


def test_rsync_args_for_single_file():
    args = build_rsync_args(REMOTE, "메모", Path("/Users/me/work/견적서.xlsx"), ())

    assert args[-2] == "/Users/me/work/견적서.xlsx"
    assert args[-1] == "dgson@ai:private-sync/store/메모/"


def test_mkdir_args_quote_label_with_spaces():
    args = build_mkdir_args(REMOTE, "SKT 문서")

    assert args[0] == "ssh"
    assert args[-2] == "dgson@ai"
    assert args[-1] == "mkdir -p 'private-sync/store/SKT 문서'"


def test_upload_runs_mkdir_then_rsync():
    calls = []

    def runner(args, timeout):
        calls.append(args[0])
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)

    assert calls == ["ssh", "rsync"]


def test_ssh_connection_failure_is_retryable():
    with pytest.raises(RetryableUploadError):
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=_failing(255))


def test_rsync_permission_error_is_not_retryable():
    def runner(args, timeout):
        if args[0] == "ssh":
            return _ok(args, timeout)
        return subprocess.CompletedProcess(
            args=args, returncode=23, stdout="", stderr="Permission denied"
        )

    with pytest.raises(UploadError) as excinfo:
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)

    assert not isinstance(excinfo.value, RetryableUploadError)


def test_rsync_socket_error_is_retryable():
    def runner(args, timeout):
        if args[0] == "ssh":
            return _ok(args, timeout)
        return subprocess.CompletedProcess(
            args=args, returncode=30, stdout="", stderr="timeout"
        )

    with pytest.raises(RetryableUploadError):
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)


def test_timeout_is_retryable():
    def runner(_args, _timeout):
        raise subprocess.TimeoutExpired(cmd="rsync", timeout=1)

    with pytest.raises(RetryableUploadError):
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_uploader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.agent.uploader'`

- [ ] **Step 3: uploader.py 구현**

```python
"""rsync over SSH 로 로컬 경로를 서버 저장소에 올린다."""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from private_sync.config import RemoteConfig
from private_sync.errors import RetryableUploadError, UploadError

logger = logging.getLogger(__name__)

# 연결·전송 계층 실패. 사내망 밖이거나 일시 장애이므로 재시도한다.
#   10 socket I/O, 12 protocol, 30 timeout, 35 timeout waiting for daemon,
#   255 ssh 연결 실패
RETRYABLE_EXIT_CODES = frozenset({10, 12, 30, 35, 255})

_SSH_TIMEOUT_SEC = 15
_RSYNC_TIMEOUT_SEC = 600
_STDERR_LOG_LIMIT = 200

Runner = Callable[[list[str], int], "subprocess.CompletedProcess[str]"]


def run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """외부 명령을 인수 배열로 실행한다(셸 미사용)."""
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def remote_dir(remote: RemoteConfig, label: str) -> str:
    """라벨에 해당하는 원격 디렉토리 경로를 만든다(원격 홈 기준)."""
    return "%s/%s" % (remote.store, label)


def build_rsync_args(
    remote: RemoteConfig, label: str, path: Path, exclude: tuple[str, ...]
) -> list[str]:
    """rsync 명령 인수를 조립한다.

    -s(--protect-args)로 원격 경로의 공백·특수문자를 rsync가 직접 처리하게
    한다. 로컬 경로에 트레일링 슬래시를 붙이지 않아 디렉토리는 자기 이름째로
    원격에 생성된다.
    """
    args = ["rsync", "-az", "-s", "--partial"]
    args += ["--exclude=%s" % pattern for pattern in exclude]
    args.append(str(path))
    args.append("%s:%s/" % (remote.host, remote_dir(remote, label)))
    return args


def build_mkdir_args(remote: RemoteConfig, label: str) -> list[str]:
    """원격 디렉토리를 미리 만드는 ssh 명령 인수를 조립한다.

    원격 셸이 문자열을 해석하므로 경로는 shlex.quote 로 감싼다.
    """
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=%d" % _SSH_TIMEOUT_SEC,
        remote.host,
        "mkdir -p %s" % shlex.quote(remote_dir(remote, label)),
    ]


def upload(
    remote: RemoteConfig,
    label: str,
    path: Path,
    exclude: tuple[str, ...],
    runner: Runner = run_command,
) -> None:
    """경로 하나를 서버 저장소에 올린다.

    Raises:
        RetryableUploadError: 연결 실패·타임아웃 등 재시도 가치가 있는 오류.
        UploadError: 권한·디스크 등 재시도해도 소용없는 오류.
    """
    # 원격 라벨 디렉토리가 없으면 rsync가 실패하므로 먼저 만든다
    try:
        mkdir = runner(build_mkdir_args(remote, label), _SSH_TIMEOUT_SEC + 5)
    except subprocess.TimeoutExpired as exc:
        raise RetryableUploadError(
            "ssh mkdir timed out for label %s" % label
        ) from exc

    if mkdir.returncode == 255:
        raise RetryableUploadError(
            "ssh connection failed for label %s (exit 255)" % label
        )
    if mkdir.returncode != 0:
        raise UploadError(
            "ssh mkdir failed for label %s (exit %d): %s"
            % (label, mkdir.returncode, (mkdir.stderr or "").strip()[:_STDERR_LOG_LIMIT])
        )

    try:
        result = runner(build_rsync_args(remote, label, path, exclude), _RSYNC_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise RetryableUploadError("rsync timed out for %s" % path) from exc

    if result.returncode == 0:
        logger.info("Uploaded %s under label %s", path, label)
        return

    message = "rsync failed for %s (label %s, exit %d): %s" % (
        path,
        label,
        result.returncode,
        (result.stderr or "").strip()[:_STDERR_LOG_LIMIT],
    )
    if result.returncode in RETRYABLE_EXIT_CODES:
        raise RetryableUploadError(message)
    raise UploadError(message)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_uploader.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/agent/uploader.py tests/test_uploader.py
git commit -m "feat: rsync 업로더 추가"
```

---

### Task 4: 감시 대상 매칭과 디바운스

**Files:**
- Create: `src/private_sync/agent/watcher.py`
- Test: `tests/test_watcher.py`

**Interfaces:**
- Consumes: `private_sync.config.Source`
- Produces:
  - `private_sync.agent.watcher`: `WatchTarget(label: str, path: Path, exclude: tuple[str, ...], watch_dir: Path, recursive: bool)` (frozen), `is_excluded(rel_parts: tuple[str, ...], patterns: tuple[str, ...]) -> bool`, `build_targets(sources: tuple[Source, ...]) -> list[WatchTarget]`, `match_target(event_path: Path, targets: list[WatchTarget]) -> WatchTarget | None`, `Debouncer(delay: float)` with `touch(key: object, now: float) -> None`, `due(now: float) -> list[object]`, `pending_count() -> int`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_watcher.py`:

```python
from pathlib import Path

from private_sync.agent.watcher import (
    Debouncer,
    build_targets,
    is_excluded,
    match_target,
)
from private_sync.config import Source


def test_directory_target_watches_itself_recursively(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = Source(label="문서", paths=(docs,), exclude=())

    targets = build_targets((source,))

    assert len(targets) == 1
    assert targets[0].watch_dir == docs
    assert targets[0].recursive is True


def test_file_target_watches_parent_directory(tmp_path):
    quote = tmp_path / "견적서.xlsx"
    quote.write_text("x", encoding="utf-8")
    source = Source(label="메모", paths=(quote,), exclude=())

    targets = build_targets((source,))

    assert targets[0].watch_dir == tmp_path
    assert targets[0].recursive is False


def test_file_target_ignores_siblings(tmp_path):
    quote = tmp_path / "견적서.xlsx"
    quote.write_text("x", encoding="utf-8")
    other = tmp_path / "무관.txt"
    other.write_text("y", encoding="utf-8")
    targets = build_targets((Source(label="메모", paths=(quote,), exclude=()),))

    assert match_target(quote, targets) is not None
    assert match_target(other, targets) is None


def test_directory_target_matches_nested_file(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    nested = docs / "sub" / "a.md"
    nested.write_text("a", encoding="utf-8")
    targets = build_targets((Source(label="문서", paths=(docs,), exclude=()),))

    assert match_target(nested, targets).label == "문서"


def test_excluded_file_does_not_match(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    junk = docs / ".DS_Store"
    junk.write_text("j", encoding="utf-8")
    targets = build_targets(
        (Source(label="문서", paths=(docs,), exclude=(".DS_Store",)),)
    )

    assert match_target(junk, targets) is None


def test_specific_file_target_survives_broader_exclude(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    quote = work / "견적서.xlsx"
    quote.write_text("q", encoding="utf-8")
    targets = build_targets(
        (
            Source(label="프로젝트", paths=(work,), exclude=("*.xlsx",)),
            Source(label="견적서", paths=(quote,), exclude=()),
        )
    )

    # 앞선 대상의 exclude가 뒤에 개별 등록된 파일을 가려서는 안 된다
    assert match_target(quote, targets).label == "견적서"


def test_is_excluded_matches_glob_on_any_part():
    assert is_excluded(("work", "~$보고서.docx"), ("~$*",)) is True
    assert is_excluded(("work", "보고서.docx"), ("~$*",)) is False


def test_is_excluded_matches_directory_pattern_only_on_parents():
    assert is_excluded((".git", "config"), (".git/",)) is True
    # 파일 자신의 이름과는 매칭하지 않는다
    assert is_excluded(("work", ".git"), (".git/",)) is False


def test_debouncer_collapses_repeated_touches():
    debouncer = Debouncer(delay=3.0)

    debouncer.touch("a", now=100.0)
    debouncer.touch("a", now=101.0)
    debouncer.touch("a", now=102.0)

    # 마지막 touch 기준으로 3초를 기다린다
    assert debouncer.due(now=104.0) == []
    assert debouncer.due(now=105.0) == ["a"]
    # 한 번 꺼낸 키는 다시 나오지 않는다
    assert debouncer.due(now=200.0) == []


def test_debouncer_releases_independent_keys():
    debouncer = Debouncer(delay=1.0)
    debouncer.touch("a", now=10.0)
    debouncer.touch("b", now=20.0)

    assert debouncer.due(now=11.0) == ["a"]
    assert debouncer.pending_count() == 1
    assert debouncer.due(now=21.0) == ["b"]
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_watcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.agent.watcher'`

- [ ] **Step 3: watcher.py 구현**

```python
"""감시 대상 매칭과 이벤트 디바운스."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from private_sync.config import Source


@dataclass(frozen=True)
class WatchTarget:
    """동기화 단위 하나와 그것을 감시할 디렉토리."""

    label: str
    path: Path
    exclude: tuple[str, ...]
    watch_dir: Path
    recursive: bool


def is_excluded(rel_parts: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    """상대경로 조각들이 exclude 패턴에 걸리는지 판단한다.

    슬래시로 끝나는 패턴은 상위 디렉토리 이름으로만, 나머지는 각 조각 이름의
    glob으로 매칭한다.
    """
    for pattern in patterns:
        if pattern.endswith("/"):
            if pattern[:-1] in rel_parts[:-1]:
                return True
            continue
        for part in rel_parts:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def build_targets(sources: tuple[Source, ...]) -> list[WatchTarget]:
    """설정의 각 경로를 감시 대상으로 변환한다.

    watchdog은 개별 파일을 감시할 수 없어서, 파일 항목은 부모 디렉토리를
    비재귀로 감시하고 매칭 단계에서 걸러낸다.
    """
    targets: list[WatchTarget] = []
    for source in sources:
        for path in source.paths:
            is_dir = path.is_dir()
            targets.append(
                WatchTarget(
                    label=source.label,
                    path=path,
                    exclude=source.exclude,
                    watch_dir=path if is_dir else path.parent,
                    recursive=is_dir,
                )
            )
    return targets


def _relative_parts(event_path: Path, target: WatchTarget) -> tuple[str, ...] | None:
    """이벤트 경로가 대상에 속하면 대상 기준 상대 경로 조각을 반환한다."""
    if target.recursive:
        try:
            return event_path.relative_to(target.path).parts
        except ValueError:
            return None
    return (event_path.name,) if event_path == target.path else None


def match_target(
    event_path: Path, targets: list[WatchTarget]
) -> WatchTarget | None:
    """이벤트 경로가 속한 감시 대상을 찾는다. 어디에도 속하지 않으면 None.

    한 경로가 여러 대상에 걸칠 수 있으므로 exclude에 걸린 대상은 건너뛰고 다음
    대상을 계속 확인한다. 폴더 전체를 제외 패턴과 함께 등록하고 그 안의 파일
    하나를 따로 등록한 설정에서, 개별 등록이 앞선 대상의 exclude에 묻히지
    않게 한다.
    """
    for target in targets:
        parts = _relative_parts(event_path, target)
        if parts is None:
            continue
        if is_excluded(parts, target.exclude):
            continue
        return target
    return None


class Debouncer:
    """같은 키의 연속 이벤트를 delay 초 동안 하나로 묶는다.

    시간을 인수로 받아 테스트에서 시계를 조작할 수 있게 한다.
    """

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._deadlines: dict[object, float] = {}

    def touch(self, key: object, now: float) -> None:
        """키의 마감을 now + delay 로 미룬다."""
        self._deadlines[key] = now + self._delay

    def due(self, now: float) -> list[object]:
        """마감이 지난 키들을 꺼내고 목록에서 제거한다."""
        ready = [key for key, deadline in self._deadlines.items() if deadline <= now]
        for key in ready:
            del self._deadlines[key]
        return ready

    def pending_count(self) -> int:
        """아직 마감을 기다리는 키 수를 반환한다."""
        return len(self._deadlines)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_watcher.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/agent/watcher.py tests/test_watcher.py
git commit -m "feat: 감시 대상 매칭과 디바운스 추가"
```

---

### Task 5: agent 데몬 조립

**Files:**
- Create: `src/private_sync/agent/main.py`
- Test: `tests/test_agent_main.py`

**Interfaces:**
- Consumes: `load_agent_config`, `PendingItem`, `PendingStore`, `build_targets`, `match_target`, `Debouncer`, `upload`, `RetryableUploadError`, `UploadError`
- Produces:
  - `private_sync.agent.main`: `Backoff(base: float = 3.0, cap: float = 300.0)` with `delay() -> float`, `fail() -> None`, `reset() -> None`; `SyncWorker(config: AgentConfig, pending: PendingStore, uploader: UploadFn = upload)` with `enqueue(label: str, path: Path) -> None`, `drain() -> None`, `has_pending() -> bool`, 그리고 공개 속성 `backoff: Backoff`; `LoopState(debouncer: Debouncer, lock: threading.Lock, stop: threading.Event)`; `main(argv: list[str] | None = None) -> int`
  - `LoopState` 는 감시 스레드와 메인 루프가 공유하는 상태를 한 덩어리로 묶는다. 이것이 있어야 `_EventHandler.__init__` 과 `_run_loop` 이 파라미터 3개 이하 규칙을 지킨다.
  - `UploadFn` 은 `Callable[[RemoteConfig, str, Path, tuple[str, ...]], None]` 타입 별칭이다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_agent_main.py`:

```python
import threading
import time
from pathlib import Path

from private_sync.agent.main import (
    _DEBOUNCE_SEC,
    Backoff,
    LoopState,
    SyncWorker,
    _EventHandler,
)
from private_sync.agent.pending import PendingItem, PendingStore
from private_sync.agent.watcher import Debouncer, build_targets
from private_sync.config import AgentConfig, RemoteConfig, Source
from private_sync.errors import RetryableUploadError, UploadError

REMOTE = RemoteConfig(host="dgson@ai", store="store")


def _config(path: Path) -> AgentConfig:
    return AgentConfig(
        remote=REMOTE,
        sources=(Source(label="문서", paths=(path,), exclude=()),),
    )


def test_successful_upload_clears_pending(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("a", encoding="utf-8")
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()
    worker = SyncWorker(_config(doc), pending, uploader=lambda *_args: None)

    worker.enqueue("문서", doc)
    worker.drain()

    assert pending.items() == []


def test_retryable_failure_keeps_item_pending(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("a", encoding="utf-8")
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()

    def failing(*_args):
        raise RetryableUploadError("offline")

    worker = SyncWorker(_config(doc), pending, uploader=failing)

    worker.enqueue("문서", doc)
    worker.drain()

    assert pending.items() == [PendingItem(label="문서", path=str(doc))]


def test_permanent_failure_drops_item(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("a", encoding="utf-8")
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()

    def failing(*_args):
        raise UploadError("permission denied")

    worker = SyncWorker(_config(doc), pending, uploader=failing)

    worker.enqueue("문서", doc)
    worker.drain()

    # 무한 재시도를 막기 위해 격리한다
    assert pending.items() == []


def test_unknown_label_is_skipped(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("a", encoding="utf-8")
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()
    calls = []
    worker = SyncWorker(
        _config(doc), pending, uploader=lambda *args: calls.append(args)
    )

    worker.enqueue("없는라벨", doc)
    worker.drain()

    assert calls == []


class _FakeEvent:
    """watchdog FileSystemEvent 를 흉내내는 최소 객체."""

    def __init__(self, src_path, event_type="modified", is_directory=False, dest_path=""):
        self.src_path = src_path
        self.event_type = event_type
        self.is_directory = is_directory
        self.dest_path = dest_path


def _handler_state(source):
    """이벤트 핸들러와 그것이 쓰는 공유 상태를 만든다."""
    state = LoopState(
        debouncer=Debouncer(_DEBOUNCE_SEC),
        lock=threading.Lock(),
        stop=threading.Event(),
    )
    return _EventHandler(build_targets((source,)), state), state


def _released(state):
    """디바운스 마감을 지나쳐 방출된 키 목록을 돌려준다."""
    return state.debouncer.due(time.monotonic() + _DEBOUNCE_SEC + 1)


def test_event_handler_queues_matching_file(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    changed = docs / "a.md"
    changed.write_text("a", encoding="utf-8")
    handler, state = _handler_state(Source(label="문서", paths=(docs,), exclude=()))

    handler.on_any_event(_FakeEvent(str(changed)))

    assert _released(state) == [("문서", str(docs))]


def test_event_handler_ignores_directory_events(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    handler, state = _handler_state(Source(label="문서", paths=(docs,), exclude=()))

    handler.on_any_event(_FakeEvent(str(docs / "sub"), is_directory=True))

    assert _released(state) == []


def test_event_handler_ignores_deleted_events(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    handler, state = _handler_state(Source(label="문서", paths=(docs,), exclude=()))

    handler.on_any_event(_FakeEvent(str(docs / "a.md"), event_type="deleted"))

    # 삭제는 서버로 전파하지 않는다
    assert _released(state) == []


def test_event_handler_follows_rename_destination(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    handler, state = _handler_state(Source(label="문서", paths=(docs,), exclude=()))

    handler.on_any_event(
        _FakeEvent(
            str(tmp_path / "elsewhere.md"),
            event_type="moved",
            dest_path=str(docs / "a.md"),
        )
    )

    # 새 내용은 목적지 경로에 있다
    assert _released(state) == [("문서", str(docs))]


def test_event_handler_ignores_excluded_file(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    handler, state = _handler_state(
        Source(label="문서", paths=(docs,), exclude=(".DS_Store",))
    )

    handler.on_any_event(_FakeEvent(str(docs / ".DS_Store")))

    assert _released(state) == []


def test_backoff_grows_then_resets():
    backoff = Backoff(base=3.0, cap=20.0)

    assert backoff.delay() == 3.0
    backoff.fail()
    assert backoff.delay() == 6.0
    backoff.fail()
    assert backoff.delay() == 12.0
    backoff.fail()
    # cap을 넘지 않는다
    assert backoff.delay() == 20.0

    backoff.reset()
    assert backoff.delay() == 3.0
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_agent_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.agent.main'`

- [ ] **Step 3: main.py 구현**

```python
"""노트북 측 동기화 데몬 진입점."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from private_sync.agent.pending import PendingItem, PendingStore
from private_sync.agent.uploader import upload
from private_sync.agent.watcher import Debouncer, build_targets, match_target
from private_sync.config import AgentConfig, RemoteConfig, load_agent_config
from private_sync.errors import ConfigError, RetryableUploadError, UploadError

logger = logging.getLogger(__name__)

UploadFn = Callable[[RemoteConfig, str, Path, tuple[str, ...]], None]

_DEBOUNCE_SEC = 3.0
_TICK_SEC = 1.0
_DEFAULT_CONFIG = Path("~/.config/private-sync/agent.yaml").expanduser()
_DEFAULT_STATE = Path("~/.local/state/private-sync/pending.json").expanduser()


class Backoff:
    """연속 실패 시 대기 시간을 지수적으로 늘린다."""

    def __init__(self, base: float = 3.0, cap: float = 300.0) -> None:
        self._base = base
        self._cap = cap
        self._failures = 0

    def delay(self) -> float:
        """현재 대기 시간을 반환한다."""
        return min(self._base * (2**self._failures), self._cap)

    def fail(self) -> None:
        """실패를 기록해 다음 대기 시간을 늘린다."""
        self._failures += 1

    def reset(self) -> None:
        """성공했으므로 대기 시간을 초기화한다."""
        self._failures = 0


class SyncWorker:
    """대기 항목을 하나씩 업로드하고 결과에 따라 목록을 갱신한다."""

    def __init__(
        self,
        config: AgentConfig,
        pending: PendingStore,
        uploader: UploadFn = upload,
    ) -> None:
        self._config = config
        self._pending = pending
        self._uploader = uploader
        self._excludes = {s.label: s.exclude for s in config.sources}
        self.backoff = Backoff()

    def enqueue(self, label: str, path: Path) -> None:
        """항목을 대기 목록에 넣는다."""
        self._pending.add(PendingItem(label=label, path=str(path)))

    def drain(self) -> None:
        """대기 항목을 순서대로 업로드한다.

        재시도 대상 실패가 나오면 남은 항목은 그대로 두고 즉시 멈춘다. 오프라인
        상태에서 목록 전체를 헛되게 시도하지 않기 위함이다.
        """
        for item in self._pending.items():
            exclude = self._excludes.get(item.label)
            if exclude is None:
                logger.warning("Dropping item with unknown label %s", item.label)
                self._pending.discard(item)
                continue

            try:
                self._uploader(
                    self._config.remote, item.label, Path(item.path), exclude
                )
            except RetryableUploadError as exc:
                logger.warning("Upload deferred for %s: %s", item.path, exc)
                self.backoff.fail()
                return
            except UploadError as exc:
                # 재시도해도 실패할 오류는 격리해 무한 루프를 막는다
                logger.error("Upload failed permanently for %s: %s", item.path, exc)
                self._pending.discard(item)
                continue

            self._pending.discard(item)
            self.backoff.reset()

    def has_pending(self) -> bool:
        """대기 항목이 남아 있는지 반환한다."""
        return bool(self._pending.items())


@dataclass
class LoopState:
    """감시 스레드와 메인 루프가 공유하는 상태."""

    debouncer: Debouncer
    lock: threading.Lock
    stop: threading.Event


class _EventHandler(FileSystemEventHandler):
    """watchdog 이벤트를 디바운서에 넣는다."""

    def __init__(self, targets: list[WatchTarget], state: LoopState) -> None:
        self._targets = targets
        self._state = state

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type == "deleted":
            return

        # 이름 변경은 목적지 경로가 새 내용을 담는다
        raw = getattr(event, "dest_path", "") or event.src_path
        target = match_target(Path(raw), self._targets)
        if target is None:
            return

        with self._state.lock:
            self._state.debouncer.touch(
                (target.label, str(target.path)), time.monotonic()
            )
```

`dataclass` 와 `WatchTarget` 을 임포트에 추가한다: `from dataclasses import dataclass`,
`from private_sync.agent.watcher import Debouncer, WatchTarget, build_targets, match_target`.

`main()` 과 루프:

```python
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="private-sync 노트북 에이전트")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=_DEFAULT_STATE)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def _run_loop(worker: SyncWorker, state: LoopState) -> None:
    """디바운스가 끝난 항목을 큐에 넣고 업로드를 시도한다."""
    while not state.stop.is_set():
        with state.lock:
            ready = state.debouncer.due(time.monotonic())
        for label, path in ready:
            worker.enqueue(label, Path(path))

        worker.drain()

        # 대기 항목이 남았다면 오프라인이므로 backoff만큼 쉬고 다시 시도한다
        wait_sec = worker.backoff.delay() if worker.has_pending() else _TICK_SEC
        state.stop.wait(wait_sec)


def main(argv: list[str] | None = None) -> int:
    """에이전트를 실행한다."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = load_agent_config(args.config)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    pending = PendingStore(args.state)
    pending.load()
    worker = SyncWorker(config, pending)
    targets = build_targets(config.sources)
    state = LoopState(
        debouncer=Debouncer(_DEBOUNCE_SEC),
        lock=threading.Lock(),
        stop=threading.Event(),
    )

    observer = Observer()
    handler = _EventHandler(targets, state)
    for watch_dir, recursive in {(t.watch_dir, t.recursive) for t in targets}:
        observer.schedule(handler, str(watch_dir), recursive=recursive)

    signal.signal(signal.SIGTERM, lambda *_: state.stop.set())
    signal.signal(signal.SIGINT, lambda *_: state.stop.set())

    observer.start()
    logger.info("Agent started with %d watch targets", len(targets))
    try:
        _run_loop(worker, state)
    finally:
        observer.stop()
        observer.join(timeout=5)
        logger.info("Agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_agent_main.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: 전체 테스트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/agent/main.py tests/test_agent_main.py
git commit -m "feat: agent 데몬 진입점 추가"
```

---

### Task 6: 저장소 탐색과 경로 안전 검증

**Files:**
- Create: `src/private_sync/bot/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `private_sync.errors.StoreError`
- Produces:
  - `private_sync.bot.store`: `Entry(name: str, rel: str, is_dir: bool, size: int)` (frozen), `_is_inside(base: Path, candidate: Path) -> bool`, `resolve_safe(root: Path, rel: str) -> Path`, `list_dir(root: Path, rel: str) -> list[Entry]`, `search(root: Path, keyword: str, limit: int = 50) -> list[Entry]`, `parent_rel(rel: str) -> str | None`
  - `rel` 은 저장소 루트 기준 POSIX 상대경로 문자열이며 루트는 `""` 이다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_store.py`:

```python
import logging

import pytest

from private_sync.bot.store import (
    list_dir,
    parent_rel,
    resolve_safe,
    search,
)
from private_sync.errors import StoreError


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "store"
    (root / "SKT 문서" / "sub").mkdir(parents=True)
    (root / "SKT 문서" / "계약서.docx").write_text("c", encoding="utf-8")
    (root / "SKT 문서" / "sub" / "회의록.md").write_text("m", encoding="utf-8")
    (root / "메모").mkdir()
    (root / "메모" / "계약_메모.txt").write_text("n", encoding="utf-8")
    return root


def test_list_root_returns_labels(store):
    entries = list_dir(store, "")

    assert [e.name for e in entries] == ["SKT 문서", "메모"]
    assert all(e.is_dir for e in entries)


def test_list_dir_sorts_directories_before_files(store):
    entries = list_dir(store, "SKT 문서")

    assert [e.name for e in entries] == ["sub", "계약서.docx"]
    assert entries[0].is_dir is True
    assert entries[1].is_dir is False
    assert entries[1].rel == "SKT 문서/계약서.docx"
    assert entries[1].size == 1


def test_search_matches_filename_substring_across_labels(store):
    results = search(store, "계약")

    assert sorted(e.rel for e in results) == [
        "SKT 문서/계약서.docx",
        "메모/계약_메모.txt",
    ]


def test_search_is_case_insensitive(store):
    (store / "메모" / "Report.PDF").write_text("r", encoding="utf-8")

    assert [e.name for e in search(store, "report")] == ["Report.PDF"]


def test_search_respects_limit(store):
    for index in range(10):
        (store / "메모" / f"bulk{index}.txt").write_text("b", encoding="utf-8")

    assert len(search(store, "bulk", limit=3)) == 3


def test_resolve_safe_rejects_parent_traversal(store):
    with pytest.raises(StoreError, match="outside the store"):
        resolve_safe(store, "../../etc/passwd")


def test_resolve_safe_rejects_absolute_path(store):
    with pytest.raises(StoreError, match="outside the store"):
        resolve_safe(store, "/etc/passwd")


def test_resolve_safe_rejects_missing_target(store):
    with pytest.raises(StoreError, match="not found"):
        resolve_safe(store, "메모/없는파일.txt")


def test_resolve_safe_accepts_valid_relative_path(store):
    resolved = resolve_safe(store, "SKT 문서/계약서.docx")

    assert resolved == (store / "SKT 문서" / "계약서.docx").resolve()


def test_parent_rel_walks_up_to_root():
    assert parent_rel("SKT 문서/sub") == "SKT 문서"
    assert parent_rel("SKT 문서") == ""
    assert parent_rel("") is None


def test_list_dir_rejects_non_directory(store):
    with pytest.raises(StoreError, match="not a directory"):
        list_dir(store, "SKT 문서/계약서.docx")


def test_symlink_escaping_store_is_hidden_and_unreadable(store, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET", encoding="utf-8")
    (store / "메모" / "link.txt").symlink_to(outside)

    # 내용은 물론 이름·크기조차 노출되지 않아야 한다
    assert "link.txt" not in [e.name for e in list_dir(store, "메모")]
    assert search(store, "link") == []
    with pytest.raises(StoreError, match="outside the store"):
        resolve_safe(store, "메모/link.txt")


def test_symlink_inside_store_stays_visible(store):
    (store / "메모" / "바로가기.docx").symlink_to(store / "SKT 문서" / "계약서.docx")

    assert "바로가기.docx" in [e.name for e in list_dir(store, "메모")]


def test_sibling_directory_sharing_name_prefix_is_rejected(store):
    evil = store.parent / (store.name + "-evil")
    evil.mkdir()
    (evil / "leak.txt").write_text("x", encoding="utf-8")

    # 문자열 접두사 비교였다면 통과했을 경로다
    with pytest.raises(StoreError, match="outside the store"):
        resolve_safe(store, f"../{evil.name}/leak.txt")


def test_search_does_not_report_truncation_when_results_fit(store, caplog):
    # 마지막 순회 항목이 '비일치'여야 오탐 분기를 실제로 밟는다.
    # 정렬상 zz_other.txt 는 두 일치 항목 뒤, 한글 이름 앞에 온다.
    (store / "메모" / "aa_match.txt").write_text("1", encoding="utf-8")
    (store / "메모" / "ab_match.txt").write_text("2", encoding="utf-8")
    (store / "메모" / "zz_other.txt").write_text("3", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        results = search(store, "match", limit=2)

    assert len(results) == 2
    assert "truncated" not in caplog.text


def test_search_reports_truncation_when_matches_exceed_limit(store, caplog):
    for index in range(4):
        (store / "메모" / f"bulk{index}.txt").write_text("b", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        results = search(store, "bulk", limit=2)

    assert len(results) == 2
    assert "truncated" in caplog.text
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.bot.store'`

- [ ] **Step 3: store.py 구현**

```python
"""서버 저장소 탐색과 경로 안전 검증."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from private_sync.errors import StoreError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entry:
    """저장소 항목 하나."""

    name: str
    rel: str
    is_dir: bool
    size: int


def _is_inside(base: Path, candidate: Path) -> bool:
    """이미 resolve된 후보 경로가 저장소 루트 안(또는 루트 자신)인지 판단한다.

    문자열 접두사 비교는 `store-evil` 같은 형제 디렉토리에 뚫리므로 `parents`
    멤버십으로 확인한다.
    """
    return candidate == base or base in candidate.parents


def resolve_safe(root: Path, rel: str) -> Path:
    """상대경로를 저장소 루트 안의 실제 경로로 바꾼다.

    심볼릭 링크와 `..` 를 모두 펼친 뒤 루트 하위인지 확인해, 저장소 밖 파일이
    노출되는 것을 막는다.

    Raises:
        StoreError: 루트를 벗어나거나 대상이 존재하지 않을 때.
    """
    base = root.resolve()
    candidate = (base / rel).resolve()

    if not _is_inside(base, candidate):
        raise StoreError(f"path {rel!r} resolves outside the store")
    if not candidate.exists():
        raise StoreError(f"path {rel!r} not found in store")
    return candidate


def list_dir(root: Path, rel: str) -> list[Entry]:
    """디렉토리 내용을 디렉토리 먼저, 이름순으로 나열한다.

    Raises:
        StoreError: 경로가 루트를 벗어나거나 디렉토리가 아닐 때.
    """
    base = root.resolve()
    target = resolve_safe(root, rel)
    if not target.is_dir():
        raise StoreError(f"path {rel!r} is not a directory")

    # 저장소 밖을 가리키는 심볼릭 링크는 이름·크기조차 노출하지 않는다
    entries = [
        _to_entry(child, rel)
        for child in target.iterdir()
        if _is_inside(base, child.resolve())
    ]
    return sorted(entries, key=lambda e: (not e.is_dir, e.name))


def search(root: Path, keyword: str, limit: int = 50) -> list[Entry]:
    """파일명에 키워드가 포함된 파일을 저장소 전체에서 찾는다."""
    needle = keyword.strip().lower()
    if not needle:
        return []

    base = root.resolve()
    results: list[Entry] = []
    truncated = False
    for path in sorted(base.rglob("*")):
        if not path.is_file() or needle not in path.name.lower():
            continue
        if not _is_inside(base, path.resolve()):
            continue

        # 한도를 넘는 '실제 일치'를 만났을 때만 절단으로 기록한다
        if len(results) >= limit:
            truncated = True
            break

        rel = str(PurePosixPath(path.relative_to(base)))
        results.append(
            Entry(name=path.name, rel=rel, is_dir=False, size=path.stat().st_size)
        )

    if truncated:
        logger.info("Search for %r truncated at %d results", keyword, limit)
    return results


def parent_rel(rel: str) -> str | None:
    """상위 디렉토리의 상대경로를 반환한다. 루트면 None."""
    if not rel:
        return None
    parent = PurePosixPath(rel).parent
    return "" if str(parent) == "." else str(parent)


def _to_entry(path: Path, parent: str) -> Entry:
    """경로를 Entry로 변환한다."""
    rel = str(PurePosixPath(parent) / path.name) if parent else path.name
    is_dir = path.is_dir()
    return Entry(
        name=path.name,
        rel=rel,
        is_dir=is_dir,
        size=0 if is_dir else path.stat().st_size,
    )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: PASS (16 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/store.py tests/test_store.py
git commit -m "feat: 저장소 탐색과 경로 안전 검증 추가"
```

---

### Task 7: 암호 ZIP 포장과 분할

**Files:**
- Create: `src/private_sync/bot/packer.py`
- Test: `tests/test_packer.py`

**Interfaces:**
- Consumes: `private_sync.errors.PackError`
- Produces:
  - `private_sync.bot.packer`: `MAX_PART_BYTES: int`, `make_encrypted_zip(src: Path, dest_dir: Path, password: str) -> Path`, `split_file(path: Path, max_bytes: int = MAX_PART_BYTES) -> list[Path]`, `pack_for_send(src: Path, dest_dir: Path, password: str, max_bytes: int = MAX_PART_BYTES) -> list[Path]`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_packer.py`:

```python
from pathlib import Path

import pyzipper
import pytest

from private_sync.bot.packer import (
    MAX_PART_BYTES,
    make_encrypted_zip,
    pack_for_send,
    split_file,
)
from private_sync.errors import PackError


def test_part_size_stays_under_telegram_limit():
    # 텔레그램 봇 sendDocument 한도는 50MB다
    assert MAX_PART_BYTES < 50 * 1024 * 1024


def test_encrypted_zip_opens_with_password_and_matches_source(tmp_path):
    src = tmp_path / "계약서.docx"
    src.write_bytes(b"secret payload")
    dest = tmp_path / "out"
    dest.mkdir()

    archive = make_encrypted_zip(src, dest, password="pw1234")

    assert archive.name == "계약서.docx.zip"
    with pyzipper.AESZipFile(archive) as zf:
        zf.setpassword(b"pw1234")
        assert zf.namelist() == ["계약서.docx"]
        assert zf.read("계약서.docx") == b"secret payload"


def test_encrypted_zip_rejects_wrong_password(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"data")
    dest = tmp_path / "out"
    dest.mkdir()
    archive = make_encrypted_zip(src, dest, password="right")

    with pyzipper.AESZipFile(archive) as zf:
        zf.setpassword(b"wrong")
        with pytest.raises(RuntimeError):
            zf.read("a.txt")


def test_split_and_rejoin_reproduces_original_bytes(tmp_path):
    payload = bytes(range(256)) * 20  # 5120 바이트
    target = tmp_path / "big.zip"
    target.write_bytes(payload)

    parts = split_file(target, max_bytes=1024)

    assert [p.name for p in parts] == [
        "big.zip.part01",
        "big.zip.part02",
        "big.zip.part03",
        "big.zip.part04",
        "big.zip.part05",
    ]
    rejoined = b"".join(p.read_bytes() for p in parts)
    assert rejoined == payload


def test_pack_for_send_returns_single_file_when_small(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"tiny")
    dest = tmp_path / "out"
    dest.mkdir()

    parts = pack_for_send(src, dest, password="pw", max_bytes=1024 * 1024)

    assert len(parts) == 1
    assert parts[0].suffix == ".zip"


def test_pack_for_send_splits_when_over_limit(tmp_path):
    src = tmp_path / "a.bin"
    # 압축되지 않는 데이터를 만들어 ZIP이 확실히 한도를 넘게 한다
    src.write_bytes(bytes(range(256)) * 40)
    dest = tmp_path / "out"
    dest.mkdir()

    parts = pack_for_send(src, dest, password="pw", max_bytes=512)

    assert len(parts) > 1
    assert all(p.stat().st_size <= 512 for p in parts)
    assert all(".part" in p.name for p in parts)


def test_pack_for_send_rejects_missing_source(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(PackError, match="cannot read"):
        pack_for_send(tmp_path / "없음.txt", dest, password="pw")


def test_split_file_wraps_unlink_failure_in_pack_error(tmp_path, monkeypatch):
    target = tmp_path / "big.zip"
    target.write_bytes(b"x" * 100)

    def failing_unlink(self, missing_ok=False):
        raise PermissionError(13, "Permission denied")

    # 파트는 정상적으로 쓰이고 원본 삭제만 실패하는 상황을 만든다.
    # 디렉토리 권한으로는 파트 쓰기가 먼저 막혀 이 분기에 도달하지 못한다.
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    # 삭제 실패가 raw OSError로 새면 봇 프로세스가 죽는다
    with pytest.raises(PackError, match="cannot split"):
        split_file(target, max_bytes=50)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_packer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.bot.packer'`

- [ ] **Step 3: packer.py 구현**

```python
"""전송용 AES-256 암호 ZIP을 만들고 필요하면 분할한다."""

from __future__ import annotations

import logging
from pathlib import Path

import pyzipper

from private_sync.errors import PackError

logger = logging.getLogger(__name__)

# 텔레그램 봇 sendDocument 한도가 50MB이므로 여유를 두고 45MB로 자른다
MAX_PART_BYTES = 45 * 1024 * 1024


def make_encrypted_zip(src: Path, dest_dir: Path, password: str) -> Path:
    """원본 파일 하나를 AES-256 암호 ZIP으로 포장한다.

    Args:
        src: 포장할 원본 파일.
        dest_dir: ZIP을 만들 디렉토리.
        password: ZIP 암호.

    Returns:
        생성된 ZIP 경로.

    Raises:
        PackError: 원본을 읽을 수 없거나 ZIP 생성에 실패했을 때.
    """
    archive = dest_dir / (src.name + ".zip")
    try:
        with pyzipper.AESZipFile(
            archive,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            zf.write(src, arcname=src.name)
    except OSError as exc:
        # 중간까지 쓰인 아카이브를 남기지 않는다
        archive.unlink(missing_ok=True)
        raise PackError(
            f"cannot read or write while packing {src.name}: {exc.strerror}"
        ) from exc

    logger.info("Packed %s into %s bytes", src.name, archive.stat().st_size)
    return archive


def split_file(path: Path, max_bytes: int = MAX_PART_BYTES) -> list[Path]:
    """파일을 max_bytes 단위로 잘라 .partNN 파일들을 만든다.

    원본은 남겨두지 않고 삭제한다. 반환 순서대로 이어붙이면 원본이 된다.

    Raises:
        PackError: 읽기·쓰기에 실패했을 때.
    """
    parts: list[Path] = []
    try:
        with path.open("rb") as source:
            index = 1
            while True:
                chunk = source.read(max_bytes)
                if not chunk:
                    break
                part = path.with_name(f"{path.name}.part{index:02d}")
                part.write_bytes(chunk)
                parts.append(part)
                index += 1

        # 파트가 모두 쓰인 뒤에 원본을 지운다. 삭제 실패도 PackError로 감싸
        # 호출자가 OSError를 따로 처리하지 않아도 되게 한다.
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise PackError(f"cannot split {path.name}: {exc.strerror}") from exc

    logger.info("Split %s into %d parts", path.name, len(parts))
    return parts


def pack_for_send(
    src: Path,
    dest_dir: Path,
    password: str,
    max_bytes: int = MAX_PART_BYTES,
) -> list[Path]:
    """전송할 파일 목록을 만든다. 한도를 넘으면 분할된 파트들을 돌려준다.

    Raises:
        PackError: 원본을 읽을 수 없거나 포장에 실패했을 때.
    """
    if not src.is_file():
        raise PackError("cannot read source file %s" % src.name)

    archive = make_encrypted_zip(src, dest_dir, password)
    if archive.stat().st_size <= max_bytes:
        return [archive]
    return split_file(archive, max_bytes)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_packer.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/packer.py tests/test_packer.py
git commit -m "feat: 암호 ZIP 포장과 분할 추가"
```

---

### Task 8: 봇 명령 디스패치 (순수 로직)

**Files:**
- Create: `src/private_sync/bot/handlers.py`
- Test: `tests/test_handlers.py`

**Interfaces:**
- Consumes: `private_sync.bot.store.Entry`, `private_sync.bot.store.parent_rel`
- Produces:
  - `private_sync.bot.handlers`: `TokenMap(limit: int = 500)` with `put(kind: str, rel: str) -> str`, `get(token: str) -> tuple[str, str] | None`; `Incoming(kind: str, chat_id: str, text: str, message_id: int | None, callback_id: str | None)` (frozen); `SendText(text: str, buttons: tuple[tuple[str, str], ...] = (), edit: bool = False)` (frozen); `SendFile(rel: str, caption: str)` (frozen); `Action = SendText | SendFile | None`; `Context(chat_id: str, tokens: TokenMap, lister: Callable[[str], list[Entry]], searcher: Callable[[str], list[Entry]])`; `extract(update: dict) -> Incoming | None`; `handle(incoming: Incoming, ctx: Context) -> Action`
  - `kind` 는 `"dir"` 또는 `"file"` 이다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_handlers.py`:

```python
from private_sync.bot.handlers import (
    Context,
    Incoming,
    SendFile,
    SendText,
    TokenMap,
    extract,
    handle,
)
from private_sync.bot.store import Entry

ROOT = [
    Entry(name="SKT 문서", rel="SKT 문서", is_dir=True, size=0),
    Entry(name="메모", rel="메모", is_dir=True, size=0),
]
SKT = [
    Entry(name="sub", rel="SKT 문서/sub", is_dir=True, size=0),
    Entry(name="계약서.docx", rel="SKT 문서/계약서.docx", is_dir=False, size=2048),
]


def _ctx(chat_id="123", listing=None, results=None):
    tree = {"": ROOT, "SKT 문서": SKT} if listing is None else listing
    return Context(
        chat_id=chat_id,
        tokens=TokenMap(),
        lister=lambda rel: tree.get(rel, []),
        searcher=lambda kw: results or [],
    )


def _message(text, chat_id="123"):
    return Incoming(
        kind="message", chat_id=chat_id, text=text, message_id=1, callback_id=None
    )


def test_start_lists_root_labels():
    action = handle(_message("/start"), _ctx())

    assert isinstance(action, SendText)
    assert [label for label, _ in action.buttons] == ["📁 SKT 문서", "📁 메모"]


def test_unauthorized_chat_is_ignored():
    action = handle(_message("/start", chat_id="999"), _ctx(chat_id="123"))

    assert action is None


def test_unknown_command_returns_usage():
    action = handle(_message("/whatever"), _ctx())

    assert isinstance(action, SendText)
    assert "/start" in action.text
    assert action.buttons == ()


def test_whitespace_only_message_returns_usage():
    # 공백만 보낸 메시지로 봇이 죽으면 안 된다
    action = handle(_message("   "), _ctx())

    assert isinstance(action, SendText)
    assert "/start" in action.text


def test_directory_button_lists_children_with_up_button():
    ctx = _ctx()
    start = handle(_message("/start"), ctx)
    skt_token = dict((label, data) for label, data in start.buttons)["📁 SKT 문서"]

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=skt_token,
            message_id=5,
            callback_id="cb1",
        ),
        ctx,
    )

    assert isinstance(action, SendText)
    assert action.edit is True
    labels = [label for label, _ in action.buttons]
    assert labels[0] == "⬆️ 상위"
    assert "📁 sub" in labels
    assert "📄 계약서.docx (2.0 KB)" in labels


def test_file_button_requests_send():
    ctx = _ctx()
    token = ctx.tokens.put("file", "SKT 문서/계약서.docx")

    action = handle(
        Incoming(
            kind="callback", chat_id="123", text=token, message_id=5, callback_id="cb"
        ),
        ctx,
    )

    assert action == SendFile(rel="SKT 문서/계약서.docx", caption="계약서.docx")


def test_expired_token_is_reported():
    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text="nosuchtoken",
            message_id=5,
            callback_id="cb",
        ),
        _ctx(),
    )

    assert isinstance(action, SendText)
    assert "만료" in action.text


def test_find_lists_matches():
    results = [Entry(name="계약서.docx", rel="SKT 문서/계약서.docx", is_dir=False, size=10)]
    action = handle(_message("/find 계약"), _ctx(results=results))

    assert [label for label, _ in action.buttons] == ["📄 계약서.docx (10 B)"]


def test_find_without_keyword_returns_usage():
    action = handle(_message("/find"), _ctx())

    assert "/find" in action.text
    assert action.buttons == ()


def test_find_with_no_results_says_so():
    action = handle(_message("/find 없는것"), _ctx(results=[]))

    assert "없습니다" in action.text
    assert action.buttons == ()


def test_token_map_evicts_oldest_beyond_limit():
    tokens = TokenMap(limit=2)
    first = tokens.put("file", "a")
    tokens.put("file", "b")
    tokens.put("file", "c")

    assert tokens.get(first) is None
    assert tokens.get(tokens.put("file", "d")) == ("file", "d")


def test_extract_reads_message_and_callback():
    message = extract(
        {"message": {"text": "/start", "chat": {"id": 123}, "message_id": 7}}
    )
    assert message == Incoming(
        kind="message", chat_id="123", text="/start", message_id=7, callback_id=None
    )

    callback = extract(
        {
            "callback_query": {
                "id": "cb9",
                "data": "tok",
                "message": {"chat": {"id": 123}, "message_id": 8},
            }
        }
    )
    assert callback == Incoming(
        kind="callback", chat_id="123", text="tok", message_id=8, callback_id="cb9"
    )

    assert extract({"edited_message": {}}) is None
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_handlers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.bot.handlers'`

- [ ] **Step 3: handlers.py 구현**

```python
"""텔레그램 명령·콜백을 처리하는 순수 로직.

파일시스템과 네트워크에 직접 접근하지 않는다. 저장소 조회는 Context에 주입된
콜러블을 통해서만 하므로 텔레그램 없이 전체 동작을 테스트할 수 있다.
"""

from __future__ import annotations

import logging
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from private_sync.bot.store import Entry, parent_rel

logger = logging.getLogger(__name__)

_USAGE = "사용법: /start 로 목록 보기, /find <키워드> 로 파일명 검색"
_TOKEN_BYTES = 6


class TokenMap:
    """짧은 토큰과 저장소 상대경로를 잇는다.

    텔레그램 callback_data 는 64바이트 제한이 있어 경로를 직접 담을 수 없다.
    토큰만 노출하므로 경로 조작 시도도 함께 차단된다.
    """

    def __init__(self, limit: int = 500) -> None:
        self._limit = limit
        self._entries: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def put(self, kind: str, rel: str) -> str:
        """(kind, rel)에 대한 토큰을 발급한다."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._entries[token] = (kind, rel)
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return token

    def get(self, token: str) -> tuple[str, str] | None:
        """토큰에 해당하는 (kind, rel)을 반환한다. 없으면 None."""
        return self._entries.get(token)


@dataclass(frozen=True)
class Incoming:
    """파싱된 텔레그램 입력."""

    kind: str
    chat_id: str
    text: str
    message_id: int | None
    callback_id: str | None


@dataclass(frozen=True)
class SendText:
    """텍스트(및 버튼)를 보내거나 기존 메시지를 수정하라는 지시."""

    text: str
    buttons: tuple[tuple[str, str], ...] = ()
    edit: bool = False


@dataclass(frozen=True)
class SendFile:
    """저장소의 파일을 포장해 보내라는 지시."""

    rel: str
    caption: str


Action = SendText | SendFile | None


@dataclass
class Context:
    """핸들러가 쓰는 주입 의존성."""

    chat_id: str
    tokens: TokenMap
    lister: Callable[[str], list[Entry]]
    searcher: Callable[[str], list[Entry]]


def extract(update: dict) -> Incoming | None:
    """텔레그램 update에서 처리 대상만 뽑는다. 대상이 아니면 None."""
    message = update.get("message")
    if isinstance(message, dict) and message.get("text"):
        chat_id = (message.get("chat") or {}).get("id")
        return Incoming(
            kind="message",
            chat_id=str(chat_id),
            text=str(message["text"]),
            message_id=message.get("message_id"),
            callback_id=None,
        )

    callback = update.get("callback_query")
    if isinstance(callback, dict) and callback.get("data"):
        inner = callback.get("message") or {}
        chat_id = (inner.get("chat") or {}).get("id")
        return Incoming(
            kind="callback",
            chat_id=str(chat_id),
            text=str(callback["data"]),
            message_id=inner.get("message_id"),
            callback_id=str(callback.get("id")),
        )

    return None


def format_size(size: int) -> str:
    """사람이 읽을 크기 문자열을 만든다."""
    if size < 1024:
        return "%d B" % size
    value = size / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return "%.1f %s" % (value, unit)
        value /= 1024
    return "%.1f GB" % value
```

이어서 버튼 생성과 디스패치:

```python
def _entry_button(entry: Entry, tokens: TokenMap) -> tuple[str, str]:
    """항목 하나를 (버튼 라벨, callback_data)로 만든다."""
    if entry.is_dir:
        return ("📁 %s" % entry.name, tokens.put("dir", entry.rel))
    label = "📄 %s (%s)" % (entry.name, format_size(entry.size))
    return (label, tokens.put("file", entry.rel))


def _browse(rel: str, ctx: Context, edit: bool) -> SendText:
    """디렉토리 내용을 버튼 목록으로 만든다."""
    entries = ctx.lister(rel)
    buttons: list[tuple[str, str]] = []

    parent = parent_rel(rel)
    if parent is not None:
        buttons.append(("⬆️ 상위", ctx.tokens.put("dir", parent)))
    buttons += [_entry_button(entry, ctx.tokens) for entry in entries]

    title = "📂 /%s" % rel if rel else "📂 저장소"
    if not entries:
        title = "%s\n(비어 있습니다)" % title
    return SendText(text=title, buttons=tuple(buttons), edit=edit)


def _find(keyword: str, ctx: Context) -> SendText:
    """검색 결과를 버튼 목록으로 만든다."""
    if not keyword:
        return SendText(text=_USAGE)

    results = ctx.searcher(keyword)
    if not results:
        return SendText(text="'%s' 와 일치하는 파일이 없습니다." % keyword)

    buttons = tuple(_entry_button(entry, ctx.tokens) for entry in results)
    return SendText(text="🔍 '%s' 검색 결과 %d건" % (keyword, len(results)), buttons=buttons)


def _handle_message(incoming: Incoming, ctx: Context) -> Action:
    """텍스트 명령을 처리한다."""
    parts = incoming.text.strip().split(maxsplit=1)
    if not parts:
        # 공백만 있는 메시지도 텔레그램에서는 유효한 text 로 도착한다
        return SendText(text=_USAGE)

    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/start", "/ls"):
        return _browse("", ctx, edit=False)
    if command == "/find":
        return _find(argument, ctx)
    return SendText(text=_USAGE)


def _handle_callback(incoming: Incoming, ctx: Context) -> Action:
    """버튼 콜백을 처리한다."""
    resolved = ctx.tokens.get(incoming.text)
    if resolved is None:
        # 봇 재시작이나 LRU 축출로 토큰이 사라진 경우다
        return SendText(text="목록이 만료되었습니다. /start 로 다시 시작해 주세요.")

    kind, rel = resolved
    if kind == "dir":
        return _browse(rel, ctx, edit=True)
    return SendFile(rel=rel, caption=rel.rsplit("/", 1)[-1])


def handle(incoming: Incoming, ctx: Context) -> Action:
    """입력을 인가 검사한 뒤 종류에 맞게 처리한다."""
    if incoming.chat_id != ctx.chat_id:
        logger.warning("Ignoring input from unauthorized chat %s", incoming.chat_id)
        return None

    if incoming.kind == "message":
        return _handle_message(incoming, ctx)
    return _handle_callback(incoming, ctx)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_handlers.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/handlers.py tests/test_handlers.py
git commit -m "feat: 봇 명령 디스패치 로직 추가"
```

---

### Task 9: 텔레그램 Bot API 래퍼

**Files:**
- Create: `src/private_sync/bot/telegram.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `private_sync.errors.TelegramError`
- Produces:
  - `private_sync.bot.telegram`: `LONG_POLL_SEC: int`, `_PostBody(data: dict, files: dict | None = None, timeout: int = 30)`, `_decode(method: str, response) -> dict`, `TelegramClient(token: str, session: requests.Session | None = None)` with `get_updates(offset: int | None) -> list[dict]`, `send_message(chat_id: str, text: str, buttons: tuple[tuple[str, str], ...] = ()) -> None`, `edit_message_text(chat_id: str, message_id: int, text: str, buttons: tuple[tuple[str, str], ...] = ()) -> None`, `send_document(chat_id: str, path: Path, caption: str = "") -> None`, `answer_callback(callback_id: str) -> None`
  - `build_keyboard(buttons: tuple[tuple[str, str], ...]) -> str | None` 도 노출한다 (JSON 문자열).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_telegram.py`:

```python
import json
import logging

import pytest
import requests

from private_sync.bot.telegram import TelegramClient, build_keyboard
from private_sync.errors import TelegramError


class _FakeResponse:
    def __init__(self, payload=None, ok=True, status_code=200):
        self._payload = payload or {"ok": True, "result": []}
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self._response = response or _FakeResponse()
        self._exc = exc

    def get(self, url, params=None, timeout=None):
        self.calls.append(("get", url, params))
        if self._exc:
            raise self._exc
        return self._response

    def post(self, url, data=None, files=None, timeout=None):
        self.calls.append(("post", url, data))
        if self._exc:
            raise self._exc
        return self._response


def test_build_keyboard_puts_one_button_per_row():
    raw = build_keyboard((("A", "t1"), ("B", "t2")))

    assert json.loads(raw) == {
        "inline_keyboard": [
            [{"text": "A", "callback_data": "t1"}],
            [{"text": "B", "callback_data": "t2"}],
        ]
    }


def test_build_keyboard_returns_none_when_empty():
    assert build_keyboard(()) is None


def test_get_updates_passes_offset_and_returns_result():
    session = _FakeSession(
        _FakeResponse({"ok": True, "result": [{"update_id": 5}]})
    )
    client = TelegramClient("tok", session=session)

    updates = client.get_updates(offset=4)

    assert updates == [{"update_id": 5}]
    _, url, params = session.calls[0]
    assert url.endswith("/getUpdates")
    assert params["offset"] == 4


def test_send_message_includes_keyboard():
    session = _FakeSession()
    client = TelegramClient("tok", session=session)

    client.send_message("123", "hello", buttons=(("A", "t1"),))

    _, url, data = session.calls[0]
    assert url.endswith("/sendMessage")
    assert data["chat_id"] == "123"
    assert json.loads(data["reply_markup"])["inline_keyboard"]


def test_network_error_message_omits_token():
    session = _FakeSession(exc=requests.ConnectionError("https://api.telegram.org/botSECRET/x failed"))
    client = TelegramClient("SECRET", session=session)

    with pytest.raises(TelegramError) as excinfo:
        client.send_message("123", "hi")

    # 예외 메시지에 토큰이 새면 안 된다
    assert "SECRET" not in str(excinfo.value)
    assert "ConnectionError" in str(excinfo.value)


def test_http_error_raises_telegram_error():
    session = _FakeSession(_FakeResponse(ok=False, status_code=403))
    client = TelegramClient("tok", session=session)

    with pytest.raises(TelegramError, match="403"):
        client.send_message("123", "hi")


def test_logging_the_exception_chain_does_not_leak_token(caplog):
    session = _FakeSession(
        exc=requests.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org'): /botSECRET/sendMessage refused"
        )
    )
    client = TelegramClient("SECRET", session=session)

    with caplog.at_level(logging.ERROR):
        try:
            client.send_message("123", "hi")
        except TelegramError:
            # CLAUDE.md 가 권하는 방식. 체인이 붙어 있으면 여기서 토큰이 샌다.
            logging.getLogger("probe").exception("send failed")

    assert "SECRET" not in caplog.text


def test_send_document_opens_file_and_posts(tmp_path):
    document = tmp_path / "a.zip"
    document.write_bytes(b"zip")
    session = _FakeSession()
    client = TelegramClient("tok", session=session)

    client.send_document("123", document, caption="a")

    _, url, data = session.calls[0]
    assert url.endswith("/sendDocument")
    assert data["caption"] == "a"
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_telegram.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.bot.telegram'`

- [ ] **Step 3: telegram.py 구현**

```python
"""텔레그램 Bot API 얇은 래퍼.

python-telegram-bot 을 쓰지 않고 raw HTTP만 사용한다. 서버가 아웃바운드로만
연결하는 롱폴링 구조를 그대로 유지하기 위함이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import requests

from private_sync.errors import TelegramError

# getUpdates 롱폴링 대기(초). 이 값만큼 봇 응답이 늦어질 수 있다.
LONG_POLL_SEC = 20

_API = "https://api.telegram.org/bot{token}/{method}"
_POST_TIMEOUT_SEC = 30
_UPLOAD_TIMEOUT_SEC = 300


class _Response(Protocol):
    """requests.Response 중 이 모듈이 쓰는 부분.

    테스트가 가짜 세션을 주입할 수 있도록 최소 표면만 선언한다.
    """

    ok: bool
    status_code: int

    def json(self) -> object: ...


@dataclass(frozen=True)
class _PostBody:
    """POST 요청 본문과 첨부."""

    data: dict[str, object]
    files: dict | None = None
    timeout: int = _POST_TIMEOUT_SEC


def _decode(method: str, response: _Response) -> dict:
    """응답을 검증하고 JSON 본문을 반환한다.

    Raises:
        TelegramError: 비정상 상태 코드, 비-JSON 본문, 예상 밖 페이로드.
    """
    if not response.ok:
        raise TelegramError(f"{method} returned status {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramError(f"{method} returned non-JSON body") from exc

    if not isinstance(payload, dict):
        raise TelegramError(f"{method} returned unexpected payload")
    return payload


def build_keyboard(buttons: tuple[tuple[str, str], ...]) -> str | None:
    """버튼 목록을 inline_keyboard JSON 문자열로 만든다. 비면 None."""
    if not buttons:
        return None
    rows = [[{"text": label, "callback_data": data}] for label, data in buttons]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


class TelegramClient:
    """Bot API 호출을 담당한다."""

    def __init__(self, token: str, session: requests.Session | None = None) -> None:
        self._token = token
        self._session = session or requests.Session()

    def _url(self, method: str) -> str:
        """메서드 호출 URL을 만든다. 이 문자열은 어떤 예외·로그에도 넣지 않는다."""
        return _API.format(token=self._token, method=method)

    def get_updates(self, offset: int | None) -> list[dict]:
        """롱폴링으로 새 update 목록을 가져온다.

        Raises:
            TelegramError: 네트워크 오류 또는 비정상 응답.
        """
        params: dict[str, object] = {"timeout": LONG_POLL_SEC}
        if offset is not None:
            params["offset"] = offset

        response = self._get("getUpdates", params, LONG_POLL_SEC + 10)
        result = response.get("result")
        if not isinstance(result, list):
            raise TelegramError("getUpdates returned unexpected payload")
        return result

    def send_message(
        self, chat_id: str, text: str, buttons: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """텍스트 메시지를 보낸다."""
        data: dict[str, object] = {"chat_id": chat_id, "text": text}
        keyboard = build_keyboard(buttons)
        if keyboard:
            data["reply_markup"] = keyboard
        self._post("sendMessage", _PostBody(data=data))

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """기존 메시지의 본문과 버튼을 바꾼다."""
        data: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        keyboard = build_keyboard(buttons)
        if keyboard:
            data["reply_markup"] = keyboard
        self._post("editMessageText", _PostBody(data=data))

    def send_document(self, chat_id: str, path: Path, caption: str = "") -> None:
        """파일을 문서로 보낸다.

        Raises:
            TelegramError: 파일을 열 수 없거나 전송이 실패했을 때.
        """
        try:
            with path.open("rb") as handle:
                self._post(
                    "sendDocument",
                    _PostBody(
                        data={"chat_id": chat_id, "caption": caption},
                        files={"document": (path.name, handle)},
                        timeout=_UPLOAD_TIMEOUT_SEC,
                    ),
                )
        except OSError as exc:
            # OSError 에는 토큰이 없으므로 체인을 유지해 디버깅 정보를 남긴다
            raise TelegramError(
                f"cannot read document {path.name}: {exc.strerror}"
            ) from exc

    def answer_callback(self, callback_id: str) -> None:
        """버튼 탭의 로딩 표시를 해제한다."""
        self._post(
            "answerCallbackQuery", _PostBody(data={"callback_query_id": callback_id})
        )

    def _get(self, method: str, params: dict, timeout: int) -> dict:
        """GET 요청을 보내고 JSON 본문을 반환한다."""
        try:
            response = self._session.get(
                self._url(method), params=params, timeout=timeout
            )
        except requests.RequestException as exc:
            # `from None` 으로 체인을 끊는다. requests 예외 메시지에는 요청
            # URL 이 담기고 그 URL 에는 봇 토큰이 들어 있어, 체인이 붙어 있으면
            # logger.exception 이나 미처리 트레이스백으로 토큰이 새어나간다.
            raise TelegramError(
                f"{method} request failed: {type(exc).__name__}"
            ) from None

        return _decode(method, response)

    def _post(self, method: str, body: _PostBody) -> dict:
        """POST 요청을 보내고 JSON 본문을 반환한다."""
        try:
            response = self._session.post(
                self._url(method),
                data=body.data,
                files=body.files,
                timeout=body.timeout,
            )
        except requests.RequestException as exc:
            # 체인을 끊는 이유는 _get 과 같다 (토큰이 담긴 URL 노출 방지)
            raise TelegramError(
                f"{method} request failed: {type(exc).__name__}"
            ) from None

        return _decode(method, response)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_telegram.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/bot/telegram.py tests/test_telegram.py
git commit -m "feat: 텔레그램 Bot API 래퍼 추가"
```

---

### Task 10: 봇 롱폴링 루프

**Files:**
- Create: `src/private_sync/bot/main.py`
- Test: `tests/test_bot_main.py`

**Interfaces:**
- Consumes: `load_bot_config`, `TelegramClient`, `handlers.extract`, `handlers.handle`, `handlers.Context`, `handlers.TokenMap`, `handlers.SendText`, `handlers.SendFile`, `store.list_dir`, `store.search`, `store.resolve_safe`, `packer.pack_for_send`, 예외들
- Produces:
  - `private_sync.bot.main`: `Deliverer(client: TelegramClient, config: BotConfig)` with `run(action: Action, incoming: Incoming) -> None`; `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_bot_main.py`:

```python
from pathlib import Path

import pytest

from private_sync.bot.handlers import Incoming, SendFile, SendText
from private_sync.bot.main import Deliverer
from private_sync.config import BotConfig
from private_sync.errors import TelegramError


class _SpyClient:
    def __init__(self, fail_on=None):
        self.messages = []
        self.edits = []
        self.documents = []
        self.answered = []
        self._fail_on = fail_on

    def send_message(self, chat_id, text, buttons=()):
        if self._fail_on == "message":
            raise TelegramError("sendMessage returned status 500")
        self.messages.append((chat_id, text, buttons))

    def edit_message_text(self, chat_id, message_id, text, buttons=()):
        self.edits.append((chat_id, message_id, text, buttons))

    def send_document(self, chat_id, path, caption=""):
        self.documents.append((chat_id, Path(path).name, caption))

    def answer_callback(self, callback_id):
        self.answered.append(callback_id)


@pytest.fixture
def config(tmp_path):
    store = tmp_path / "store"
    (store / "메모").mkdir(parents=True)
    (store / "메모" / "a.txt").write_bytes(b"hello")
    return BotConfig(store=store, token="tok", chat_id="123", zip_password="pw")


def _callback(text="tok"):
    return Incoming(
        kind="callback", chat_id="123", text=text, message_id=9, callback_id="cb1"
    )


def _message(text="/start"):
    return Incoming(
        kind="message", chat_id="123", text=text, message_id=1, callback_id=None
    )


def test_send_text_from_message_sends_new_message(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(SendText(text="hi", buttons=(("A", "t"),)), _message())

    assert client.messages == [("123", "hi", (("A", "t"),))]
    assert client.edits == []


def test_send_text_with_edit_updates_existing_message(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(SendText(text="dir", buttons=(), edit=True), _callback())

    assert client.edits == [("123", 9, "dir", ())]
    # 버튼 탭의 로딩 표시를 해제한다
    assert client.answered == ["cb1"]


def test_send_file_delivers_encrypted_zip(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(SendFile(rel="메모/a.txt", caption="a.txt"), _callback())

    assert len(client.documents) == 1
    chat_id, name, caption = client.documents[0]
    assert chat_id == "123"
    assert name == "a.txt.zip"
    assert "a.txt" in caption


def test_send_file_cleans_up_temp_files(config, monkeypatch):
    created = []

    import private_sync.bot.main as bot_main

    original = bot_main.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = original(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(bot_main.tempfile, "mkdtemp", tracking_mkdtemp)
    deliverer = Deliverer(_SpyClient(), config)

    deliverer.run(SendFile(rel="메모/a.txt", caption="a.txt"), _callback())

    assert created and not created[0].exists()


def test_missing_file_reports_to_user(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(SendFile(rel="메모/없음.txt", caption="없음.txt"), _callback())

    assert "동기화 대기 중" in client.messages[0][1]


def test_none_action_does_nothing(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(None, _message())

    assert client.messages == []
    assert client.documents == []
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_bot_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.bot.main'`

- [ ] **Step 3: main.py 구현**

```python
"""서버 측 봇 진입점. 롱폴링으로 명령을 받아 파일을 전달한다."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from private_sync.bot import store
from private_sync.bot.handlers import (
    Action,
    Context,
    Incoming,
    SendFile,
    SendText,
    TokenMap,
    extract,
    handle,
)
from private_sync.bot.packer import pack_for_send
from private_sync.bot.telegram import TelegramClient
from private_sync.config import BotConfig, load_bot_config
from private_sync.errors import ConfigError, PackError, StoreError, TelegramError

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path("~/.config/private-sync/bot.yaml").expanduser()
_ERROR_SLEEP_SEC = 3
_MISSING_FILE_MESSAGE = "파일을 찾을 수 없습니다. 동기화 대기 중이거나 삭제되었습니다."
_SPLIT_NOTICE = (
    "파일이 커서 %d개로 나눠 보냈습니다.\n"
    "PC에서 아래 명령으로 합친 뒤 비밀번호를 입력해 열어주세요.\n"
    "cat %s.part* > %s"
)


class Deliverer:
    """핸들러가 만든 Action을 실제 텔레그램 호출로 옮긴다."""

    def __init__(self, client: TelegramClient, config: BotConfig) -> None:
        self._client = client
        self._config = config

    def run(self, action: Action, incoming: Incoming) -> None:
        """Action을 실행한다. 실패는 로깅하고 사용자에게 알린다."""
        if incoming.callback_id:
            self._answer(incoming.callback_id)

        if action is None:
            return

        if isinstance(action, SendText):
            self._send_text(action, incoming)
            return

        self._send_file(action, incoming)

    def _answer(self, callback_id: str) -> None:
        """버튼 로딩 표시를 해제한다. 실패는 무시해도 무해하다."""
        try:
            self._client.answer_callback(callback_id)
        except TelegramError as exc:
            logger.warning("answerCallbackQuery failed: %s", exc)

    def _send_text(self, action: SendText, incoming: Incoming) -> None:
        try:
            if action.edit and incoming.message_id is not None:
                self._client.edit_message_text(
                    self._config.chat_id,
                    incoming.message_id,
                    action.text,
                    action.buttons,
                )
                return
            self._client.send_message(
                self._config.chat_id, action.text, action.buttons
            )
        except TelegramError as exc:
            logger.error("Failed to deliver text response: %s", exc)

    def _notify(self, text: str) -> None:
        """사용자에게 짧은 안내를 보낸다."""
        try:
            self._client.send_message(self._config.chat_id, text)
        except TelegramError as exc:
            logger.error("Failed to deliver notice: %s", exc)

    def _send_file(self, action: SendFile, _incoming: Incoming) -> None:
        """저장소 파일을 암호 ZIP으로 포장해 보낸다."""
        try:
            source = store.resolve_safe(self._config.store, action.rel)
        except StoreError as exc:
            logger.warning("Rejected file request %r: %s", action.rel, exc)
            self._notify(_MISSING_FILE_MESSAGE)
            return

        workdir = Path(tempfile.mkdtemp(prefix="private-sync-"))
        try:
            parts = pack_for_send(source, workdir, self._config.zip_password)
            for part in parts:
                self._client.send_document(
                    self._config.chat_id, part, caption=part.name
                )
            if len(parts) > 1:
                archive = source.name + ".zip"
                self._notify(_SPLIT_NOTICE % (len(parts), archive, archive))
            logger.info("Delivered %s as %d part(s)", action.rel, len(parts))
        except PackError as exc:
            logger.error("Packing failed for %s: %s", action.rel, exc)
            self._notify("파일을 포장하는 중 오류가 발생했습니다.")
        except TelegramError as exc:
            logger.error("Sending failed for %s: %s", action.rel, exc)
            self._notify("파일 전송에 실패했습니다. 다시 시도해 주세요.")
        finally:
            # 평문·암호문 임시 파일을 남기지 않는다
            shutil.rmtree(workdir, ignore_errors=True)
```

루프와 `main()`:

```python
def _build_context(config: BotConfig, tokens: TokenMap) -> Context:
    """저장소 조회를 주입한 핸들러 컨텍스트를 만든다."""
    return Context(
        chat_id=config.chat_id,
        tokens=tokens,
        lister=lambda rel: store.list_dir(config.store, rel),
        searcher=lambda keyword: store.search(config.store, keyword),
    )


def _serve(client: TelegramClient, config: BotConfig) -> None:
    """롱폴링 루프. 예외로 죽지 않는다."""
    tokens = TokenMap()
    context = _build_context(config, tokens)
    deliverer = Deliverer(client, config)
    offset: int | None = None

    logger.info("Bot started, serving store %s", config.store)
    while True:
        try:
            updates = client.get_updates(offset)
        except TelegramError as exc:
            logger.warning("getUpdates failed: %s", exc)
            time.sleep(_ERROR_SLEEP_SEC)
            continue

        for update in updates:
            offset = int(update.get("update_id", 0)) + 1
            incoming = extract(update)
            if incoming is None:
                continue
            try:
                action = handle(incoming, context)
            except StoreError as exc:
                logger.warning("Store error while handling input: %s", exc)
                deliverer.run(SendText(text=_MISSING_FILE_MESSAGE), incoming)
                continue
            deliverer.run(action, incoming)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="private-sync 텔레그램 봇")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """봇을 실행한다."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = load_bot_config(args.config)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    client = TelegramClient(config.token)
    try:
        _serve(client, config)
    except KeyboardInterrupt:
        logger.info("Bot stopped by signal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_bot_main.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: 전체 테스트와 커밋**

Run:
```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
```
Expected: 91 passed

```bash
git add src/private_sync/bot/main.py tests/test_bot_main.py
git commit -m "feat: 봇 롱폴링 루프 추가"
```

---

### Task 11: 배포 설정과 문서

**Files:**
- Create: `deploy/com.private-sync.agent.plist`
- Create: `deploy/private-sync-bot.service`
- Create: `README.md`
- Test: 수동 검증 (아래 Step 5~7)

**Interfaces:**
- Consumes: `private-sync-agent`, `private-sync-bot` 콘솔 스크립트
- Produces: 배포 산출물. 코드 인터페이스는 없다.

- [ ] **Step 1: macOS LaunchAgent 작성**

`deploy/com.private-sync.agent.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.private-sync.agent</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/CHANGEME/source/python/private-sync/.venv/bin/private-sync-agent</string>
    <string>--config</string>
    <string>/Users/CHANGEME/.config/private-sync/agent.yaml</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/Users/CHANGEME/Library/Logs/private-sync-agent.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/CHANGEME/Library/Logs/private-sync-agent.log</string>
</dict>
</plist>
```

`CHANGEME` 는 설치 시 실제 사용자명으로 바꾼다.

- [ ] **Step 2: systemd user 유닛 작성**

`deploy/private-sync-bot.service`:

```ini
[Unit]
Description=private-sync Telegram bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h
EnvironmentFile=%h/.config/private-sync/bot.env
ExecStart=%h/private-sync/.venv/bin/private-sync-bot --config %h/.config/private-sync/bot.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

`bot.env` 는 `chmod 600` 으로 두고 아래 세 줄만 넣는다.

```
PRIVATE_SYNC_BOT_TOKEN=...
PRIVATE_SYNC_CHAT_ID=...
PRIVATE_SYNC_ZIP_PASSWORD=...
```

- [ ] **Step 3: README 작성**

`README.md` 에 아래 내용을 담는다.

````markdown
# private-sync

노트북의 지정된 파일·디렉토리를 사내 DGX 서버로 자동 동기화하고, 텔레그램 봇으로
목록을 탐색해 암호 ZIP으로 내려받는다.

설계 문서: [docs/superpowers/specs/2026-07-29-private-sync-design.md](docs/superpowers/specs/2026-07-29-private-sync-design.md)

## 구조

- `agent` (노트북): watchdog으로 변경을 감지해 3초 디바운스 후 rsync over SSH로 업로드한다.
  삭제는 전파하지 않는다.
- `bot` (DGX 서버): 텔레그램 `getUpdates` 롱폴링으로 명령을 받는다. 서버는 아웃바운드
  연결만 사용하므로 인바운드 포트를 열지 않는다.

## 설치

```bash
git clone <repo> private-sync && cd private-sync
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## 노트북 설정

```bash
mkdir -p ~/.config/private-sync
cp config.example.yaml ~/.config/private-sync/agent.yaml
# agent.yaml 의 remote 와 sources 를 편집한다
.venv/bin/private-sync-agent --config ~/.config/private-sync/agent.yaml --debug
```

자동 시작:

```bash
sed "s|CHANGEME|$USER|g" deploy/com.private-sync.agent.plist \
  > ~/Library/LaunchAgents/com.private-sync.agent.plist
launchctl load ~/Library/LaunchAgents/com.private-sync.agent.plist
```

## 서버 설정

```bash
ssh dgson@ai
mkdir -p ~/private-sync/store ~/.config/private-sync
# 저장소 코드를 서버에 배치하고 venv 를 만든다
cp config.example.yaml ~/.config/private-sync/bot.yaml   # store 항목만 남긴다
install -m 600 /dev/null ~/.config/private-sync/bot.env  # 토큰·chat_id·ZIP 암호
cp deploy/private-sync-bot.service ~/.config/systemd/user/
systemctl --user enable --now private-sync-bot
```

`systemctl --user` 를 쓸 수 없으면 대신 crontab에 아래를 넣는다.

```
@reboot cd $HOME && set -a && . $HOME/.config/private-sync/bot.env && set +a && nohup $HOME/private-sync/.venv/bin/private-sync-bot --config $HOME/.config/private-sync/bot.yaml >> $HOME/private-sync-bot.log 2>&1 &
```

## 봇 사용법

- `/start` — 저장소 목록을 버튼으로 표시
- `/find <키워드>` — 파일명 부분일치 검색
- 파일 버튼 탭 — AES-256 암호 ZIP으로 받는다. 폰 압축 앱에서 비밀번호를 넣고 열면 된다.
- 45MB를 넘으면 `.partNN` 으로 나눠 오므로 PC에서 `cat 이름.zip.part* > 이름.zip` 으로 합친다.

## 보안

- 봇 토큰, chat_id, ZIP 암호는 환경변수로만 읽는다. YAML과 코드에 넣지 않는다.
- 텔레그램에는 항상 암호 ZIP만 나간다. 서버 저장소에는 평문으로 둔다(사내 서버는 규정상
  허용 저장소).
- 등록된 chat_id 외의 입력은 응답 없이 무시한다.
- 임시 ZIP은 전송 성공·실패와 무관하게 삭제된다.

## 개발

```bash
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```
````

- [ ] **Step 4: 전체 테스트와 커밋**

Run:
```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
```
Expected: 91 passed

```bash
git add README.md deploy
git commit -m "docs: 배포 설정과 사용 문서 추가"
```

- [ ] **Step 5: 노트북에서 수동 업로드 검증**

임시 소스로 실제 서버에 붙는지 확인한다.

```bash
mkdir -p /tmp/ps-test && echo hello > /tmp/ps-test/a.txt
cat > /tmp/ps-agent.yaml <<'EOF'
remote:
  host: dgson@ai
  store: ~/private-sync/store
sources:
  - label: 테스트
    paths:
      - /tmp/ps-test
EOF
ssh dgson@ai 'mkdir -p ~/private-sync/store'
.venv/bin/private-sync-agent --config /tmp/ps-agent.yaml --debug
```
다른 터미널에서 `echo world >> /tmp/ps-test/a.txt` 를 실행한 뒤 3~5초 기다린다.
Expected: 로그에 `Uploaded /tmp/ps-test under label 테스트` 가 찍힌다.

검증:
```bash
ssh dgson@ai 'cat ~/private-sync/store/테스트/ps-test/a.txt'
```
Expected: `hello` 와 `world` 두 줄

- [ ] **Step 6: 서버에서 봇 검증**

서버에 코드를 배치하고 환경변수를 설정한 뒤 봇을 포그라운드로 띄운다.

```bash
ssh dgson@ai
cd ~/private-sync
set -a && . ~/.config/private-sync/bot.env && set +a
.venv/bin/private-sync-bot --config ~/.config/private-sync/bot.yaml --debug
```

폰에서 순서대로 확인한다.
1. `/start` → `📁 테스트` 버튼이 보인다
2. 버튼을 타고 들어가 `📄 a.txt (12 B)` 를 탭한다
3. `a.txt.zip` 이 도착하고, 압축 앱에서 ZIP 암호를 넣으면 두 줄이 보인다
4. `/find a.txt` → 같은 파일이 검색된다

- [ ] **Step 7: systemd 가용성 확인과 정리**

Run (서버):
```bash
systemctl --user status 2>&1 | head -3
```
`Failed to connect to bus` 가 나오면 README의 crontab 방식을 쓴다. 결과를 README의
"서버 설정" 절에 한 줄로 기록한다.

정리:
```bash
rm -rf /tmp/ps-test /tmp/ps-agent.yaml
ssh dgson@ai 'rm -rf ~/private-sync/store/테스트'
```

README에 변경이 생겼으면 커밋한다.

```bash
git add README.md
git commit -m "docs: 서버 서비스 등록 방식 확인 결과 반영"
```

---

## Self-Review

**1. 스펙 커버리지**

| 스펙 요구사항 | 구현 태스크 |
|---|---|
| 설정: 디렉토리·파일 혼합 `paths`, 라벨 하나에 여러 경로 | Task 1 |
| 설정 검증: 경로 존재, 라벨 중복, 저장 경로 충돌, 환경변수 | Task 1 |
| 기본 제외 목록 + 사용자 `exclude` | Task 1(정의), Task 4(매칭) |
| 원격 저장소 `~` 정규화 | Task 1 |
| 미전송 목록 영속화 | Task 2 |
| rsync over SSH, `shell=True` 금지, 디렉토리 이름 유지 | Task 3 |
| 연결 실패(재시도) vs 권한·디스크(격리) 구분 | Task 3, Task 5 |
| 지수 백오프 3초→최대 5분 | Task 5 (`Backoff(base=3.0, cap=300.0)`) |
| watchdog 감시, 개별 파일은 부모 디렉토리 감시 | Task 4, Task 5 |
| 3초 디바운스 | Task 4(로직), Task 5(`_DEBOUNCE_SEC`) |
| 삭제 전파 없음 | Task 3(`--delete` 미사용), Task 5(deleted 이벤트 무시) |
| 저장소 트리 탐색, 파일명 검색 | Task 6 |
| 경로 탈출 차단, `Path.resolve()` 재확인 | Task 6 |
| 콜백에 토큰만 담기 (64바이트 제한) | Task 8 (`TokenMap`) |
| AES-256 암호 ZIP | Task 7 |
| 45MB 분할 + 결합 명령 안내 | Task 7(분할), Task 10(안내 메시지) |
| 임시 ZIP `finally` 삭제 | Task 10 |
| chat_id 화이트리스트, 미등록은 무응답 | Task 8 |
| 인라인 키보드 탐색, `⬆️ 상위`, `/find` | Task 8 |
| Bot API 래퍼 5개 메서드 | Task 9 |
| 토큰 유출 방지 (`type(exc).__name__`) | Task 9 |
| 롱폴링 루프가 예외로 죽지 않음 | Task 10 |
| 잘못된 update 무시 | Task 8(`extract` → None), Task 10 |
| "동기화 대기 중이거나 삭제됨" 안내 | Task 10 |
| LaunchAgent / systemd user, 대체 수단 | Task 11 |

누락 없음.

**2. 플레이스홀더 스캔**

TBD·TODO·"적절히 처리" 류 표현 없음. 모든 코드 스텝에 실제 코드가 있고, 모든 실행 스텝에 명령과 기대 결과가 있다. Task 11의 `CHANGEME` 는 사용자별 경로 치환 지점이며 Step 1과 Step 3의 `sed` 명령으로 처리 방법까지 명시했다.

**3. 타입 일관성 확인**

- `Source.exclude`, `WatchTarget.exclude`, `upload(..., exclude)` 모두 `tuple[str, ...]` 로 일치
- `Entry.rel` 은 Task 6에서 `str` (POSIX 상대경로)로 정의되고 Task 8·10에서 같은 형태로 쓰인다
- `TokenMap.get()` 반환 `tuple[str, str] | None` 이 Task 8 `_handle_callback` 의 사용과 일치
- `Runner` = `Callable[[list[str], int], CompletedProcess[str]]` 가 Task 3 테스트의 `runner(args, timeout)` 호출 형태와 일치
- `UploadFn` = `Callable[[RemoteConfig, str, Path, tuple[str, ...]], None]` 이 Task 5 테스트의 `lambda *_args: None` 과 호환
- `Action` = `SendText | SendFile | None` 이 Task 8 반환과 Task 10 `Deliverer.run` 파라미터에서 동일
- `PendingStore.discard` 이름이 Task 2·5에서 일관 (`remove` 와 혼용하지 않음)
- Task 5에서 추가한 `SyncWorker.has_pending()` 을 Interfaces에도 반영했다

불일치 없음.
