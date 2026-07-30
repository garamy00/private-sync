from pathlib import Path

from private_sync.agent.main import Backoff, SyncWorker
from private_sync.agent.pending import PendingItem, PendingStore
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
