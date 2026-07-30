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

    def _read_mtime(self) -> int | None:
        """설정 파일의 mtime을 읽는다. 읽을 수 없으면 None.

        초 단위 float는 1초 해상도 파일시스템에서 같은 초에 저장한 두 번을
        구분하지 못한다. 오타를 고쳐 곧바로 다시 저장하는 흐름이 막히므로
        나노초 값을 쓴다.
        """
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None
