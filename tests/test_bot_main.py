import logging
import os
from pathlib import Path

import pytest

from private_sync.bot.handlers import (
    Context,
    Incoming,
    SendFile,
    SendText,
    TokenMap,
)
from private_sync.bot.main import Deliverer, _handle_one, _serve, main
from private_sync.bot.store import DirStats
from private_sync.config import BotConfig
from private_sync.errors import PackError, StoreError, TelegramError


def _unused_stats(_rel):
    """이 파일의 테스트들은 폴더 크기 조회를 실행하지 않으므로 값은 무의미하다."""
    return DirStats(files=0, total_bytes=0)


class _SpyClient:
    def __init__(self, fail_on=None):
        self.messages = []
        self.edits = []
        self.documents = []
        self.answered = []
        self.attempts = []
        self._fail_on = fail_on

    def send_message(self, chat_id, text, buttons=()):
        # 실패하더라도 시도했다는 사실은 남긴다
        self.attempts.append(text)
        if self._fail_on in ("message", "all"):
            raise TelegramError("sendMessage returned status 500")
        self.messages.append((chat_id, text, buttons))

    def edit_message_text(self, chat_id, message_id, text, buttons=()):
        if self._fail_on in ("edit", "all"):
            raise TelegramError("editMessageText returned status 400")
        self.edits.append((chat_id, message_id, text, buttons))

    def send_document(self, chat_id, path, caption=""):
        # 첫 파트만 성공시키고 그다음부터 실패시켜 부분 전송을 만든다
        if self._fail_on == "document_after_1" and self.documents:
            raise TelegramError("sendDocument returned status 500")
        self.documents.append((chat_id, Path(path).name, caption))

    def answer_callback(self, callback_id):
        if self._fail_on == "answer":
            raise TelegramError("answerCallbackQuery returned status 400")
        self.answered.append(callback_id)


def _fake_pack(parts):
    """지정한 개수의 가짜 파트 파일을 만드는 pack_for_send 대체품."""

    def pack(src, dest_dir, password, max_bytes=None):
        made = []
        for index in range(1, parts + 1):
            part = Path(dest_dir) / f"{src.name}.zip.part{index:02d}"
            part.write_bytes(b"x")
            made.append(part)
        return made

    return pack


@pytest.fixture
def config(tmp_path):
    store = tmp_path / "store"
    (store / "메모").mkdir(parents=True)
    (store / "메모" / "a.txt").write_bytes(b"hello")
    return BotConfig(
        store=store,
        token="tok",
        chat_id="123",
        zip_password="pw",
        api_base="https://api.telegram.org",
        max_part_bytes=45 * 1024 * 1024,
    )


def _callback(text="tok"):
    return Incoming(
        kind="callback", chat_id="123", text=text, message_id=9, callback_id="cb1"
    )


def _message(text="/start"):
    return Incoming(
        kind="message", chat_id="123", text=text, message_id=1, callback_id=None
    )


def test_send_text_from_message_sends_new_message(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(SendText(text="hi", buttons=(("A", "t"),)), _message())

    assert client.messages == [("123", "hi", (("A", "t"),))]
    assert client.edits == []


def test_send_text_with_edit_updates_existing_message(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(SendText(text="dir", buttons=(), edit=True), _callback())

    assert client.edits == [("123", 9, "dir", ())]
    # 버튼 탭의 로딩 표시를 해제한다
    assert client.answered == ["cb1"]


def test_send_file_delivers_encrypted_zip(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(SendFile(rel="메모/a.txt", caption="a.txt"), _callback())

    assert len(client.documents) == 1
    chat_id, name, caption = client.documents[0]
    assert chat_id == "123"
    assert name == "a.txt.zip"
    assert "a.txt" in caption


def test_send_file_splits_when_source_exceeds_configured_max_part_bytes(tmp_path):
    # config 픽스처의 max_part_bytes 는 건드리지 않는다. 여기서만 작은 한도를
    # 준 별도 BotConfig 로, 배선이 끊기면(패커 기본값 45MB 로 되돌아가면)
    # 이 작은 파일은 절대 나뉘지 않는다는 사실로 회귀를 잡는다.
    store = tmp_path / "store"
    (store / "메모").mkdir(parents=True)
    # 무작위 바이트라 압축해도 줄지 않으므로 분할 여부가 max_part_bytes 에만 달렸다
    (store / "메모" / "big.bin").write_bytes(os.urandom(200 * 1024))
    small_part_config = BotConfig(
        store=store,
        token="tok",
        chat_id="123",
        zip_password="pw",
        api_base="https://api.telegram.org",
        max_part_bytes=64 * 1024,
    )
    client = _SpyClient()
    deliverer = Deliverer(client, small_part_config)

    deliverer.run(SendFile(rel="메모/big.bin", caption="big.bin"), _callback())

    assert len(client.documents) > 1


def test_send_file_cleans_up_temp_files(config, monkeypatch):
    created = []

    import private_sync.bot.main as bot_main

    original = bot_main.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = original(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(bot_main.tempfile, "mkdtemp", tracking_mkdtemp)
    deliverer = Deliverer(_SpyClient(), config)

    deliverer.run(SendFile(rel="메모/a.txt", caption="a.txt"), _callback())

    assert created and not created[0].exists()


def test_missing_file_reports_to_user(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(SendFile(rel="메모/없음.txt", caption="없음.txt"), _callback())

    assert "동기화 대기 중" in client.messages[0][1]


def test_none_action_does_nothing(config):
    client = _SpyClient()
    deliverer = Deliverer(client, config)

    deliverer.run(None, _message())

    assert client.messages == []
    assert client.documents == []


def test_send_document_failure_cleans_up_and_reports_partial(config, monkeypatch):
    import private_sync.bot.main as bot_main

    created = []
    original = bot_main.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = original(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(bot_main.tempfile, "mkdtemp", tracking_mkdtemp)
    # 3개 파트로 쪼개지도록 작은 한도를 준다
    monkeypatch.setattr(bot_main, "pack_for_send", _fake_pack(parts=3))

    client = _SpyClient(fail_on="document_after_1")
    Deliverer(client, config).run(
        SendFile(rel="메모/a.txt", caption="a.txt"), _callback()
    )

    assert len(client.documents) == 1
    assert "이미 받은 파트는 지우고" in client.messages[-1][1]
    # 실패해도 평문·암호문이 서버에 남으면 안 된다
    assert created and not created[0].exists()


def test_pack_failure_cleans_up_temp_dir(config, monkeypatch):
    import private_sync.bot.main as bot_main

    created = []
    original = bot_main.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = original(*args, **kwargs)
        created.append(Path(path))
        return path

    def exploding_pack(*_args, **_kwargs):
        raise PackError("cannot read source file a.txt")

    monkeypatch.setattr(bot_main.tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(bot_main, "pack_for_send", exploding_pack)

    client = _SpyClient()
    Deliverer(client, config).run(
        SendFile(rel="메모/a.txt", caption="a.txt"), _callback()
    )

    assert "포장하는 중 오류" in client.messages[-1][1]
    assert created and not created[0].exists()


def test_answer_callback_failure_does_not_abort_delivery(config):
    client = _SpyClient(fail_on="answer")

    Deliverer(client, config).run(
        SendFile(rel="메모/a.txt", caption="a.txt"), _callback()
    )

    # 로딩 표시 해제 실패는 전달을 막지 않는다
    assert len(client.documents) == 1


def test_failed_edit_tells_the_user_instead_of_going_silent(config):
    client = _SpyClient(fail_on="edit")

    Deliverer(client, config).run(
        SendText(text="목록", buttons=(("A", "t"),), edit=True), _callback()
    )

    # 로그만 남기면 폰에서는 "눌러도 아무 반응 없음" 으로 보인다
    assert client.messages
    assert "표시할 수 없습니다" in client.messages[-1][1]


def test_failure_notice_that_also_fails_stays_quiet(config):
    client = _SpyClient(fail_on="all")

    # 알림마저 실패해도 예외가 밖으로 나오면 안 된다
    Deliverer(client, config).run(
        SendText(text="목록", buttons=(("A", "t"),), edit=True), _callback()
    )

    # 빈 messages 만 보면 "알림을 아예 안 보낸 것" 과 구분되지 않는다.
    # 시도했는지를 함께 본다.
    assert client.messages == []
    assert any("표시할 수 없습니다" in text for text in client.attempts)


def _start_update():
    return {"message": {"text": "/start", "chat": {"id": 123}, "message_id": 1}}


def test_filesystem_error_while_listing_does_not_kill_the_loop(config):
    client = _SpyClient()

    def exploding_lister(_rel):
        # 깨진 심볼릭 링크나 심볼릭 루프에서 실제로 나오는 오류다
        raise OSError(62, "Too many levels of symbolic links")

    context = Context(
        chat_id="123",
        tokens=TokenMap(),
        lister=exploding_lister,
        searcher=lambda _keyword: [],
        stats=_unused_stats,
    )

    # 예외가 밖으로 나오면 봇 프로세스가 죽는다
    _handle_one(_start_update(), context, Deliverer(client, config))

    assert "오류가 발생했습니다" in client.messages[0][1]


def test_store_error_while_listing_reports_missing_file(config):
    client = _SpyClient()

    def missing_lister(_rel):
        raise StoreError("path 'x' not found in store")

    context = Context(
        chat_id="123",
        tokens=TokenMap(),
        lister=missing_lister,
        searcher=lambda _keyword: [],
        stats=_unused_stats,
    )

    _handle_one(_start_update(), context, Deliverer(client, config))

    assert "동기화 대기 중" in client.messages[0][1]


def _context_with_lister(lister, tokens=None):
    return Context(
        chat_id="123",
        tokens=tokens or TokenMap(),
        lister=lister,
        searcher=lambda _keyword: [],
        stats=_unused_stats,
    )


def test_runtime_error_while_listing_does_not_kill_the_loop(config):
    client = _SpyClient()

    def looping_lister(_rel):
        # 저장소 안의 심볼릭 루프에서 Path.resolve() 가 실제로 던지는 예외다.
        # OSError 가 아니므로 좁은 except 목록으로는 잡히지 않는다.
        raise RuntimeError("Symlink loop from '/store/loop'")

    # 예외가 밖으로 나오면 봇 프로세스가 죽고, 텔레그램이 같은 update 를
    # 다시 보내므로 재시작해도 같은 자리에서 또 죽는다
    _handle_one(
        _start_update(), _context_with_lister(looping_lister), Deliverer(client, config)
    )

    assert "오류가 발생했습니다" in client.messages[0][1]


def test_error_from_callback_clears_the_button_spinner(config):
    client = _SpyClient()
    tokens = TokenMap()
    token = tokens.put("dir", "메모")

    def looping_lister(_rel):
        raise RuntimeError("Symlink loop from '/store/loop'")

    update = {
        "callback_query": {
            "id": "cb1",
            "data": token,
            "message": {"message_id": 9, "chat": {"id": 123}},
        }
    }

    _handle_one(
        update, _context_with_lister(looping_lister, tokens), Deliverer(client, config)
    )

    # 콜백에 응답하지 않으면 휴대폰의 버튼 로딩 표시가 영원히 돈다
    assert client.answered == ["cb1"]
    assert "오류가 발생했습니다" in client.messages[0][1]


class _LoopStopped(Exception):
    """무한 루프를 테스트에서 빠져나오기 위한 신호."""


class _LoopClient(_SpyClient):
    """지정한 배치를 한 번 돌려주고 다음 호출에서 루프를 끊는다."""

    def __init__(self, updates):
        super().__init__()
        self._updates = updates
        self.offsets = []

    def get_updates(self, offset=None):
        self.offsets.append(offset)
        if self._updates is None:
            raise _LoopStopped
        batch, self._updates = self._updates, None
        return batch


def test_debug_mode_silences_the_layer_that_logs_the_token(tmp_path, monkeypatch):
    # urllib3 는 DEBUG 에서 요청 URL 을 그대로 찍는데, 봇 API URL 에는 토큰이 들어 있다.
    # 유효 레벨이 아니라 자체 레벨을 보는 이유는 루트 레벨과 무관하게 이 계층이
    # 눌려 있어야 저널에 토큰이 남지 않기 때문이다.
    urllib3_logger = logging.getLogger("urllib3")
    monkeypatch.setattr(urllib3_logger, "level", logging.NOTSET)

    # 설정 오류로 즉시 반환하지만 로깅 설정은 이미 끝난 뒤다
    assert main(["--config", str(tmp_path / "없는.yaml"), "--debug"]) == 1

    assert urllib3_logger.level == logging.WARNING


def test_malformed_update_entry_does_not_break_the_poll_loop(config):
    good = dict(_start_update(), update_id=7)
    client = _LoopClient(["not a dict", None, good])

    with pytest.raises(_LoopStopped):
        _serve(client, config)

    # 기형 항목을 건너뛰고 정상 update 까지 처리했으며, offset 도 전진했다
    assert client.messages, "정상 update 가 처리되지 않았다"
    assert client.offsets == [None, 8]
