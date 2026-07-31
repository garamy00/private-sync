"""rsync over SSH 로 로컬 경로를 서버 저장소에 올린다."""

from __future__ import annotations

import logging
import shlex
import signal
import subprocess
import threading
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
# rsync --timeout 은 이만큼 아무 데이터도 오가지 않을 때만 발동한다. 큰 파일이라도
# 전송이 진행 중이면 걸리지 않으므로, 끊긴 연결에만 반응하는 상한이다.
_RSYNC_IO_TIMEOUT_SEC = 60
_STDERR_LOG_LIMIT = 200

# 시그널로 끊긴 자식의 반환 코드. 자식을 띄우지 않고 거절할 때도 같은 값을 쓴다.
_SIGNALLED_RETURNCODE = -signal.SIGTERM

Runner = Callable[[list[str], int], "subprocess.CompletedProcess[str]"]


class _ChildGuard:
    """실행 중인 자식 프로세스를 추적해 정지 요청 시 곧바로 끊는다.

    시그널 핸들러는 메인 스레드에서, 그것도 임의의 바이트코드 사이에서 끼어든다.
    run_command 가 락을 쥔 순간에 끼어들어도 멎지 않도록 재진입 가능한 RLock 을
    쓴다. 일반 Lock 이면 같은 스레드가 자기가 쥔 락을 기다리며 영영 멈춘다.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._stopped = False

    def start(self, args: list[str]) -> subprocess.Popen[str] | None:
        """자식을 띄워 등록하고 반환한다. 이미 정지 상태면 None 을 반환한다."""
        with self._lock:
            if self._stopped:
                return None

        # 락을 쥔 채로 띄우지 않는다. spawn 이 끝날 때까지 정지 요청이 밀린다.
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        with self._lock:
            self._process = process
            stopped = self._stopped

        # spawn 도중에 지나간 정지 요청은 이 자식을 보지 못했으므로 여기서 끊는다
        if stopped:
            process.terminate()

        return process

    def clear(self, process: subprocess.Popen[str]) -> None:
        """끝난 자식의 등록을 지운다."""
        with self._lock:
            if self._process is process:
                self._process = None

    def terminate(self) -> None:
        """정지 상태로 바꾸고 실행 중인 자식이 있으면 끊는다."""
        with self._lock:
            self._stopped = True
            process = self._process

        if process is not None:
            process.terminate()

    def reset(self) -> None:
        """정지 상태를 되돌린다."""
        with self._lock:
            self._stopped = False
            self._process = None


_child_guard = _ChildGuard()


def terminate_running_command() -> None:
    """실행 중인 외부 명령을 끊고, 이후 명령이 새로 뜨지 않게 한다.

    실행 중인 명령이 없어도 안전하게 호출할 수 있다. 시그널 핸들러에서 부르는
    것을 전제로 한다.
    """
    _child_guard.terminate()


def reset_command_guard() -> None:
    """정지 상태를 해제해 다시 명령을 실행할 수 있게 한다."""
    _child_guard.reset()


def run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """외부 명령을 인수 배열로 실행한다(셸 미사용).

    정지 요청을 받으면 실행 중이던 자식은 끊기고, 그 뒤에 시작하는 명령은 아예
    뜨지 않는다. 종료 중에 새 전송을 띄워봐야 Wi-Fi 가 이미 끊겨 종료만 더
    늦추기 때문이다. 어느 쪽이든 시그널로 끊긴 것과 같은 음수 반환 코드를
    돌려주므로 호출자가 재시도 대상으로 분류해 대기 항목을 보존한다.
    """
    process = _child_guard.start(args)
    if process is None:
        return subprocess.CompletedProcess(
            args=args, returncode=_SIGNALLED_RETURNCODE, stdout="", stderr=""
        )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            args, timeout, output=stdout, stderr=stderr
        ) from exc
    finally:
        _child_guard.clear(process)

    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


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

    rsync 자신에게도 대기 상한을 준다. `-e` 값은 rsync 가 직접 쪼개므로 인수
    하나로 넘겨야 한다.
    """
    args = ["rsync", "-az", "--partial", f"--timeout={_RSYNC_IO_TIMEOUT_SEC}"]
    args += ["-e", f"ssh -o BatchMode=yes -o ConnectTimeout={_SSH_TIMEOUT_SEC}"]
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
    except OSError as exc:
        # launchd 는 최소 PATH 만 넘기므로 ssh·rsync 를 못 찾을 수 있다
        raise UploadError(f"cannot run ssh: {exc.strerror}") from exc

    if mkdir.returncode < 0:
        raise RetryableUploadError(
            f"ssh mkdir was killed by a signal for label {label} "
            f"(exit {mkdir.returncode})"
        )
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
    except OSError as exc:
        # launchd 는 최소 PATH 만 넘기므로 ssh·rsync 를 못 찾을 수 있다
        raise UploadError(f"cannot run rsync: {exc.strerror}") from exc

    if result.returncode == 0:
        logger.info("Uploaded %s under label %s", path, label)
        return

    stderr = (result.stderr or "").strip()[:_STDERR_LOG_LIMIT]
    message = (
        f"rsync failed for {path} (label {label}, exit {result.returncode}): {stderr}"
    )
    # 음수는 시그널로 끊겼다는 뜻이다. 종료 중에 우리가 끊은 것이므로 폐기하지
    # 않고 대기 목록에 남겨 다음 실행에 다시 올린다.
    if result.returncode < 0 or result.returncode in RETRYABLE_EXIT_CODES:
        raise RetryableUploadError(message)
    raise UploadError(message)
