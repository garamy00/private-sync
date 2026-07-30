from pathlib import Path

import pytest

from private_sync.bot.handlers import (
    Context,
    Incoming,
    SendFile,
    SendText,
    TokenMap,
)
from private_sync.bot.main import Deliverer, _handle_one
from private_sync.config import BotConfig
from private_sync.errors import PackError, StoreError, TelegramError


class _SpyClient:
    def __init__(self, fail_on=None):
        self.messages = []
        self.edits = []
        self.documents = []
        self.answered = []
        self._fail_on = fail_on

    def send_message(self, chat_id, text, buttons=()):
        if self._fail_on == "message":
            raise TelegramError("sendMessage returned status 500")
        self.messages.append((chat_id, text, buttons))

    def edit_message_text(self, chat_id, message_id, text, buttons=()):
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
    return BotConfig(store=store, token="tok", chat_id="123", zip_password="pw")


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
    )

    _handle_one(_start_update(), context, Deliverer(client, config))

    assert "동기화 대기 중" in client.messages[0][1]
