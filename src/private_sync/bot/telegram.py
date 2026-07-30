"""텔레그램 Bot API 얇은 래퍼.

python-telegram-bot 을 쓰지 않고 raw HTTP만 사용한다. 서버가 아웃바운드로만
연결하는 롱폴링 구조를 그대로 유지하기 위함이다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from private_sync.errors import TelegramError

logger = logging.getLogger(__name__)

# getUpdates 롱폴링 대기(초). 이 값만큼 봇 응답이 늦어질 수 있다.
LONG_POLL_SEC = 20

_API = "https://api.telegram.org/bot{}/{}"
_POST_TIMEOUT_SEC = 30
_UPLOAD_TIMEOUT_SEC = 300


def build_keyboard(buttons: tuple[tuple[str, str], ...]) -> str | None:
    """버튼 목록을 inline_keyboard JSON 문자열로 만든다. 비면 None."""
    if not buttons:
        return None
    rows = [[{"text": label, "callback_data": data}] for label, data in buttons]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


class TelegramClient:
    """Bot API 호출을 담당한다."""

    def __init__(self, token: str, session: requests.Session | None = None) -> None:
        self._token = token
        self._session = session or requests.Session()

    def _url(self, method: str) -> str:
        return _API.format(self._token, method)

    def get_updates(self, offset: int | None) -> list[dict]:
        """롱폴링으로 새 update 목록을 가져온다.

        Raises:
            TelegramError: 네트워크 오류 또는 비정상 응답.
        """
        params: dict[str, object] = {"timeout": LONG_POLL_SEC}
        if offset is not None:
            params["offset"] = offset

        response = self._request(
            "get", "getUpdates", params=params, timeout=LONG_POLL_SEC + 10
        )
        result = response.get("result")
        if not isinstance(result, list):
            raise TelegramError("getUpdates returned unexpected payload")
        return result

    def send_message(
        self, chat_id: str, text: str, buttons: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """텍스트 메시지를 보낸다."""
        data: dict[str, object] = {"chat_id": chat_id, "text": text}
        keyboard = build_keyboard(buttons)
        if keyboard:
            data["reply_markup"] = keyboard
        self._request("post", "sendMessage", data=data, timeout=_POST_TIMEOUT_SEC)

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """기존 메시지의 본문과 버튼을 바꾼다."""
        data: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        keyboard = build_keyboard(buttons)
        if keyboard:
            data["reply_markup"] = keyboard
        self._request("post", "editMessageText", data=data, timeout=_POST_TIMEOUT_SEC)

    def send_document(self, chat_id: str, path: Path, caption: str = "") -> None:
        """파일을 문서로 보낸다.

        Raises:
            TelegramError: 파일을 열 수 없거나 전송이 실패했을 때.
        """
        try:
            with path.open("rb") as handle:
                self._request(
                    "post",
                    "sendDocument",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"document": (path.name, handle)},
                    timeout=_UPLOAD_TIMEOUT_SEC,
                )
        except OSError as exc:
            raise TelegramError(
                f"cannot read document {path.name}: {exc.strerror}"
            ) from exc

    def answer_callback(self, callback_id: str) -> None:
        """버튼 탭의 로딩 표시를 해제한다."""
        self._request(
            "post",
            "answerCallbackQuery",
            data={"callback_query_id": callback_id},
            timeout=_POST_TIMEOUT_SEC,
        )

    def _request(
        self,
        verb: str,
        method: str,
        params: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        timeout: int = _POST_TIMEOUT_SEC,
    ) -> dict:
        """API를 호출하고 JSON 본문을 반환한다.

        예외 메시지에는 URL을 넣지 않는다. URL에 봇 토큰이 들어 있다.
        """
        url = self._url(method)
        try:
            if verb == "get":
                response = self._session.get(url, params=params, timeout=timeout)
            else:
                response = self._session.post(
                    url, data=data, files=files, timeout=timeout
                )
        except requests.RequestException as exc:
            raise TelegramError(
                f"{method} request failed: {type(exc).__name__}"
            ) from exc

        if not response.ok:
            raise TelegramError(f"{method} returned status {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method} returned non-JSON body") from exc

        if not isinstance(payload, dict):
            raise TelegramError(f"{method} returned unexpected payload")
        return payload
