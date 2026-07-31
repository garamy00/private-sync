import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from private_sync.agent.uploader import (
    build_mkdir_args,
    build_rsync_args,
    reset_command_guard,
    run_command,
    terminate_running_command,
    upload,
)
from private_sync.config import RemoteConfig
from private_sync.errors import RetryableUploadError, UploadError

REMOTE = RemoteConfig(host="user@sync-server", store="private-sync/store")

# 자식은 넉넉히 자게 두고, 테스트는 그보다 훨씬 짧은 상한에서 판정한다.
# 회귀 시 통째로 멈추지 않고 실패로 끝나게 하려는 것이다.
_SLOW_CHILD = [sys.executable, "-c", "import time; time.sleep(30)"]
_CHILD_TIMEOUT_SEC = 25
_CUT_DEADLINE_SEC = 5.0


@pytest.fixture(autouse=True)
def _fresh_command_guard():
    """가드의 정지 상태가 다른 테스트로 새지 않게 한다."""
    reset_command_guard()
    yield
    reset_command_guard()


def _ok(_args, _timeout):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _killed(args):
    # 시그널로 끊긴 자식은 음수 반환 코드를 남긴다(SIGTERM 이면 -15)
    return subprocess.CompletedProcess(args=args, returncode=-15, stdout="", stderr="")


def _failing(code, stderr=""):
    def runner(_args, _timeout):
        return subprocess.CompletedProcess(
            args=[], returncode=code, stdout="", stderr=stderr
        )

    return runner


def test_rsync_args_keep_directory_name_and_apply_excludes():
    args = build_rsync_args(
        REMOTE, "업무 문서", Path("/Users/me/Documents/work"), (".DS_Store", ".git/")
    )

    assert args[0] == "rsync"
    # macOS 기본 rsync(openrsync)는 -s 를 모른다. 붙이면 노트북에서 전부 실패한다.
    assert "-s" not in args
    assert "--exclude=.DS_Store" in args
    assert "--exclude=.git/" in args
    # 트레일링 슬래시가 없어야 원격에 work/ 디렉토리째로 생성된다
    assert args[-2] == "/Users/me/Documents/work"
    # 공백이 든 원격 경로는 원격 셸이 쪼개지 않도록 따옴표로 감싼다
    assert args[-1] == "user@sync-server:'private-sync/store/업무 문서/'"


def test_rsync_args_for_single_file():
    args = build_rsync_args(REMOTE, "메모", Path("/Users/me/work/견적서.xlsx"), ())

    assert args[-2] == "/Users/me/work/견적서.xlsx"
    # 한글은 shlex 기준 안전 문자가 아니라 따옴표가 붙는다
    assert args[-1] == "user@sync-server:'private-sync/store/메모/'"


def test_rsync_args_leave_plain_ascii_path_unquoted():
    args = build_rsync_args(REMOTE, "docs", Path("/Users/me/docs"), ())

    assert args[-1] == "user@sync-server:private-sync/store/docs/"


def test_rsync_args_carry_connect_and_stall_timeouts():
    """rsync 자신도 대기 상한을 갖는지 확인한다.

    Wi-Fi 가 끊긴 채 시작한 전송은 파이썬 쪽 상한(600초)까지 매달려 있어서
    종료를 지연시킨다. `-e` 값은 rsync 가 직접 쪼개므로 한 인수여야 한다.
    """
    args = build_rsync_args(REMOTE, "docs", Path("/Users/me/docs"), ())

    assert "--timeout=60" in args
    dash_e = args.index("-e")
    assert args[dash_e + 1] == "ssh -o BatchMode=yes -o ConnectTimeout=15"


def test_signal_killed_rsync_is_retryable():
    """시그널로 끊긴 rsync 는 폐기되지 않고 다음 실행에 다시 올라가야 한다."""

    def runner(args, timeout):
        if args[0] == "ssh":
            return _ok(args, timeout)
        return _killed(args)

    with pytest.raises(RetryableUploadError):
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)


def test_signal_killed_ssh_mkdir_is_retryable():
    """mkdir 단계가 시그널로 끊겨도 항목은 대기 목록에 남아야 한다."""

    def runner(args, _timeout):
        return _killed(args)

    with pytest.raises(RetryableUploadError):
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)


def test_mkdir_args_quote_label_with_spaces():
    args = build_mkdir_args(REMOTE, "업무 문서")

    assert args[0] == "ssh"
    assert args[-2] == "user@sync-server"
    assert args[-1] == "mkdir -p 'private-sync/store/업무 문서'"


def test_upload_runs_mkdir_then_rsync():
    calls = []

    def runner(args, timeout):
        calls.append(args[0])
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)

    assert calls == ["ssh", "rsync"]


def test_ssh_connection_failure_is_retryable():
    with pytest.raises(RetryableUploadError):
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=_failing(255))


def test_rsync_permission_error_is_not_retryable():
    def runner(args, timeout):
        if args[0] == "ssh":
            return _ok(args, timeout)
        return subprocess.CompletedProcess(
            args=args, returncode=23, stdout="", stderr="Permission denied"
        )

    with pytest.raises(UploadError) as excinfo:
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)

    assert not isinstance(excinfo.value, RetryableUploadError)


def test_rsync_socket_error_is_retryable():
    def runner(args, timeout):
        if args[0] == "ssh":
            return _ok(args, timeout)
        return subprocess.CompletedProcess(
            args=args, returncode=30, stdout="", stderr="timeout"
        )

    with pytest.raises(RetryableUploadError):
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)


def test_timeout_is_retryable():
    def runner(_args, _timeout):
        raise subprocess.TimeoutExpired(cmd="rsync", timeout=1)

    with pytest.raises(RetryableUploadError):
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)


def test_missing_binary_is_reported_as_permanent_upload_error():
    def runner(args, _timeout):
        # launchd 가 최소 PATH 만 넘겨 ssh·rsync 를 못 찾을 때 나오는 예외다
        raise FileNotFoundError(2, "No such file or directory", args[0])

    with pytest.raises(UploadError) as excinfo:
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)

    # 없는 바이너리는 기다린다고 생기지 않으므로 재시도 대상이 아니다
    assert not isinstance(excinfo.value, RetryableUploadError)
    assert "cannot run ssh" in str(excinfo.value)


def test_missing_rsync_binary_is_reported_as_permanent_upload_error():
    def runner(args, timeout):
        if args[0] == "ssh":
            return _ok(args, timeout)
        raise FileNotFoundError(2, "No such file or directory", "rsync")

    with pytest.raises(UploadError) as excinfo:
        upload(REMOTE, "메모", Path("/Users/me/a.md"), (), runner=runner)

    assert not isinstance(excinfo.value, RetryableUploadError)
    assert "cannot run rsync" in str(excinfo.value)


def test_terminate_cuts_a_running_child_short():
    """실제로 오래 도는 자식이 종료 요청 직후 끊기는지 확인한다.

    데몬은 업로드 중에도 즉시 멈출 수 있어야 한다. 네트워크 없이 로컬 자식으로
    같은 상황을 만든다.
    """
    started = threading.Event()
    outcome: dict[str, object] = {}

    def call_run_command() -> None:
        started.set()
        begin = time.monotonic()
        outcome["result"] = run_command(_SLOW_CHILD, _CHILD_TIMEOUT_SEC)
        outcome["elapsed"] = time.monotonic() - begin

    caller = threading.Thread(target=call_run_command, daemon=True)
    caller.start()
    assert started.wait(_CUT_DEADLINE_SEC)
    # 자식이 실제로 떠서 돌고 있는 상태를 끊는 경로를 확인한다
    time.sleep(0.5)

    terminate_running_command()
    caller.join(_CUT_DEADLINE_SEC)

    assert not caller.is_alive(), "run_command did not return after terminate"
    assert outcome["elapsed"] < _CUT_DEADLINE_SEC
    # 시그널로 끊긴 자식은 음수 반환 코드를 남긴다
    assert outcome["result"].returncode < 0


def test_command_started_after_terminate_does_not_run():
    """정지 이후 시작된 명령은 자식을 띄우지 않고 즉시 돌아온다."""
    # 실행 중인 명령이 없어도 호출은 안전해야 한다
    terminate_running_command()

    begin = time.monotonic()
    result = run_command(_SLOW_CHILD, _CHILD_TIMEOUT_SEC)

    assert time.monotonic() - begin < _CUT_DEADLINE_SEC
    assert result.returncode < 0


def test_built_rsync_args_actually_work_with_the_real_rsync_binary(tmp_path):
    """조립한 인수를 실제 rsync 로 실행해 파일이 도착하는지 확인한다.

    옵션 오타·미지원 플래그는 단위 테스트로는 잡히지 않고 운영에서 전부 실패로만
    나타난다. 네트워크·SSH 없이 원격 목적지만 로컬 디렉토리로 바꿔 검증한다.
    """
    if shutil.which("rsync") is None:
        pytest.skip("rsync binary not available")

    source = tmp_path / "src"
    source.mkdir()
    (source / "계약서.docx").write_text("real content", encoding="utf-8")
    (source / "skip.tmp").write_text("ignore me", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()

    args = build_rsync_args(REMOTE, "업무 문서", source, ("*.tmp",))
    # 원격 목적지만 로컬 경로로 교체한다. 나머지 인수는 운영과 동일하다.
    args[-1] = f"{dest}/"

    result = run_command(args, timeout=60)

    assert result.returncode == 0, result.stderr
    arrived = dest / source.name / "계약서.docx"
    assert arrived.read_text(encoding="utf-8") == "real content"
    # 트레일링 슬래시가 없으므로 디렉토리가 자기 이름째로 생성된다
    assert not (dest / source.name / "skip.tmp").exists()
