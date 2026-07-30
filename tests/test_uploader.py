import subprocess
from pathlib import Path

import pytest

from private_sync.agent.uploader import (
    build_mkdir_args,
    build_rsync_args,
    upload,
)
from private_sync.config import RemoteConfig
from private_sync.errors import RetryableUploadError, UploadError

REMOTE = RemoteConfig(host="dgson@ai", store="private-sync/store")


def _ok(_args, _timeout):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _failing(code, stderr=""):
    def runner(_args, _timeout):
        return subprocess.CompletedProcess(
            args=[], returncode=code, stdout="", stderr=stderr
        )

    return runner


def test_rsync_args_keep_directory_name_and_apply_excludes():
    args = build_rsync_args(
        REMOTE, "SKT 문서", Path("/Users/me/Documents/skt"), (".DS_Store", ".git/")
    )

    assert args[0] == "rsync"
    # macOS 기본 rsync(openrsync)는 -s 를 모른다. 붙이면 노트북에서 전부 실패한다.
    assert "-s" not in args
    assert "--exclude=.DS_Store" in args
    assert "--exclude=.git/" in args
    # 트레일링 슬래시가 없어야 원격에 skt/ 디렉토리째로 생성된다
    assert args[-2] == "/Users/me/Documents/skt"
    # 공백이 든 원격 경로는 원격 셸이 쪼개지 않도록 따옴표로 감싼다
    assert args[-1] == "dgson@ai:'private-sync/store/SKT 문서/'"


def test_rsync_args_for_single_file():
    args = build_rsync_args(REMOTE, "메모", Path("/Users/me/work/견적서.xlsx"), ())

    assert args[-2] == "/Users/me/work/견적서.xlsx"
    # 한글은 shlex 기준 안전 문자가 아니라 따옴표가 붙는다
    assert args[-1] == "dgson@ai:'private-sync/store/메모/'"


def test_rsync_args_leave_plain_ascii_path_unquoted():
    args = build_rsync_args(REMOTE, "docs", Path("/Users/me/docs"), ())

    assert args[-1] == "dgson@ai:private-sync/store/docs/"


def test_mkdir_args_quote_label_with_spaces():
    args = build_mkdir_args(REMOTE, "SKT 문서")

    assert args[0] == "ssh"
    assert args[-2] == "dgson@ai"
    assert args[-1] == "mkdir -p 'private-sync/store/SKT 문서'"


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
