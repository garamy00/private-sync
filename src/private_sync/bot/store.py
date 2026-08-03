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


@dataclass(frozen=True)
class DirStats:
    """디렉토리 하나의 크기 요약."""

    files: int
    total_bytes: int


def _is_inside(base: Path, candidate: Path) -> bool:
    """이미 resolve된 후보 경로가 저장소 루트 안(또는 루트 자신)인지 판단한다.

    문자열 접두사 비교는 `store-evil` 같은 형제 디렉토리에 뚫리므로 `parents`
    멤버십으로 확인한다.
    """
    return candidate == base or base in candidate.parents


def _resolves_inside(base: Path, path: Path) -> bool:
    """항목이 저장소 안으로 풀리는지 확인한다. 판단할 수 없으면 False."""
    try:
        return _is_inside(base, path.resolve())
    except (OSError, RuntimeError):
        # 심볼릭 루프는 RuntimeError 를 던진다. 확신할 수 없으면 숨긴다.
        logger.warning("Skipping entry that cannot be resolved: %s", path.name)
        return False


def resolve_safe(root: Path, rel: str) -> Path:
    """상대경로를 저장소 루트 안의 실제 경로로 바꾼다.

    심볼릭 링크와 `..` 를 모두 펼친 뒤 루트 하위인지 확인해, 저장소 밖 파일이
    노출되는 것을 막는다.

    Raises:
        StoreError: 루트를 벗어나거나 대상이 존재하지 않을 때.
    """
    try:
        base = root.resolve()
        candidate = (base / rel).resolve()
    except (OSError, RuntimeError) as exc:
        raise StoreError(f"path {rel!r} cannot be resolved") from exc

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

    entries: list[Entry] = []
    for child in target.iterdir():
        # 저장소 밖을 가리키는 심볼릭 링크는 이름·크기조차 노출하지 않는다
        if not _resolves_inside(base, child):
            continue

        entry = _to_entry(child, rel)
        if entry is not None:
            entries.append(entry)

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
        if not _resolves_inside(base, path):
            continue

        # 한도를 넘는 '실제 일치'를 만났을 때만 절단으로 기록한다
        if len(results) >= limit:
            truncated = True
            break

        try:
            size = path.stat().st_size
        except OSError:
            # 깨진 링크 하나 때문에 검색 전체가 실패하면 안 된다
            logger.warning("Skipping unreadable search hit %s", path.name)
            continue

        rel = str(PurePosixPath(path.relative_to(base)))
        results.append(Entry(name=path.name, rel=rel, is_dir=False, size=size))

    if truncated:
        logger.info("Search for %r truncated at %d results", keyword, limit)
    return results


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


def parent_rel(rel: str) -> str | None:
    """상위 디렉토리의 상대경로를 반환한다. 루트면 None."""
    if not rel:
        return None
    parent = PurePosixPath(rel).parent
    return "" if str(parent) == "." else str(parent)


def _to_entry(path: Path, parent: str) -> Entry | None:
    """경로를 Entry로 변환한다. 상태를 읽을 수 없으면 None."""
    rel = str(PurePosixPath(parent) / path.name) if parent else path.name
    try:
        is_dir = path.is_dir()
        size = 0 if is_dir else path.stat().st_size
    except OSError:
        # 깨진 링크 하나 때문에 옆의 멀쩡한 파일까지 못 보게 되면 안 된다
        logger.warning("Skipping unreadable entry %s", rel)
        return None

    return Entry(name=path.name, rel=rel, is_dir=is_dir, size=size)
