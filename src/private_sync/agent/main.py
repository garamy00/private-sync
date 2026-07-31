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
from watchdog.observers.api import BaseObserver

from private_sync.agent.config_watch import ConfigWatcher
from private_sync.agent.pending import PendingItem, PendingStore
from private_sync.agent.uploader import (
    reset_command_guard,
    terminate_running_command,
    upload,
)
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
                # 재시도해도 실패할 오류는 격리해 무한 루프를 막는다. 다만
                # 설정·환경 문제면 모든 항목이 조용히 버려지므로 눈에 띄게 남긴다.
                logger.critical(
                    "Dropping %s permanently, sync will not retry it: %s",
                    item.path,
                    exc,
                )
                self._pending.discard(item)
                continue

            self._pending.discard(item)
            self.backoff.reset()

    def has_pending(self) -> bool:
        """대기 항목이 남아 있는지 반환한다."""
        return bool(self._pending.items())

    def replace_config(self, config: AgentConfig) -> None:
        """설정을 교체한다. 대기 목록과 backoff 상태는 그대로 둔다.

        사라진 라벨의 대기 항목은 여기서 손대지 않는다. drain이 이미 알 수 없는
        라벨을 폐기하므로 중복해서 처리할 이유가 없다.
        """
        self._config = config
        self._excludes = {s.label: s.exclude for s in config.sources}


@dataclass
class LoopState:
    """감시 스레드와 메인 루프가 공유하는 상태."""

    debouncer: Debouncer
    lock: threading.Lock
    stop: threading.Event


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
    config: AgentConfig


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

    def replace_targets(self, targets: list[WatchTarget]) -> None:
        """감시 대상 목록을 교체한다.

        observer 스레드가 `on_any_event`에서 이 목록을 읽는다. 완성된 새 리스트로
        속성을 통째로 갈아끼우기만 하므로, 읽는 쪽은 항상 옛 목록이나 새 목록 중
        하나를 온전히 보게 된다.
        """
        self._targets = targets


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="private-sync 노트북 에이전트")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=_DEFAULT_STATE)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def queue_initial_sync(worker: SyncWorker, targets: list[WatchTarget]) -> None:
    """시작 시 모든 감시 대상을 한 번 큐에 넣는다.

    watchdog은 데몬이 뜬 뒤의 변경만 알려준다. 아직 한 번도 올리지 않은 파일과
    데몬이 꺼져 있는 동안 바뀐 파일은 이것이 없으면 영영 올라가지 않는다.
    실제 차이 비교와 전송량 최소화는 rsync가 한다.
    """
    for target in targets:
        worker.enqueue(target.label, target.path)

    logger.info("Queued %d target(s) for initial sync", len(targets))


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
    자동으로 재시도된다. 다만 `_schedule_targets` 가 실패하면 앞의 교체는 이미
    적용된 채 예외가 올라간다. 호출자가 데몬을 멈추므로 그 상태로 계속 돌지는
    않는다.
    """
    try:
        config = load_agent_config(runtime.config_path)
    except ConfigError as exc:
        logger.error("Keeping previous settings, config reload failed: %s", exc)
        return False

    targets = build_targets(config.sources)
    known = {(t.label, str(t.path)) for t in runtime.targets}
    if config.remote == runtime.config.remote:
        added = [t for t in targets if (t.label, str(t.path)) not in known]
    else:
        # 서버가 바뀌면 새 저장소는 비어 있다. 전부 다시 올려야 한다.
        logger.info("Remote changed, re-queueing every target")
        added = list(targets)

    runtime.worker.replace_config(config)
    runtime.handler.replace_targets(targets)
    _schedule_targets(runtime.observer, runtime.handler, targets)
    _log_label_changes(runtime.targets, targets)
    runtime.targets = targets
    runtime.config = config

    # 새로 추가된 대상만 훑는다. 추가하고도 건드리기 전까지 안 올라가면
    # 초기 동기화로 막아둔 문제가 되살아난다.
    for target in added:
        runtime.worker.enqueue(target.label, target.path)

    logger.info(
        "Config reloaded: %d target(s), %d newly queued", len(targets), len(added)
    )
    return True


def _run_loop(runtime: AgentRuntime, state: LoopState) -> bool:
    """설정 변경을 반영하고, 디바운스가 끝난 항목을 큐에 넣어 업로드한다.

    Returns:
        정상 종료면 True, 감시를 잃어 스스로 멈췄으면 False.
    """
    while not state.stop.is_set():
        if runtime.watcher.changed():
            try:
                _reload_config(runtime)
            except OSError as exc:
                # 기존 watch를 이미 해제한 뒤라 감시가 하나도 없는 채로 남는다.
                # 정상인 척하며 아무것도 동기화하지 않느니 시끄럽게 멈춘다.
                logger.critical(
                    "Cannot re-register watches after reload, stopping: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                state.stop.set()
                return False

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

    return True


def main(argv: list[str] | None = None) -> int:
    """에이전트를 실행한다."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # 가드는 모듈 전역이라 이전 실행의 정지 상태가 그대로 남는다. 풀지 않으면
    # 같은 프로세스에서 다시 시작했을 때 모든 명령이 조용히 거절된다.
    reset_command_guard()

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
    _schedule_targets(observer, handler, targets)

    runtime = AgentRuntime(
        worker=worker,
        handler=handler,
        observer=observer,
        config_path=args.config,
        targets=targets,
        watcher=ConfigWatcher(args.config),
        config=config,
    )

    def request_stop(*_: object) -> None:
        """정지 플래그를 세우고 실행 중인 자식 명령을 끊는다.

        플래그만 세우면 업로드가 끝날 때까지 루프가 그것을 보지 못한다. 하필
        종료 시퀀스 초반에 Wi-Fi 가 끊기므로, 전송 중인 rsync 는 자기 상한까지
        매달려 있고 그만큼 로그아웃이 밀린다.
        """
        state.stop.set()
        terminate_running_command()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    observer.start()
    logger.info("Agent started with %d watch targets", len(targets))
    queue_initial_sync(worker, targets)
    try:
        healthy = _run_loop(runtime, state)
    finally:
        observer.stop()
        observer.join(timeout=5)
        logger.info("Agent stopped")

    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
