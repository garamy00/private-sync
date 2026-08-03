import json
import logging

import pytest
import requests

from private_sync.bot.telegram import TelegramClient, build_keyboard
from private_sync.errors import TelegramError


class _FakeResponse:
    def __init__(self, payload=None, ok=True, status_code=200):
        self._payload = payload or {"ok": True, "result": []}
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self._response = response or _FakeResponse()
        self._exc = exc

    def get(self, url, params=None, timeout=None):
        self.calls.append(("get", url, params))
        if self._exc:
            raise self._exc
        return self._response

    def post(self, url, data=None, files=None, timeout=None):
        self.calls.append(("post", url, data))
        if self._exc:
            raise self._exc
        return self._response


def test_build_keyboard_puts_one_button_per_row():
    raw = build_keyboard((("A", "t1"), ("B", "t2")))

    assert json.loads(raw) == {
        "inline_keyboard": [
            [{"text": "A", "callback_data": "t1"}],
            [{"text": "B", "callback_data": "t2"}],
        ]
    }


def test_build_keyboard_returns_none_when_empty():
    assert build_keyboard(()) is None


def test_get_updates_passes_offset_and_returns_result():
    session = _FakeSession(_FakeResponse({"ok": True, "result": [{"update_id": 5}]}))
    client = TelegramClient("tok", session=session)

    updates = client.get_updates(offset=4)

    assert updates == [{"update_id": 5}]
    _, url, params = session.calls[0]
    assert url.endswith("/getUpdates")
    assert params["offset"] == 4


def test_send_message_includes_keyboard():
    session = _FakeSession()
    client = TelegramClient("tok", session=session)

    client.send_message("123", "hello", buttons=(("A", "t1"),))

    _, url, data = session.calls[0]
    assert url.endswith("/sendMessage")
    assert data["chat_id"] == "123"
    assert json.loads(data["reply_markup"])["inline_keyboard"]


def test_network_error_message_omits_token():
    session = _FakeSession(
        exc=requests.ConnectionError("https://api.telegram.org/botSECRET/x failed")
    )
    client = TelegramClient("SECRET", session=session)

    with pytest.raises(TelegramError) as excinfo:
        client.send_message("123", "hi")

    # 예외 메시지에 토큰이 새면 안 된다
    assert "SECRET" not in str(excinfo.value)
    assert "ConnectionError" in str(excinfo.value)


def test_http_error_raises_telegram_error():
    session = _FakeSession(_FakeResponse(ok=False, status_code=403))
    client = TelegramClient("tok", session=session)

    with pytest.raises(TelegramError, match="403"):
        client.send_message("123", "hi")


def test_logging_the_exception_chain_does_not_leak_token(caplog):
    session = _FakeSession(
        exc=requests.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org'): /botSECRET/sendMessage refused"
        )
    )
    client = TelegramClient("SECRET", session=session)

    with caplog.at_level(logging.ERROR):
        try:
            client.send_message("123", "hi")
        except TelegramError:
            # CLAUDE.md 가 권하는 방식. 체인이 붙어 있으면 여기서 토큰이 샌다.
            logging.getLogger("probe").exception("send failed")

    assert "SECRET" not in caplog.text


def test_send_document_opens_file_and_posts(tmp_path):
    document = tmp_path / "a.zip"
    document.write_bytes(b"zip")
    session = _FakeSession()
    client = TelegramClient("tok", session=session)

    client.send_document("123", document, caption="a")

    _, url, data = session.calls[0]
    assert url.endswith("/sendDocument")
    assert data["caption"] == "a"


def test_default_base_builds_the_public_url():
    session = _FakeSession()
    client = TelegramClient("tok", session=session)

    client.get_updates(offset=None)

    _, url, _ = session.calls[0]
    assert url == "https://api.telegram.org/bottok/getUpdates"


def test_custom_base_is_used_verbatim():
    session = _FakeSession()
    client = TelegramClient("tok", session=session, api_base="http://127.0.0.1:8081")

    client.get_updates(offset=None)

    _, url, _ = session.calls[0]
    assert url == "http://127.0.0.1:8081/bottok/getUpdates"


def test_get_me_returns_the_result_payload():
    session = _FakeSession(_FakeResponse({"ok": True, "result": {"username": "mybot"}}))
    client = TelegramClient("tok", session=session)

    assert client.get_me() == {"username": "mybot"}


def test_public_api_uploads_the_file_body(tmp_path):
    document = tmp_path / "a.zip"
    document.write_bytes(b"zip")
    session = _FakeSession()
    client = TelegramClient("tok", session=session)

    client.send_document("123", document, caption="a")

    _, _, data = session.calls[0]
    assert "document" not in data


def test_local_api_sends_the_path_instead_of_the_body(tmp_path):
    document = tmp_path / "a.zip"
    document.write_bytes(b"zip")
    session = _FakeSession()
    client = TelegramClient("tok", session=session, api_base="http://127.0.0.1:8081")

    client.send_document("123", document, caption="a")

    _, _, data = session.calls[0]
    # 같은 장비이므로 본문을 HTTP 로 밀어 넣을 이유가 없다
    assert data["document"] == f"file://{document}"


def test_local_api_upload_does_not_open_the_file(tmp_path):
    missing = tmp_path / "없는파일.zip"
    session = _FakeSession()
    client = TelegramClient("tok", session=session, api_base="http://127.0.0.1:8081")

    # 파일을 읽지 않으므로 존재하지 않아도 요청은 나간다. 판단은 서버가 한다.
    client.send_document("123", missing, caption="a")

    assert session.calls
