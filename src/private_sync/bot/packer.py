"""전송용 AES-256 암호 ZIP을 만들고 필요하면 분할한다."""

from __future__ import annotations

import logging
from pathlib import Path

import pyzipper

from private_sync.bot import store
from private_sync.errors import PackError

logger = logging.getLogger(__name__)

# 텔레그램 봇 sendDocument 한도가 50MB이므로 여유를 두고 45MB로 자른다
MAX_PART_BYTES = 45 * 1024 * 1024


def make_encrypted_zip(src: Path, dest_dir: Path, password: str) -> Path:
    """원본 파일 하나를 AES-256 암호 ZIP으로 포장한다.

    Args:
        src: 포장할 원본 파일.
        dest_dir: ZIP을 만들 디렉토리.
        password: ZIP 암호.

    Returns:
        생성된 ZIP 경로.

    Raises:
        PackError: 원본을 읽을 수 없거나 ZIP 생성에 실패했을 때.
    """
    archive = dest_dir / (src.name + ".zip")
    try:
        with pyzipper.AESZipFile(
            archive,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            zf.write(src, arcname=src.name)
    except OSError as exc:
        # 중간까지 쓰인 아카이브를 남기지 않는다
        archive.unlink(missing_ok=True)
        raise PackError(
            f"cannot read or write while packing {src.name}: {exc.strerror}"
        ) from exc

    logger.info("Packed %s into %s bytes", src.name, archive.stat().st_size)
    return archive


def split_file(path: Path, max_bytes: int = MAX_PART_BYTES) -> list[Path]:
    """파일을 max_bytes 단위로 잘라 .partNN 파일들을 만든다.

    원본은 남겨두지 않고 삭제한다. 반환 순서대로 이어붙이면 원본이 된다.

    Raises:
        PackError: 읽기·쓰기에 실패했을 때.
    """
    parts: list[Path] = []
    try:
        with path.open("rb") as source:
            index = 1
            while True:
                chunk = source.read(max_bytes)
                if not chunk:
                    break
                part = path.with_name(f"{path.name}.part{index:02d}")
                part.write_bytes(chunk)
                parts.append(part)
                index += 1

        # 파트가 모두 쓰인 뒤에 원본을 지운다. 삭제 실패도 PackError로 감싸
        # 호출자가 OSError를 따로 처리하지 않아도 되게 한다.
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise PackError(f"cannot split {path.name}: {exc.strerror}") from exc

    logger.info("Split %s into %d parts", path.name, len(parts))
    return parts


def pack_for_send(
    src: Path,
    dest_dir: Path,
    password: str,
    max_bytes: int = MAX_PART_BYTES,
) -> list[Path]:
    """전송할 파일 목록을 만든다. 한도를 넘으면 분할된 파트들을 돌려준다.

    Raises:
        PackError: 원본을 읽을 수 없거나 포장에 실패했을 때.
    """
    if not src.is_file():
        raise PackError(f"cannot read source file {src.name}")

    archive = make_encrypted_zip(src, dest_dir, password)
    if archive.stat().st_size <= max_bytes:
        return [archive]
    return split_file(archive, max_bytes)


def _make_encrypted_dir_zip(
    src_dir: Path, dest_dir: Path, password: str, store_root: Path
) -> Path:
    """디렉토리 하위 전체를 AES-256 암호 ZIP으로 포장한다.

    아카이브 안의 경로는 대상 디렉토리 이름부터 시작하므로, 풀면 폴더 하나가
    통째로 나온다. `store.resolves_inside` 로 저장소 밖으로 풀리는 심볼릭
    링크를 걸러낸다 — `store.directory_stats` 와 같은 규칙을 써야 확인 화면의
    파일 개수·크기와 실제로 담기는 내용이 어긋나지 않는다.

    Raises:
        PackError: 읽기·쓰기에 실패했을 때.
    """
    base = store_root.resolve()
    archive = dest_dir / (src_dir.name + ".zip")
    try:
        with pyzipper.AESZipFile(
            archive,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            for path in sorted(src_dir.rglob("*")):
                if not store.resolves_inside(base, path):
                    continue
                if path.is_file():
                    zf.write(
                        path,
                        arcname=str(Path(src_dir.name) / path.relative_to(src_dir)),
                    )
    except OSError as exc:
        archive.unlink(missing_ok=True)
        raise PackError(
            f"cannot read or write while packing {src_dir.name}: {exc.strerror}"
        ) from exc

    logger.info(
        "Packed directory %s into %d bytes", src_dir.name, archive.stat().st_size
    )
    return archive


def pack_dir_for_send(
    src_dir: Path,
    dest_dir: Path,
    password: str,
    store_root: Path,
    max_bytes: int = MAX_PART_BYTES,
) -> list[Path]:
    """폴더를 압축해 전송할 파일 목록을 만든다.

    Args:
        store_root: 저장소 루트. 심볼릭 링크가 이 밖으로 풀리면 아카이브에서
            제외한다.

    Raises:
        PackError: 대상이 디렉토리가 아니거나 포장에 실패했을 때.
    """
    if not src_dir.is_dir():
        raise PackError(f"source {src_dir.name} is not a directory")

    archive = _make_encrypted_dir_zip(src_dir, dest_dir, password, store_root)
    if archive.stat().st_size <= max_bytes:
        return [archive]
    return split_file(archive, max_bytes)
