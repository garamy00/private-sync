"""rsync over SSH 로 로컬 경로를 서버 저장소에 올린다."""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from private_sync.config import RemoteConfig
from private_sync.errors import RetryableUploadError, UploadError

logger = logging.getLogger(__name__)

# 연결·전송 계층 실패. 사내망 밖이거나 일시 장애이므로 재시도한다.
#   10 socket I/O, 12 protocol, 30 timeout, 35 timeout waiting for daemon,
#   255 ssh 연결 실패
RETRYABLE_EXIT_CODES = frozenset({10, 12, 30, 35, 255})

_SSH_TIMEOUT_SEC = 15
_RSYNC_TIMEOUT_SEC = 600
_STDERR_LOG_LIMIT = 200

Runner = Callable[[list[str], int], "subprocess.CompletedProcess[str]"]


def run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """외부 명령을 인수 배열로 실행한다(셸 미사용)."""
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def remote_dir(remote: RemoteConfig, label: str) -> str:
    """라벨에 해당하는 원격 디렉토리 경로를 만든다(원격 홈 기준)."""
    return f"{remote.store}/{label}"


def build_rsync_args(
    remote: RemoteConfig, label: str, path: Path, exclude: tuple[str, ...]
) -> list[str]:
    """rsync 명령 인수를 조립한다.

    원격 경로는 원격 셸이 해석하므로 shlex.quote로 감싼다. rsync 3의
    `-s`(--protect-args)를 쓰지 않는 이유는 macOS 기본 rsync(openrsync)가
    그 옵션을 모르기 때문이다. 붙이면 노트북에서 모든 업로드가 실패한다.

    로컬 경로에 트레일링 슬래시를 붙이지 않아 디렉토리는 자기 이름째로
    원격에 생성된다.
    """
    args = ["rsync", "-az", "--partial"]
    args += [f"--exclude={pattern}" for pattern in exclude]
    args.append(str(path))
    args.append(f"{remote.host}:{shlex.quote(remote_dir(remote, label) + '/')}")
    return args


def build_mkdir_args(remote: RemoteConfig, label: str) -> list[str]:
    """원격 디렉토리를 미리 만드는 ssh 명령 인수를 조립한다.

    원격 셸이 문자열을 해석하므로 경로는 shlex.quote 로 감싼다.
    """
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={_SSH_TIMEOUT_SEC}",
        remote.host,
        f"mkdir -p {shlex.quote(remote_dir(remote, label))}",
    ]


def upload(
    remote: RemoteConfig,
    label: str,
    path: Path,
    exclude: tuple[str, ...],
    runner: Runner = run_command,
) -> None:
    """경로 하나를 서버 저장소에 올린다.

    Raises:
        RetryableUploadError: 연결 실패·타임아웃 등 재시도 가치가 있는 오류.
        UploadError: 권한·디스크 등 재시도해도 소용없는 오류.
    """
    # 원격 라벨 디렉토리가 없으면 rsync가 실패하므로 먼저 만든다
    try:
        mkdir = runner(build_mkdir_args(remote, label), _SSH_TIMEOUT_SEC + 5)
    except subprocess.TimeoutExpired as exc:
        raise RetryableUploadError(f"ssh mkdir timed out for label {label}") from exc

    if mkdir.returncode == 255:
        raise RetryableUploadError(
            f"ssh connection failed for label {label} (exit 255)"
        )
    if mkdir.returncode != 0:
        stderr = (mkdir.stderr or "").strip()[:_STDERR_LOG_LIMIT]
        raise UploadError(
            f"ssh mkdir failed for label {label} (exit {mkdir.returncode}): {stderr}"
        )

    try:
        result = runner(
            build_rsync_args(remote, label, path, exclude), _RSYNC_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired as exc:
        raise RetryableUploadError(f"rsync timed out for {path}") from exc

    if result.returncode == 0:
        logger.info("Uploaded %s under label %s", path, label)
        return

    stderr = (result.stderr or "").strip()[:_STDERR_LOG_LIMIT]
    message = (
        f"rsync failed for {path} (label {label}, exit {result.returncode}): {stderr}"
    )
    if result.returncode in RETRYABLE_EXIT_CODES:
        raise RetryableUploadError(message)
    raise UploadError(message)
