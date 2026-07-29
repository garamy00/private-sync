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
