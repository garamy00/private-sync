"""YAML 설정을 읽어 검증된 dataclass로 변환한다."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from private_sync.errors import ConfigError

# macOS·오피스·에디터가 만드는 잡파일은 설정과 무관하게 항상 제외한다
DEFAULT_EXCLUDES: tuple[str, ...] = (".DS_Store", "~$*", "*.swp", ".git/")

# 라벨은 서버 저장소의 디렉토리명이 되므로 경로 조작 문자를 허용하지 않는다
_FORBIDDEN_IN_LABEL = ("/", "\\", "..", "\n")


@dataclass(frozen=True)
class Source:
    """봇 화면의 한 라벨과 그에 속한 동기화 경로들."""

    label: str
    paths: tuple[Path, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class RemoteConfig:
    """SSH 대상과 원격 저장소 경로."""

    host: str
    store: str


@dataclass(frozen=True)
class AgentConfig:
    """노트북 agent 설정."""

    remote: RemoteConfig
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class BotConfig:
    """서버 bot 설정. 비밀값은 환경변수에서 온다."""

    store: Path
    token: str
    chat_id: str
    zip_password: str
    api_base: str
    max_part_bytes: int


def normalize_remote_store(raw: str) -> str:
    """원격 저장소 경로를 정규화한다.

    ssh·rsync가 원격 경로의 `~` 를 확장해주지 않는 경우가 있어, 홈 기준 상대
    경로로 바꿔 원격 CWD(홈)에 의존하게 만든다.
    """
    if raw.startswith("~/"):
        return raw[2:].rstrip("/")
    return raw.rstrip("/")


def _read_yaml(path: Path) -> dict:
    """YAML 파일을 매핑으로 읽는다."""
    try:
        with path.open(encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a mapping")
    return data


def _validate_label(label: str) -> None:
    """라벨이 디렉토리명으로 안전한지 확인한다."""
    if not label.strip():
        raise ConfigError("source label must not be empty")
    for bad in _FORBIDDEN_IN_LABEL:
        if bad in label:
            raise ConfigError(f"source label {label!r} must not contain {bad!r}")


def _build_source(raw: dict) -> Source:
    """sources 항목 하나를 검증해 Source로 만든다."""
    if not isinstance(raw, dict):
        raise ConfigError(f"each source must be a mapping, got {type(raw).__name__}")

    label = str(raw.get("label", ""))
    _validate_label(label)

    raw_paths = raw.get("paths") or []
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ConfigError(f"source {label!r} must define a non-empty paths list")

    paths: list[Path] = []
    for item in raw_paths:
        path = Path(str(item)).expanduser()
        if not path.exists():
            raise ConfigError(f"source {label!r} path does not exist: {path}")
        # watchdog은 실제 경로(realpath)로 이벤트를 보고하므로, 심볼릭 링크를
        # 거치는 설정 경로도 미리 resolve해 저장해야 이벤트와 계속 매칭된다
        # 순환 링크는 위 존재 확인에서 이미 걸린다. 이 처리는 그 사이에 링크가
        # 바뀌는 TOCTOU 대비다. 또한 Python 3.13+ 의 resolve() 는 순환에도
        # RuntimeError 를 던지지 않으므로 이 갈래는 언젠가 죽은 코드가 된다.
        try:
            path = path.resolve()
        except RuntimeError as exc:
            raise ConfigError(
                f"source {label!r} path has a symlink loop: {path}"
            ) from exc
        paths.append(path)

    # 저장 이름이 겹치면 서버에서 서로 덮어쓰므로 시작 시점에 잡는다
    names = [p.name for p in paths]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"source {label!r} has conflicting store names: {duplicates}")

    raw_exclude = raw.get("exclude") or []
    if not isinstance(raw_exclude, list):
        raise ConfigError(f"source {label!r} exclude must be a list")

    exclude = tuple(str(x) for x in raw_exclude)
    return Source(label=label, paths=tuple(paths), exclude=DEFAULT_EXCLUDES + exclude)


def load_agent_config(path: Path) -> AgentConfig:
    """노트북 agent 설정을 읽고 검증한다.

    Raises:
        ConfigError: 필수 항목 누락, 없는 경로, 라벨 중복, 저장 이름 충돌.
    """
    data = _read_yaml(path)

    remote_raw = data.get("remote") or {}
    if not isinstance(remote_raw, dict):
        raise ConfigError("remote must be a mapping with host and store")

    host = str(remote_raw.get("host", ""))
    store = str(remote_raw.get("store", ""))
    if not host or not store:
        raise ConfigError("remote.host and remote.store are required")

    raw_sources = data.get("sources") or []
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("at least one source is required")

    sources = tuple(_build_source(item) for item in raw_sources)

    labels = [s.label for s in sources]
    if len(set(labels)) != len(labels):
        raise ConfigError(f"duplicate source labels: {sorted(labels)}")

    return AgentConfig(
        remote=RemoteConfig(host=host, store=normalize_remote_store(store)),
        sources=sources,
    )


def load_bot_config(path: Path, env: Mapping[str, str] | None = None) -> BotConfig:
    """서버 bot 설정을 읽고 비밀값은 환경변수에서 가져온다.

    Raises:
        ConfigError: store 누락·부재 또는 환경변수 누락.
    """
    env = os.environ if env is None else env
    data = _read_yaml(path)

    raw_store = str(data.get("store", ""))
    if not raw_store:
        raise ConfigError("store is required")
    store = Path(raw_store).expanduser()
    if not store.is_dir():
        raise ConfigError(f"store directory does not exist: {store}")

    secrets = {
        name: env.get(name, "")
        for name in (
            "PRIVATE_SYNC_BOT_TOKEN",
            "PRIVATE_SYNC_CHAT_ID",
            "PRIVATE_SYNC_ZIP_PASSWORD",
        )
    }
    missing = [name for name, value in secrets.items() if not value]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")

    api_base = env.get("PRIVATE_SYNC_API_BASE", "https://api.telegram.org").rstrip("/")

    raw_part_mb = env.get("PRIVATE_SYNC_MAX_PART_MB", "45")
    if not raw_part_mb.isdigit() or int(raw_part_mb) <= 0:
        raise ConfigError(
            f"PRIVATE_SYNC_MAX_PART_MB must be a positive integer, got {raw_part_mb!r}"
        )

    return BotConfig(
        store=store,
        token=secrets["PRIVATE_SYNC_BOT_TOKEN"],
        chat_id=secrets["PRIVATE_SYNC_CHAT_ID"],
        zip_password=secrets["PRIVATE_SYNC_ZIP_PASSWORD"],
        api_base=api_base,
        max_part_bytes=int(raw_part_mb) * 1024 * 1024,
    )
