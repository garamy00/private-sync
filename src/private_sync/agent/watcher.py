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


def match_target(event_path: Path, targets: list[WatchTarget]) -> WatchTarget | None:
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
