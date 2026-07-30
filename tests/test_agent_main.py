import threading
import time
from pathlib import Path

from private_sync.agent.main import (
    _DEBOUNCE_SEC,
    Backoff,
    LoopState,
    SyncWorker,
    _EventHandler,
    queue_initial_sync,
)
from private_sync.agent.pending import PendingItem, PendingStore
from private_sync.agent.watcher import Debouncer, build_targets
from private_sync.config import AgentConfig, RemoteConfig, Source
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


def test_replace_config_drops_items_of_removed_labels(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("a", encoding="utf-8")
    pending = PendingStore(tmp_path / "pending.json")
    pending.load()
    worker = SyncWorker(_config(doc, label="이전"), pending, uploader=lambda *_a: None)
    worker.enqueue("이전", doc)

    worker.replace_config(_config(doc, label="이후"))
    worker.drain()

    # 설정에서 사라진 라벨의 대기 항목은 폐기된다
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
