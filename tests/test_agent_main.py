import logging
import threading
import time
from pathlib import Path

from private_sync.agent.config_watch import ConfigWatcher
from private_sync.agent.main import (
    _DEBOUNCE_SEC,
    AgentRuntime,
    Backoff,
    LoopState,
    SyncWorker,
    _EventHandler,
    _reload_config,
    queue_initial_sync,
)
from private_sync.agent.pending import PendingItem, PendingStore
from private_sync.agent.watcher import Debouncer, build_targets
from private_sync.config import AgentConfig, RemoteConfig, Source, load_agent_config
from private_sync.errors import RetryableUploadError, UploadError

REMOTE = RemoteConfig(host="dgson@ai", store="store")


def _config(path: Path, label: str = "문서") -> AgentConfig:
    return AgentConfig(
        remote=REMOTE,
        sources=(Source(label=label, paths=(path,), exclude=()),),
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


def test_initial_sync_queues_every_configured_target(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    quote = tmp_path / "견적서.xlsx"
    quote.write_text("q", encoding="utf-8")
    config = AgentConfig(
        remote=REMOTE,
        sources=(Source(label="문서", paths=(docs, quote), exclude=()),),
    )
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()
    worker = SyncWorker(config, pending, uploader=lambda *_args: None)

    queue_initial_sync(worker, build_targets(config.sources))

    # 디렉토리와 개별 파일 모두, 변경 이벤트 없이 대기 목록에 들어가야 한다
    assert set(pending.items()) == {
        PendingItem(label="문서", path=str(docs)),
        PendingItem(label="문서", path=str(quote)),
    }


def test_initial_sync_targets_are_uploaded_and_cleared(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    quote = tmp_path / "견적서.xlsx"
    quote.write_text("q", encoding="utf-8")
    config = AgentConfig(
        remote=REMOTE,
        sources=(Source(label="문서", paths=(docs, quote), exclude=()),),
    )
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()
    calls = []
    worker = SyncWorker(config, pending, uploader=lambda *args: calls.append(args))

    queue_initial_sync(worker, build_targets(config.sources))
    worker.drain()

    uploaded_paths = {call[2] for call in calls}
    assert uploaded_paths == {docs, quote}
    assert pending.items() == []


class _FakeEvent:
    """watchdog FileSystemEvent 를 흉내내는 최소 객체."""

    def __init__(
        self, src_path, event_type="modified", is_directory=False, dest_path=""
    ):
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
    """리로드를 시험할 최소 런타임을 만든다."""
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
    return runtime, pending


def test_reload_applies_the_new_label(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    runtime, _pending = _runtime(tmp_path, "이전", docs)

    runtime.config_path.write_text(_yaml("이후", docs), encoding="utf-8")

    assert _reload_config(runtime) is True
    assert [t.label for t in runtime.targets] == ["이후"]


def test_reload_queues_only_newly_added_targets(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    runtime, pending = _runtime(tmp_path, "문서", docs)
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
    runtime, _pending = _runtime(tmp_path, "이전", docs)
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
    runtime, _pending = _runtime(tmp_path, "사라질라벨", docs)
    runtime.config_path.write_text(_yaml("남을라벨", other), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        _reload_config(runtime)

    assert "사라질라벨" in caplog.text


def test_reload_reregisters_observer_watches(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    runtime, _pending = _runtime(tmp_path, "문서", docs)
    runtime.config_path.write_text(_yaml("문서", other), encoding="utf-8")

    _reload_config(runtime)

    # 기존 등록을 먼저 지우고 새 경로로 다시 등록해야 한다
    assert runtime.observer.calls[0] == "unschedule_all"
    assert ("schedule", str(other), True) in runtime.observer.calls
