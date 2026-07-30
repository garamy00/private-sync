# agent 설정 자동 리로드 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agent.yaml`을 저장하면 데몬 재시작 없이 감시 대상·exclude·remote 설정이 반영되게 한다.

**Architecture:** 이미 1초마다 도는 `_run_loop`에 설정 파일 mtime 확인을 얹는다. 변경이 감지되면 새 설정을 읽어 감시 대상, observer 등록, 이벤트 핸들러, `SyncWorker`를 함께 교체한다. 읽기나 검증이 실패하면 아무것도 바꾸지 않고 이전 설정으로 계속 돈다.

**Tech Stack:** Python 3.11+, watchdog, PyYAML, pytest, ruff

**Spec:** [docs/superpowers/specs/2026-07-30-config-reload-design.md](../specs/2026-07-30-config-reload-design.md)

## Global Constraints

- Python `requires-python = ">=3.11"`. 타입 힌트는 `X | None` 형식을 쓴다 (`Optional[X]` 금지).
- ruff `line-length = 88`. 커밋 전 `ruff format` 과 `ruff check` 를 통과해야 한다. `pyproject.toml` 에 `[tool.ruff.lint]` 를 추가하지 않는다 (ruff 기본 415개 규칙 유지).
- 새 `# noqa` 를 추가하지 않는다. `bot/main.py` 의 기존 `# noqa: BLE001` 한 줄은 그대로 둔다.
- 모든 public 함수에 타입 힌트와 Google Style docstring을 작성한다. 1줄 docstring은 명령형으로 쓰고 마침표로 끝낸다.
- 로그는 `logging` 모듈만 사용한다. 로그 호출은 lazy args 를 쓴다: `logger.info("msg %s", value)`.
- **로그가 아닌 문자열은 f-string 으로 만든다.** `"text %s" % value` 형태의 `%` 연산자는 쓰지 않는다.
- 로그 메시지는 영문, 주석과 docstring은 한국어로 작성한다.
- 예외는 구체적 타입만 잡는다. bare `except:` 와 `except Exception:` 금지.
- 구조 있는 데이터는 `dict` 대신 dataclass를 쓴다.
- 파라미터는 3개 이하로 유지한다. 초과하면 dataclass로 묶는다.
- `.venv/bin/pytest` 와 `.venv/bin/ruff` 를 쓴다. venv를 새로 만들거나 `pip install` 을 실행하지 않는다.
- 네트워크·SSH 접근을 시도하지 않는다.
- 커밋 메시지는 `<type>: <요약>` 형식. 본문 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` 를 넣는다.

---

### Task 1: 설정 파일 변경 감지

**Files:**
- Create: `src/private_sync/agent/config_watch.py`
- Test: `tests/test_config_watch.py`

**Interfaces:**
- Consumes: 없음
- Produces: `private_sync.agent.config_watch.ConfigWatcher(path: Path)` with `changed() -> bool`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_config_watch.py`:

```python
import logging
import os

from private_sync.agent.config_watch import ConfigWatcher


def _write(path, text="a"):
    path.write_text(text, encoding="utf-8")
    return path


def test_first_check_reports_no_change(tmp_path):
    watcher = ConfigWatcher(_write(tmp_path / "agent.yaml"))

    assert watcher.changed() is False


def test_change_is_detected_exactly_once(tmp_path):
    config = _write(tmp_path / "agent.yaml")
    watcher = ConfigWatcher(config)

    # mtime 해상도에 기대지 않도록 직접 설정한다
    os.utime(config, (2_000_000_000, 2_000_000_000))

    assert watcher.changed() is True
    # 같은 상태를 반복해서 리로드하면 안 된다
    assert watcher.changed() is False


def test_missing_file_reports_no_change(tmp_path):
    config = _write(tmp_path / "agent.yaml")
    watcher = ConfigWatcher(config)
    config.unlink()

    # 설정이 사라졌다고 이전 설정을 버리지는 않는다
    assert watcher.changed() is False


def test_file_restored_after_deletion_is_detected(tmp_path):
    config = _write(tmp_path / "agent.yaml")
    watcher = ConfigWatcher(config)
    config.unlink()
    watcher.changed()
    _write(config, "b")

    assert watcher.changed() is True


def test_watcher_on_never_existing_file_does_not_raise(tmp_path):
    watcher = ConfigWatcher(tmp_path / "없음.yaml")

    assert watcher.changed() is False


def test_missing_file_warns_only_once(tmp_path, caplog):
    config = _write(tmp_path / "agent.yaml")
    watcher = ConfigWatcher(config)
    config.unlink()

    with caplog.at_level(logging.WARNING):
        watcher.changed()
        watcher.changed()

    # 매 틱마다 같은 경고를 쌓으면 로그가 못 쓰게 된다
    assert caplog.text.count("is unreadable") == 1
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_config_watch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'private_sync.agent.config_watch'`

- [ ] **Step 3: config_watch.py 구현**

```python
"""설정 파일의 변경을 mtime으로 감지한다."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfigWatcher:
    """설정 파일의 mtime 변화만 판단한다.

    watchdog으로 단일 파일을 감시하지 않는 이유는, 대부분의 에디터가 임시 파일에
    쓴 뒤 rename으로 교체해 inode가 바뀌기 때문이다. 이미 1초마다 도는 루프에
    stat() 한 번을 얹는 편이 단순하고 실패 모드도 적다.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime = self._read_mtime()

    def changed(self) -> bool:
        """직전 확인 이후 설정 파일이 바뀌었으면 True를 반환한다."""
        current = self._read_mtime()

        if current is None:
            if self._mtime is not None:
                logger.warning(
                    "Config file %s is unreadable, keeping current settings", self._path
                )
                self._mtime = None
            return False

        if current == self._mtime:
            return False

        self._mtime = current
        return True

    def _read_mtime(self) -> float | None:
        """설정 파일의 mtime을 읽는다. 읽을 수 없으면 None."""
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_config_watch.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/agent/config_watch.py tests/test_config_watch.py
git commit -m "feat: 설정 파일 변경 감지 추가"
```

---

### Task 2: 런타임 교체 지점

**Files:**
- Modify: `src/private_sync/agent/main.py` (`SyncWorker`, `_EventHandler`)
- Test: `tests/test_agent_main.py`

**Interfaces:**
- Consumes: `private_sync.config.AgentConfig`, `private_sync.agent.watcher.WatchTarget`
- Produces: `SyncWorker.replace_config(config: AgentConfig) -> None`, `_EventHandler.replace_targets(targets: list[WatchTarget]) -> None`

- [ ] **Step 1: 실패하는 테스트 작성**

먼저 `tests/test_agent_main.py` 의 기존 헬퍼에 라벨 인자를 더한다. 기본값이 있으므로 기존 호출은 그대로 동작한다.

```python
def _config(path: Path, label: str = "문서") -> AgentConfig:
    return AgentConfig(
        remote=REMOTE,
        sources=(Source(label=label, paths=(path,), exclude=()),),
    )
```

`import logging` 을 테스트 파일 상단에 추가한다 (`import threading` 앞).

그다음 아래 세 테스트를 `test_backoff_grows_then_resets` 바로 앞에 넣는다.

```python
def test_replace_config_applies_the_new_label(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("a", encoding="utf-8")
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()
    calls = []
    worker = SyncWorker(
        _config(doc, label="이전"), pending, uploader=lambda *args: calls.append(args)
    )

    worker.replace_config(_config(doc, label="이후"))
    worker.enqueue("이후", doc)
    worker.drain()

    assert [args[1] for args in calls] == ["이후"]


def test_replace_config_drops_items_of_removed_labels(tmp_path, caplog):
    doc = tmp_path / "a.md"
    doc.write_text("a", encoding="utf-8")
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()
    calls = []
    worker = SyncWorker(
        _config(doc, label="이전"), pending, uploader=lambda *args: calls.append(args)
    )
    worker.enqueue("이전", doc)

    worker.replace_config(_config(doc, label="이후"))
    with caplog.at_level(logging.WARNING):
        worker.drain()

    # 빈 목록만 보면 정상 업로드와 구분되지 않는다. 올리지 않고 버렸는지를 본다.
    assert calls == []
    assert "unknown label 이전" in caplog.text
    assert pending.items() == []


def test_replace_targets_changes_what_the_handler_matches(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    new = tmp_path / "new"
    new.mkdir()
    changed = new / "a.md"
    changed.write_text("a", encoding="utf-8")
    handler, state = _handler_state(Source(label="이전", paths=(old,), exclude=()))

    handler.replace_targets(
        build_targets((Source(label="이후", paths=(new,), exclude=()),))
    )
    handler.on_any_event(_FakeEvent(str(changed)))

    assert _released(state) == [("이후", str(new))]
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_agent_main.py -v`
Expected: FAIL — `AttributeError: 'SyncWorker' object has no attribute 'replace_config'`

- [ ] **Step 3: 교체 메서드 구현**

`SyncWorker` 안, `has_pending` 바로 뒤에 넣는다.

```python
    def replace_config(self, config: AgentConfig) -> None:
        """설정을 교체한다. 대기 목록과 backoff 상태는 그대로 둔다.

        사라진 라벨의 대기 항목은 여기서 손대지 않는다. drain이 이미 알 수 없는
        라벨을 폐기하므로 중복해서 처리할 이유가 없다.
        """
        self._config = config
        self._excludes = {s.label: s.exclude for s in config.sources}
```

`_EventHandler` 안, `on_any_event` 바로 뒤에 넣는다.

```python
    def replace_targets(self, targets: list[WatchTarget]) -> None:
        """감시 대상 목록을 교체한다.

        observer 스레드가 `on_any_event`에서 이 목록을 읽는다. 완성된 새 리스트로
        속성을 통째로 갈아끼우기만 하므로, 읽는 쪽은 항상 옛 목록이나 새 목록 중
        하나를 온전히 보게 된다.
        """
        self._targets = targets
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_agent_main.py -v`
Expected: PASS (15 passed)

- [ ] **Step 5: 린트와 커밋**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
git add src/private_sync/agent/main.py tests/test_agent_main.py
git commit -m "feat: SyncWorker와 이벤트 핸들러에 교체 지점 추가"
```

---

### Task 3: 리로드 조립과 루프 연결

**Files:**
- Modify: `src/private_sync/agent/main.py` (`AgentRuntime`, `_schedule_targets`, `_log_label_changes`, `_reload_config`, `_run_loop`, `main`)
- Test: `tests/test_agent_main.py`

**Interfaces:**
- Consumes: `ConfigWatcher`, `SyncWorker.replace_config`, `_EventHandler.replace_targets`, `build_targets`, `load_agent_config`
- Produces: `AgentRuntime(worker, handler, observer, config_path, targets, watcher)` (dataclass), `_reload_config(runtime: AgentRuntime) -> bool`, `_run_loop(runtime: AgentRuntime, state: LoopState) -> None`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_agent_main.py` 끝에 추가한다. 임포트에 `AgentRuntime`, `_reload_config`, `load_agent_config`, `ConfigWatcher` 를 더한다.

```python
class _FakeObserver:
    """schedule 호출 순서만 기록하는 가짜 observer."""

    def __init__(self):
        self.calls = []

    def unschedule_all(self):
        self.calls.append("unschedule_all")

    def schedule(self, handler, path, recursive=False):
        self.calls.append(("schedule", path, recursive))


def _yaml(label, path):
    return f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: {label}
    paths:
      - {path}
"""


def _runtime(tmp_path, label, watched):
    """리로드를 시험할 최소 런타임과 루프 상태를 만든다."""
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(_yaml(label, watched), encoding="utf-8")
    config = load_agent_config(config_path)
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()
    targets = build_targets(config.sources)
    state = LoopState(
        debouncer=Debouncer(_DEBOUNCE_SEC),
        lock=threading.Lock(),
        stop=threading.Event(),
    )
    runtime = AgentRuntime(
        worker=SyncWorker(config, pending, uploader=lambda *_a: None),
        handler=_EventHandler(targets, state),
        observer=_FakeObserver(),
        config_path=config_path,
        targets=targets,
        watcher=ConfigWatcher(config_path),
    )
    return runtime, pending, state


def test_reload_applies_the_new_label(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    runtime, _pending, _state = _runtime(tmp_path, "이전", docs)

    runtime.config_path.write_text(_yaml("이후", docs), encoding="utf-8")

    assert _reload_config(runtime) is True
    assert [t.label for t in runtime.targets] == ["이후"]


def test_reload_queues_only_newly_added_targets(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    runtime, pending, _state = _runtime(tmp_path, "문서", docs)
    runtime.config_path.write_text(
        f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: 문서
    paths:
      - {docs}
      - {extra}
""",
        encoding="utf-8",
    )

    _reload_config(runtime)

    # 이미 동기화된 대상을 다시 훑을 이유가 없다
    assert pending.items() == [PendingItem(label="문서", path=str(extra))]


def test_invalid_config_keeps_previous_settings(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    runtime, _pending, _state = _runtime(tmp_path, "이전", docs)
    before = runtime.targets

    runtime.config_path.write_text("sources: [", encoding="utf-8")

    # 저장 도중에 읽혔거나 오타일 뿐이므로 데몬은 계속 돌아야 한다
    assert _reload_config(runtime) is False
    assert runtime.targets is before
    assert runtime.observer.calls == []


def test_removed_label_is_warned_about(tmp_path, caplog):
    docs = tmp_path / "docs"
    docs.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    runtime, _pending, _state = _runtime(tmp_path, "사라질라벨", docs)
    runtime.config_path.write_text(_yaml("남을라벨", other), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        _reload_config(runtime)

    assert "사라질라벨" in caplog.text


def test_reload_reregisters_observer_watches(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    runtime, _pending, _state = _runtime(tmp_path, "문서", docs)
    runtime.config_path.write_text(_yaml("문서", other), encoding="utf-8")

    _reload_config(runtime)

    # 기존 등록을 먼저 지우고 새 경로로 다시 등록해야 한다
    assert runtime.observer.calls[0] == "unschedule_all"
    assert ("schedule", str(other), True) in runtime.observer.calls


def test_reregistration_failure_stops_the_daemon(tmp_path, caplog):
    docs = tmp_path / "docs"
    docs.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    runtime, _pending, state = _runtime(tmp_path, "문서", docs)

    class _ExplodingObserver(_FakeObserver):
        def schedule(self, handler, path, recursive=False):
            raise OSError(24, "Too many open files")

    runtime.observer = _ExplodingObserver()
    runtime.config_path.write_text(_yaml("문서", other), encoding="utf-8")
    # mtime 이 그대로면 리로드가 돌지 않아 루프가 영원히 돈다
    os.utime(runtime.config_path, (2_000_000_000, 2_000_000_000))

    with caplog.at_level(logging.CRITICAL):
        _run_loop(runtime, state)

    # 감시가 하나도 없는 채로 계속 도는 것이 최악이다. 시끄럽게 멈춰야 한다.
    assert state.stop.is_set()
    assert "Cannot re-register watches" in caplog.text
```

`import os` 를 테스트 파일 상단에 추가하고, 임포트에 `_run_loop` 을 더한다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/pytest tests/test_agent_main.py -v`
Expected: FAIL — `ImportError: cannot import name 'AgentRuntime'`

- [ ] **Step 3: 리로드 조립 구현**

임포트에 `from watchdog.observers.api import BaseObserver` 와 `from private_sync.agent.config_watch import ConfigWatcher` 를 더한다.

`LoopState` 바로 뒤, `_EventHandler` 앞에 넣는다. `_EventHandler` 를 참조하므로 타입 힌트는 문자열로 쓴다 (`from __future__ import annotations` 가 이미 있어 그대로 두면 된다).

```python
@dataclass
class AgentRuntime:
    """리로드할 때 함께 갱신되는 구성요소 묶음.

    넷 이상을 함께 바꿔야 상태가 어긋나지 않으므로 개별 인자로 넘기지 않는다.
    """

    worker: SyncWorker
    handler: _EventHandler
    observer: BaseObserver
    config_path: Path
    targets: list[WatchTarget]
    watcher: ConfigWatcher
```

`queue_initial_sync` 바로 뒤에 아래 셋을 넣는다.

```python
def _schedule_targets(
    observer: BaseObserver, handler: _EventHandler, targets: list[WatchTarget]
) -> None:
    """observer의 기존 등록을 모두 지우고 새 대상으로 다시 등록한다."""
    observer.unschedule_all()
    for watch_dir, recursive in {(t.watch_dir, t.recursive) for t in targets}:
        observer.schedule(handler, str(watch_dir), recursive=recursive)


def _log_label_changes(before: list[WatchTarget], after: list[WatchTarget]) -> None:
    """설정에서 사라진 라벨을 경고로 남긴다.

    서버의 해당 폴더는 지우지 않는다. 삭제를 전파하지 않는 기존 원칙대로이며,
    오타 한 번으로 백업본이 사라지지 않게 하기 위함이다.
    """
    removed = {t.label for t in before} - {t.label for t in after}
    for label in sorted(removed):
        logger.warning("Label %s left the config, its files stay on the server", label)


def _reload_config(runtime: AgentRuntime) -> bool:
    """설정을 다시 읽어 런타임 전체에 적용한다.

    실패하면 아무것도 바꾸지 않고 False를 반환한다. 저장 도중에 읽혔거나 오타가
    있을 뿐이므로 데몬은 계속 돌아야 한다. 고쳐서 저장하면 mtime이 다시 바뀌므로
    자동으로 재시도된다.
    """
    try:
        config = load_agent_config(runtime.config_path)
    except ConfigError as exc:
        logger.error("Keeping previous settings, config reload failed: %s", exc)
        return False

    targets = build_targets(config.sources)
    known = {(t.label, str(t.path)) for t in runtime.targets}
    added = [t for t in targets if (t.label, str(t.path)) not in known]

    runtime.worker.replace_config(config)
    runtime.handler.replace_targets(targets)
    _schedule_targets(runtime.observer, runtime.handler, targets)
    _log_label_changes(runtime.targets, targets)
    runtime.targets = targets

    # 새로 추가된 대상만 훑는다. 추가하고도 건드리기 전까지 안 올라가면
    # 초기 동기화로 막아둔 문제가 되살아난다.
    for target in added:
        runtime.worker.enqueue(target.label, target.path)

    logger.info(
        "Config reloaded: %d target(s), %d newly queued", len(targets), len(added)
    )
    return True
```

`_run_loop` 을 아래로 교체한다.

```python
def _run_loop(runtime: AgentRuntime, state: LoopState) -> None:
    """설정 변경을 반영하고, 디바운스가 끝난 항목을 큐에 넣어 업로드한다."""
    while not state.stop.is_set():
        if runtime.watcher.changed():
            try:
                _reload_config(runtime)
            except OSError as exc:
                # 기존 watch를 이미 해제한 뒤라 감시가 하나도 없는 채로 남는다.
                # 정상인 척하며 아무것도 동기화하지 않느니 시끄럽게 멈춘다.
                logger.critical(
                    "Cannot re-register watches after reload, stopping: %s",
                    type(exc).__name__,
                )
                state.stop.set()
                break

        with state.lock:
            ready = state.debouncer.due(time.monotonic())
        for label, path in ready:
            runtime.worker.enqueue(label, Path(path))

        runtime.worker.drain()

        # 대기 항목이 남았다면 오프라인이므로 backoff만큼 쉬고 다시 시도한다
        wait_sec = (
            runtime.worker.backoff.delay()
            if runtime.worker.has_pending()
            else _TICK_SEC
        )
        state.stop.wait(wait_sec)
```

`main()` 에서 observer 등록 부분과 루프 호출을 아래로 교체한다. `queue_initial_sync` 호출 위치는 그대로 `observer.start()` 뒤다.

```python
    observer = Observer()
    handler = _EventHandler(targets, state)
    _schedule_targets(observer, handler, targets)

    runtime = AgentRuntime(
        worker=worker,
        handler=handler,
        observer=observer,
        config_path=args.config,
        targets=targets,
        watcher=ConfigWatcher(args.config),
    )

    signal.signal(signal.SIGTERM, lambda *_: state.stop.set())
    signal.signal(signal.SIGINT, lambda *_: state.stop.set())

    observer.start()
    logger.info("Agent started with %d watch targets", len(targets))
    queue_initial_sync(worker, targets)
    try:
        _run_loop(runtime, state)
    finally:
        observer.stop()
        observer.join(timeout=5)
        logger.info("Agent stopped")
    return 0
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_agent_main.py -v`
Expected: PASS (21 passed)

- [ ] **Step 5: 전체 테스트와 린트**

Run:
```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/pytest -q
```
Expected: 125 passed

- [ ] **Step 6: 실제 데몬으로 확인**

임시 설정으로 데몬을 띄운 뒤, 데몬이 도는 동안 설정을 고쳐 리로드 로그가 나오는지 본다.
SSH가 없는 환경이면 업로드는 실패하겠지만 리로드 자체는 확인할 수 있다.

```bash
mkdir -p /tmp/reload-a /tmp/reload-b
cat > /tmp/reload.yaml <<'EOF'
remote:
  host: dgson@ai
  store: ~/private-sync/store
sources:
  - label: 첫번째
    paths:
      - /tmp/reload-a
EOF
.venv/bin/private-sync-agent --config /tmp/reload.yaml --state /tmp/reload-state.json --debug
```

다른 터미널에서 설정에 두 번째 source를 추가한다.

```bash
cat >> /tmp/reload.yaml <<'EOF'
  - label: 두번째
    paths:
      - /tmp/reload-b
EOF
```

Expected: 몇 초 안에 `Config reloaded: 2 target(s), 1 newly queued` 로그가 나온다.

이어서 설정을 일부러 깨뜨린다.

```bash
echo "sources: [" >> /tmp/reload.yaml
```

Expected: `Keeping previous settings, config reload failed: ...` 로그가 나오고 데몬은 계속 돈다.

정리:
```bash
rm -r /tmp/reload-a /tmp/reload-b
rm /tmp/reload.yaml /tmp/reload-state.json
```

- [ ] **Step 7: README 갱신과 커밋**

README의 agent 설명에 두세 문장을 더한다: `agent.yaml`을 저장하면 몇 초 안에 자동으로 반영되므로 재시작이 필요 없다. 설정에 오류가 있으면 이전 설정을 유지한 채 로그에 남기고 계속 동작하며, 고쳐서 저장하면 다시 반영된다. 라벨을 지워도 서버의 기존 파일은 그대로 남는다.

```bash
git add src/private_sync/agent/main.py tests/test_agent_main.py README.md
git commit -m "feat: agent 설정 변경 시 자동 리로드"
```

---

## Self-Review

**1. 스펙 커버리지**

| 스펙 요구사항 | 구현 태스크 |
|---|---|
| mtime 폴링으로 감지 | Task 1 |
| 첫 확인은 변경 없음, 같은 값 반복 감지 안 함 | Task 1 |
| 설정 파일이 사라져도 예외 없이 이전 설정 유지 | Task 1 |
| 감시 대상 재계산 | Task 3 (`build_targets`) |
| observer `unschedule_all` 후 재등록 | Task 3 (`_schedule_targets`) |
| 이벤트 핸들러 대상 교체 | Task 2 (`replace_targets`) |
| SyncWorker remote·exclude 교체 | Task 2 (`replace_config`) |
| 새로 추가된 대상만 초기 동기화 | Task 3 |
| 사라진 라벨 경고, 서버 파일 유지 | Task 3 (`_log_label_changes`) |
| 잘못된 설정에서 이전 설정 유지·데몬 계속 | Task 3 (`_reload_config` → False) |
| 대기 목록의 사라진 라벨 항목 폐기 | 기존 `drain` 동작, Task 2에서 테스트로 고정 |
| observer 재등록 실패 시 CRITICAL 후 종료 | Task 3 (`_run_loop`) |
| `_run_loop(runtime, state)` 2인자 | Task 3 |

누락 없음.

**2. 플레이스홀더 스캔**

TBD·TODO·"적절히 처리" 류 표현 없음. 모든 코드 스텝에 실제 코드가, 모든 실행 스텝에 명령과 기대 결과가 있다.

**3. 타입 일관성**

- `ConfigWatcher.changed() -> bool` 가 Task 1 정의와 Task 3 사용에서 일치
- `replace_config(config: AgentConfig)` / `replace_targets(targets: list[WatchTarget])` 가 Task 2 정의와 Task 3 호출에서 일치
- `AgentRuntime` 필드 여섯 개가 Task 3의 생성부(`_runtime` 헬퍼, `main`)와 사용부(`_reload_config`, `_run_loop`)에서 동일
- `_schedule_targets(observer, handler, targets)` 인자 순서가 Task 3의 두 호출부에서 동일
- `_reload_config` 는 `bool` 을 반환하고 테스트가 `is True` / `is False` 로 단언

불일치 없음.
