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
  host: user@sync-server
  store: ~/private-sync/store
sources:
  - label: 업무 문서
    paths:
      - {docs}
      - {quote}
    exclude: ["*.tmp"]
""",
    )

    conf = load_agent_config(cfg)

    assert conf.remote.host == "user@sync-server"
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
  host: user@sync-server
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
  host: user@sync-server
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
  host: user@sync-server
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
  host: user@sync-server
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
  host: user@sync-server
  store: store
sources:
  - label: 문서
    paths: [{docs}]
    exclude: "*.tmp"
""",
    )

    with pytest.raises(ConfigError, match="exclude must be a list"):
        load_agent_config(cfg)


def test_agent_config_rejects_scalar_remote(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote: user@sync-server
sources:
  - label: 문서
    paths: [{docs}]
""",
    )

    # 중첩 매핑을 빠뜨리는 흔한 오타다. 데몬을 죽이지 말고 거부해야 한다.
    with pytest.raises(ConfigError, match="remote must be a mapping"):
        load_agent_config(cfg)


def test_agent_config_rejects_scalar_source_entry(tmp_path):
    cfg = _write(
        tmp_path / "agent.yaml",
        """
remote:
  host: user@nonexistent.invalid
  store: store
sources:
  - 문서
""",
    )

    with pytest.raises(ConfigError, match="each source must be a mapping"):
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


def test_agent_config_resolves_symlinked_source_path(tmp_path):
    # /etc가 /private/etc의 심볼릭 링크인 macOS 사례를 그대로 재현한다.
    # watchdog은 실제 경로(/private/etc/hosts)로 이벤트를 보고하므로, 설정에
    # 심볼릭 링크 경로를 그대로 저장하면 감시 등록과 이벤트 경로가 어긋난다.
    real_dir = tmp_path / "real_docs"
    real_dir.mkdir()
    link = tmp_path / "link_docs"
    link.symlink_to(real_dir)

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: user@sync-server
  store: store
sources:
  - label: 문서
    paths:
      - {link}
""",
    )

    conf = load_agent_config(cfg)

    assert conf.sources[0].paths == (real_dir.resolve(),)


def test_agent_config_rejects_symlink_loop_path_as_nonexistent(tmp_path):
    """심볼릭 루프 경로가 데몬을 죽이지 않고 ConfigError 로 걸러지는지 확인한다.

    실제로 걸리는 지점은 resolve() 가 아니라 그 앞의 존재 확인이다. 순환 링크는
    따라갈 수 없으니 Path.exists() 가 이미 False 를 돌려주기 때문이다. 어느
    검사가 잡든 데몬이 죽지 않는 것이 요점이므로, 실제로 나오는 메시지를 그대로
    확인해 커버리지를 가진 척하지 않는다.
    """
    loop_a = tmp_path / "loop_a"
    loop_b = tmp_path / "loop_b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)

    cfg = _write(
        tmp_path / "agent.yaml",
        f"""
remote:
  host: user@sync-server
  store: store
sources:
  - label: 문서
    paths:
      - {loop_a}
""",
    )

    with pytest.raises(ConfigError, match="path does not exist"):
        load_agent_config(cfg)


def test_bot_config_reports_all_missing_env_vars(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    with pytest.raises(ConfigError, match="PRIVATE_SYNC_ZIP_PASSWORD"):
        load_bot_config(cfg, env={"PRIVATE_SYNC_BOT_TOKEN": "tok"})


def test_bot_config_defaults_to_the_public_api(tmp_path):
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

    # 미설정이면 지금과 동일하게 동작해야 한다
    assert conf.api_base == "https://api.telegram.org"
    assert conf.max_part_bytes == 45 * 1024 * 1024


def test_bot_config_reads_local_api_settings(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    conf = load_bot_config(
        cfg,
        env={
            "PRIVATE_SYNC_BOT_TOKEN": "tok",
            "PRIVATE_SYNC_CHAT_ID": "123",
            "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
            "PRIVATE_SYNC_API_BASE": "http://127.0.0.1:8081/",
            "PRIVATE_SYNC_MAX_PART_MB": "1900",
        },
    )

    # 끝의 슬래시는 URL 조립에서 중복되므로 떼어둔다
    assert conf.api_base == "http://127.0.0.1:8081"
    assert conf.max_part_bytes == 1900 * 1024 * 1024


def test_bot_config_rejects_non_numeric_part_size(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    # "②" 는 str.isdigit() 이 True 지만 int() 가 거부한다. 0 과 음수도 함께 본다.
    for bad in ("많이", "②", "0", "-5"):
        with pytest.raises(ConfigError, match="PRIVATE_SYNC_MAX_PART_MB"):
            load_bot_config(
                cfg,
                env={
                    "PRIVATE_SYNC_BOT_TOKEN": "tok",
                    "PRIVATE_SYNC_CHAT_ID": "123",
                    "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
                    "PRIVATE_SYNC_MAX_PART_MB": bad,
                },
            )


def test_bot_config_rejects_part_size_above_the_public_api_limit(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    # 45 를 450 으로 잘못 입력한 흔한 오타. 걸러지지 않으면 매 sendDocument 가
    # 413 으로 실패하고 사용자는 이유를 알 수 없다.
    with pytest.raises(ConfigError, match="PRIVATE_SYNC_MAX_PART_MB"):
        load_bot_config(
            cfg,
            env={
                "PRIVATE_SYNC_BOT_TOKEN": "tok",
                "PRIVATE_SYNC_CHAT_ID": "123",
                "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
                "PRIVATE_SYNC_MAX_PART_MB": "450",
            },
        )


def test_bot_config_rejects_part_size_above_the_local_api_limit(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    # 로컬 서버 문서상 한도는 2000MB 다.
    with pytest.raises(ConfigError, match="PRIVATE_SYNC_MAX_PART_MB"):
        load_bot_config(
            cfg,
            env={
                "PRIVATE_SYNC_BOT_TOKEN": "tok",
                "PRIVATE_SYNC_CHAT_ID": "123",
                "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
                "PRIVATE_SYNC_API_BASE": "http://127.0.0.1:8081",
                "PRIVATE_SYNC_MAX_PART_MB": "2001",
            },
        )


def test_bot_config_rejects_api_base_with_a_path_component(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    # PRIVATE_SYNC_API_BASE 라는 이름 때문에 봇 URL 전체
    # (토큰 포함)를 넣는 실수가 나올 수 있다. 거부하되 값 자체는 메시지에
    # 담지 않아야 한다 — 담으면 로그에 토큰이 남는다.
    with pytest.raises(ConfigError) as excinfo:
        load_bot_config(
            cfg,
            env={
                "PRIVATE_SYNC_BOT_TOKEN": "tok",
                "PRIVATE_SYNC_CHAT_ID": "123",
                "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
                "PRIVATE_SYNC_API_BASE": "https://api.telegram.org/botSECRETTOKEN",
            },
        )

    assert "PRIVATE_SYNC_API_BASE" in str(excinfo.value)
    assert "SECRETTOKEN" not in str(excinfo.value)


def test_bot_config_rejects_api_base_with_a_password(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    # username 만 보고 password 를 놓치면 "https://:secret@host" 처럼 빈
    # username 에 비밀번호만 실은 URL 이 그대로 통과해 저널에 남는다.
    with pytest.raises(ConfigError) as excinfo:
        load_bot_config(
            cfg,
            env={
                "PRIVATE_SYNC_BOT_TOKEN": "tok",
                "PRIVATE_SYNC_CHAT_ID": "123",
                "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
                "PRIVATE_SYNC_API_BASE": "https://:secretpw@host",
            },
        )

    assert "PRIVATE_SYNC_API_BASE" in str(excinfo.value)
    assert "secretpw" not in str(excinfo.value)


def test_bot_config_rejects_api_base_without_a_scheme(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    with pytest.raises(ConfigError, match="PRIVATE_SYNC_API_BASE"):
        load_bot_config(
            cfg,
            env={
                "PRIVATE_SYNC_BOT_TOKEN": "tok",
                "PRIVATE_SYNC_CHAT_ID": "123",
                "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
                "PRIVATE_SYNC_API_BASE": "api.telegram.org",
            },
        )


def test_bot_config_bounds_part_size_for_a_differently_cased_public_api_base(
    tmp_path,
):
    store = tmp_path / "store"
    store.mkdir()
    cfg = _write(tmp_path / "bot.yaml", f"store: {store}\n")

    # 대소문자만 다를 뿐 공개 API 다. 문자열을 그대로 비교하면 로컬 서버로
    # 오인되어 2000MB 상한이 적용되고, 실제로는 50MB 를 넘을 때마다
    # sendDocument 가 413 으로 실패한다.
    with pytest.raises(ConfigError, match="PRIVATE_SYNC_MAX_PART_MB"):
        load_bot_config(
            cfg,
            env={
                "PRIVATE_SYNC_BOT_TOKEN": "tok",
                "PRIVATE_SYNC_CHAT_ID": "123",
                "PRIVATE_SYNC_ZIP_PASSWORD": "pw",
                "PRIVATE_SYNC_API_BASE": "HTTPS://API.TELEGRAM.ORG",
                "PRIVATE_SYNC_MAX_PART_MB": "1900",
            },
        )
