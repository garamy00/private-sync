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


def resolve_safe(root: Path, rel: str) -> Path:
    """상대경로를 저장소 루트 안의 실제 경로로 바꾼다.

    심볼릭 링크와 `..` 를 모두 펼친 뒤 루트 하위인지 확인해, 저장소 밖 파일이
    노출되는 것을 막는다.

    Raises:
        StoreError: 루트를 벗어나거나 대상이 존재하지 않을 때.
    """
    base = root.resolve()
    candidate = (base / rel).resolve()

    if candidate != base and base not in candidate.parents:
        raise StoreError(f"path {rel!r} resolves outside the store")
    if not candidate.exists():
        raise StoreError(f"path {rel!r} not found in store")
    return candidate


def list_dir(root: Path, rel: str) -> list[Entry]:
    """디렉토리 내용을 디렉토리 먼저, 이름순으로 나열한다.

    Raises:
        StoreError: 경로가 루트를 벗어나거나 디렉토리가 아닐 때.
    """
    target = resolve_safe(root, rel)
    if not target.is_dir():
        raise StoreError(f"path {rel!r} is not a directory")

    entries = [_to_entry(child, rel) for child in target.iterdir()]
    return sorted(entries, key=lambda e: (not e.is_dir, e.name))


def search(root: Path, keyword: str, limit: int = 50) -> list[Entry]:
    """파일명에 키워드가 포함된 파일을 저장소 전체에서 찾는다."""
    needle = keyword.strip().lower()
    if not needle:
        return []

    base = root.resolve()
    results: list[Entry] = []
    for path in sorted(base.rglob("*")):
        if len(results) >= limit:
            logger.info("Search for %r truncated at %d results", keyword, limit)
            break
        if not path.is_file() or needle not in path.name.lower():
            continue
        rel = str(PurePosixPath(path.relative_to(base)))
        results.append(
            Entry(name=path.name, rel=rel, is_dir=False, size=path.stat().st_size)
        )
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
