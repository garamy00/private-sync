from pathlib import Path

import pytest

from private_sync.config import (
    DEFAULT_EXCLUDES,
    load_agent_config,
    load_bot_config,
    normalize_remote_store,
)
from private_sync.errors import ConfigError


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_agent_config_loads_directory_and_file_paths(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    quote = _write(tmp_path / "quote.xlsx", "x")

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: ~/private-sync/store
sources:
  - label: SKT 문서
    paths:
      - {docs}
      - {quote}
    exclude: ["*.tmp"]
""",
    )

    conf = load_agent_config(cfg)

    assert conf.remote.host == "dgson@ai"
    assert conf.remote.store == "private-sync/store"
    assert len(conf.sources) == 1
    assert conf.sources[0].paths == (docs, quote)
    # 사용자 지정 패턴은 내장 기본 제외 목록에 더해진다
    assert conf.sources[0].exclude == DEFAULT_EXCLUDES + ("*.tmp",)


def test_agent_config_rejects_missing_path(tmp_path):
    cfg = _write(
        tmp_path / "agent.yaml",
        """
remote:
  host: dgson@ai
  store: store
sources:
  - label: 문서
    paths:
      - /nonexistent/place/xyz
""",
    )

    with pytest.raises(ConfigError, match="does not exist"):
        load_agent_config(cfg)


def test_agent_config_rejects_duplicate_labels(tmp_path):
    first = tmp_path / "a"
    first.mkdir()
    second = tmp_path / "b"
    second.mkdir()

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: 문서
    paths: [{first}]
  - label: 문서
    paths: [{second}]
""",
    )

    with pytest.raises(ConfigError, match="duplicate source labels"):
        load_agent_config(cfg)


def test_agent_config_rejects_colliding_store_names(tmp_path):
    left = tmp_path / "left"
    left.mkdir()
    right = tmp_path / "right"
    right.mkdir()
    _write(left / "notes.md", "l")
    _write(right / "notes.md", "r")

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: 문서
    paths:
      - {left / "notes.md"}
      - {right / "notes.md"}
""",
    )

    with pytest.raises(ConfigError, match="conflicting store names"):
        load_agent_config(cfg)


def test_agent_config_rejects_label_with_path_separator(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: a/b
    paths: [{docs}]
""",
    )

    with pytest.raises(ConfigError, match="must not contain"):
        load_agent_config(cfg)


def test_agent_config_rejects_scalar_exclude(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()

    # 리스트 대신 문자열을 쓰면 한 글자씩 순회돼 '*' 패턴이 생기고,
    # 그 패턴이 모든 경로에 걸려 아무것도 동기화되지 않는다
    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: dgson@ai
  store: store
sources:
  - label: 문서
    paths: [{docs}]
    exclude: "*.tmp"
""",
    )

    with pytest.raises(ConfigError, match="exclude must be a list"):
        load_agent_config(cfg)


def test_normalize_remote_store_strips_home_prefix():
    assert normalize_remote_store("~/private-sync/store/") == "private-sync/store"
    assert normalize_remote_store("/srv/store/") == "/srv/store"


def test_bot_config_reads_secrets_from_env(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    conf = load_bot_config(
        cfg,
        env={
            "PRIVATE_SYNC_BOT_TOKEN": "tok",
            "PRIVATE_SYNC_CHAT_ID": "123",
            "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
        },
    )

    assert conf.store == store
    assert conf.token == "tok"
    assert conf.chat_id == "123"
    assert conf.zip_password == "pw"


def test_bot_config_reports_all_missing_env_vars(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    with pytest.raises(ConfigError, match="PRIVATE_SYNC_ZIP_PASSWORD"):
        load_bot_config(cfg, env={"PRIVATE_SYNC_BOT_TOKEN": "tok"})
