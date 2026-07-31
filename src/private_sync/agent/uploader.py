"""rsync over SSH 로 로컬 경로를 서버 저장소에 올린다."""

from __future__ import annotations

import logging
import os
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

# 우리가 스스로 보내는 종료 시그널의 반환 코드. 이 값으로 끊긴 자식만 재시도
# 대상이다. SIGSEGV·SIGKILL·SIGBUS 처럼 자식이 스스로 깨진 경우는 다시 시도해도
# 같은 결과이고, drain 이 첫 재시도 실패에서 멈추므로 대기 목록 전체가 막힌다.
_SELF_SENT_SIGNAL_RETURNCODES = frozenset(
    {-signal.SIGTERM, -signal.SIGINT, -signal.SIGHUP}
)

# 자식을 띄우지 않고 거절했다는 표식. 실제로 실행된 적이 없으므로 "시그널에
# 끊겼다"고 기록하면 로그가 사실과 달라진다.
_SHUTDOWN_REFUSAL_STDERR = "not started: shutdown in progress"


def _signal_child_group(process: subprocess.Popen[str], sig: int) -> None:
    """자식이 속한 프로세스 그룹 전체에 시그널을 보낸다.

    rsync 는 ssh 를 포크하고 그 손자가 부모의 파이프를 물려받는다. 직접 자식만
    끊으면 communicate 가 EOF 를 받지 못해 상한까지 매달리므로 그룹째 끊는다.
    시그널 핸들러에서도 불리니 어떤 예외도 밖으로 내보내지 않는다.
    """
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        # 자식이 이미 사라졌거나(ProcessLookupError) 권한 밖이면
        # (PermissionError) 그룹을 알 수 없다. 직접 자식만이라도 끊어 본다.
        _signal_child(process, sig)
        return

    try:
        os.killpg(pgid, sig)
    except OSError:
        # 그룹이 이미 사라졌거나 권한 밖이다. 직접 자식만 다시 시도한다.
        _signal_child(process, sig)


def _signal_child(process: subprocess.Popen[str], sig: int) -> None:
    """직접 자식에게만 시그널을 보낸다(그룹 경로가 실패했을 때의 대비책)."""
    try:
        process.send_signal(sig)
    except OSError:
        # 이미 끝난 자식이다. 정지 요청이 실패로 번지게 두지 않는다.
        return


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
        # start_new_session 으로 자체 프로세스 그룹을 준다. 그래야 자식이 포크한
        # 손자까지 한 번에 끊을 수 있고, 우리 그룹에 시그널이 되돌아오지 않는다.
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        with self._lock:
            self._process = process
            stopped = self._stopped

        # spawn 도중에 지나간 정지 요청은 이 자식을 보지 못했으므로 여기서 끊는다
        if stopped:
            _signal_child_group(process, signal.SIGTERM)

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
            _signal_child_group(process, signal.SIGTERM)

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
            args=args,
            returncode=_SIGNALLED_RETURNCODE,
            stdout="",
            stderr=_SHUTDOWN_REFUSAL_STDERR,
        )

    # subprocess.run 과 같이 with 로 감싼다. 예외가 나든 아니든 파이프를 닫고
    # 자식을 거두는 일을 빠뜨리지 않기 위함이다.
    with process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # 손자가 파이프를 쥐고 있으면 두 번째 communicate 도 막힌다. 그룹째
            # 죽여야 EOF 가 온다.
            _signal_child_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                args, timeout, output=stdout, stderr=stderr
            ) from exc
        except BaseException:
            # 넓게 잡는 것이 맞는 자리다. subprocess.run 도 똑같이 한다. 좁은
            # 예외만 잡으면 KeyboardInterrupt·SystemExit 로 빠져나갈 때 자식이
            # 살아남고, 등록은 아래 finally 에서 지워지므로 아무도 손댈 수 없는
            # 고아가 된다. 곧바로 다시 던지므로 예외를 삼키지 않는다.
            _signal_child_group(process, signal.SIGKILL)
            raise
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


def _interrupted_message(
    step: str, label: str, result: subprocess.CompletedProcess[str]
) -> str:
    """우리가 끊었거나 아예 띄우지 않은 명령의 사유를 문장으로 만든다."""
    if result.stderr == _SHUTDOWN_REFUSAL_STDERR:
        return f"{step} was not started for label {label}, shutdown in progress"

    return (
        f"{step} was killed by our own signal for label {label} "
        f"(exit {result.returncode})"
    )


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

    # 우리가 끊은 것만 되살린다. 다른 음수는 자식이 스스로 깨진 것이므로 아래
    # 영구 실패 경로로 내려가 폐기된다.
    if mkdir.returncode in _SELF_SENT_SIGNAL_RETURNCODES:
        raise RetryableUploadError(_interrupted_message("ssh mkdir", label, mkdir))
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

    # 종료 중에 우리가 끊은 전송은 폐기하지 않고 대기 목록에 남겨 다음 실행에
    # 다시 올린다. SIGSEGV 처럼 rsync 가 스스로 깨진 경우는 여기 해당하지 않는다.
    if result.returncode in _SELF_SENT_SIGNAL_RETURNCODES:
        raise RetryableUploadError(
            _interrupted_message(f"rsync for {path}", label, result)
        )

    stderr = (result.stderr or "").strip()[:_STDERR_LOG_LIMIT]
    message = (
        f"rsync failed for {path} (label {label}, exit {result.returncode}): {stderr}"
    )
    if result.returncode in RETRYABLE_EXIT_CODES:
        raise RetryableUploadError(message)
    raise UploadError(message)
