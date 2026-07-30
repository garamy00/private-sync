from pathlib import Path

import pytest
import pyzipper

from private_sync.bot.packer import (
    MAX_PART_BYTES,
    make_encrypted_zip,
    pack_for_send,
    split_file,
)
from private_sync.errors import PackError


def test_part_size_stays_under_telegram_limit():
    # 텔레그램 봇 sendDocument 한도는 50MB다
    assert MAX_PART_BYTES < 50 * 1024 * 1024


def test_encrypted_zip_opens_with_password_and_matches_source(tmp_path):
    src = tmp_path / "계약서.docx"
    src.write_bytes(b"secret payload")
    dest = tmp_path / "out"
    dest.mkdir()

    archive = make_encrypted_zip(src, dest, password="pw1234")

    assert archive.name == "계약서.docx.zip"
    with pyzipper.AESZipFile(archive) as zf:
        zf.setpassword(b"pw1234")
        assert zf.namelist() == ["계약서.docx"]
        assert zf.read("계약서.docx") == b"secret payload"


def test_encrypted_zip_rejects_wrong_password(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"data")
    dest = tmp_path / "out"
    dest.mkdir()
    archive = make_encrypted_zip(src, dest, password="right")

    with pyzipper.AESZipFile(archive) as zf:
        zf.setpassword(b"wrong")
        with pytest.raises(RuntimeError):
            zf.read("a.txt")


def test_split_and_rejoin_reproduces_original_bytes(tmp_path):
    payload = bytes(range(256)) * 20  # 5120 바이트
    target = tmp_path / "big.zip"
    target.write_bytes(payload)

    parts = split_file(target, max_bytes=1024)

    assert [p.name for p in parts] == [
        "big.zip.part01",
        "big.zip.part02",
        "big.zip.part03",
        "big.zip.part04",
        "big.zip.part05",
    ]
    rejoined = b"".join(p.read_bytes() for p in parts)
    assert rejoined == payload


def test_pack_for_send_returns_single_file_when_small(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"tiny")
    dest = tmp_path / "out"
    dest.mkdir()

    parts = pack_for_send(src, dest, password="pw", max_bytes=1024 * 1024)

    assert len(parts) == 1
    assert parts[0].suffix == ".zip"


def test_pack_for_send_splits_when_over_limit(tmp_path):
    src = tmp_path / "a.bin"
    # 압축되지 않는 데이터를 만들어 ZIP이 확실히 한도를 넘게 한다
    src.write_bytes(bytes(range(256)) * 40)
    dest = tmp_path / "out"
    dest.mkdir()

    parts = pack_for_send(src, dest, password="pw", max_bytes=512)

    assert len(parts) > 1
    assert all(p.stat().st_size <= 512 for p in parts)
    assert all(".part" in p.name for p in parts)


def test_pack_for_send_rejects_missing_source(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(PackError, match="cannot read"):
        pack_for_send(tmp_path / "없음.txt", dest, password="pw")


def test_split_file_wraps_unlink_failure_in_pack_error(tmp_path, monkeypatch):
    target = tmp_path / "big.zip"
    target.write_bytes(b"x" * 100)

    def failing_unlink(self, missing_ok=False):
        raise PermissionError(13, "Permission denied")

    # 파트는 정상적으로 쓰이고 원본 삭제만 실패하는 상황을 만든다.
    # 디렉토리 권한으로는 파트 쓰기가 먼저 막혀 이 분기에 도달하지 못한다.
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    # 삭제 실패가 raw OSError로 새면 봇 프로세스가 죽는다
    with pytest.raises(PackError, match="cannot split"):
        split_file(target, max_bytes=50)
