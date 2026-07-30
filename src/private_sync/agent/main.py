"""노트북 측 동기화 데몬 진입점."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from private_sync.agent.pending import PendingItem, PendingStore
from private_sync.agent.uploader import upload
from private_sync.agent.watcher import (
    Debouncer,
    WatchTarget,
    build_targets,
    match_target,
)
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
