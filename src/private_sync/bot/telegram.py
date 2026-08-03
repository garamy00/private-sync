"""텔레그램 Bot API 얇은 래퍼.

python-telegram-bot 을 쓰지 않고 raw HTTP만 사용한다. 서버가 아웃바운드로만
연결하는 롱폴링 구조를 그대로 유지하기 위함이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

from private_sync.errors import TelegramError

# getUpdates 롱폴링 대기(초). 이 값만큼 봇 응답이 늦어질 수 있다.
LONG_POLL_SEC = 20

_DEFAULT_API_BASE = "https://api.telegram.org"
_POST_TIMEOUT_SEC = 30
_UPLOAD_TIMEOUT_SEC = 300


class _Response(Protocol):
    """requests.Response 중 이 모듈이 쓰는 부분.

    테스트가 가짜 세션을 주입할 수 있도록 최소 표면만 선언한다.
    """

    ok: bool
    status_code: int

    def json(self) -> object: ...


@dataclass(frozen=True)
class _PostBody:
    """POST 요청 본문과 첨부."""

    data: dict[str, object]
    files: dict | None = None
    timeout: int = _POST_TIMEOUT_SEC


def _decode(method: str, response: _Response) -> dict:
    """응답을 검증하고 JSON 본문을 반환한다.

    Raises:
        TelegramError: 비정상 상태 코드, 비-JSON 본문, 예상 밖 페이로드.
    """
    if not response.ok:
        raise TelegramError(f"{method} returned status {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramError(f"{method} returned non-JSON body") from exc

    if not isinstance(payload, dict):
        raise TelegramError(f"{method} returned unexpected payload")
    return payload


def build_keyboard(buttons: tuple[tuple[str, str], ...]) -> str | None:
    """버튼 목록을 inline_keyboard JSON 문자열로 만든다. 비면 None."""
    if not buttons:
        return None
    rows = [[{"text": label, "callback_data": data}] for label, data in buttons]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


# editMessageText 에서 버튼을 실제로 지우려면 빈 inline_keyboard 를 명시해야
# 한다. build_keyboard 는 sendMessage 쪽 "생략" 의미와 겹치지 않도록 이 값을
# 따로 반환하지 않는다.
_EMPTY_KEYBOARD = json.dumps({"inline_keyboard": []})


class TelegramClient:
    """Bot API 호출을 담당한다."""

    def __init__(
        self,
        token: str,
        session: requests.Session | None = None,
        api_base: str = _DEFAULT_API_BASE,
    ) -> None:
        self._token = token
        self._session = session or requests.Session()
        self._api_base = api_base

    def _url(self, method: str) -> str:
        """메서드 호출 URL을 만든다. 이 문자열은 어떤 예외·로그에도 넣지 않는다."""
        return f"{self._api_base}/bot{self._token}/{method}"

    def get_updates(self, offset: int | None) -> list[dict]:
        """롱폴링으로 새 update 목록을 가져온다.

        Raises:
            TelegramError: 네트워크 오류 또는 비정상 응답.
        """
        params: dict[str, object] = {"timeout": LONG_POLL_SEC}
        if offset is not None:
            params["offset"] = offset

        response = self._get("getUpdates", params, LONG_POLL_SEC + 10)
        result = response.get("result")
        if not isinstance(result, list):
            raise TelegramError("getUpdates returned unexpected payload")
        return result

    def get_me(self) -> dict:
        """봇 자신의 정보를 가져온다. 시작 시 API 도달 확인에 쓴다.

        Raises:
            TelegramError: 네트워크 오류 또는 비정상 응답.
        """
        response = self._get("getMe", {}, _POST_TIMEOUT_SEC)
        result = response.get("result")
        if not isinstance(result, dict):
            raise TelegramError("getMe returned unexpected payload")
        return result

    def send_message(
        self, chat_id: str, text: str, buttons: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """텍스트 메시지를 보낸다."""
        data: dict[str, object] = {"chat_id": chat_id, "text": text}
        keyboard = build_keyboard(buttons)
        if keyboard:
            data["reply_markup"] = keyboard
        self._post("sendMessage", _PostBody(data=data))

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """기존 메시지의 본문과 버튼을 바꾼다.

        `reply_markup` 을 생략하면 텔레그램은 "버튼 유지"로 해석해 이전
        버튼이 그대로 남는다. 버튼이 없는 편집에서는 빈 인라인 키보드를
        명시해 실제로 지운다. sendMessage 는 새 메시지라 남길 버튼이 없으므로
        여기와 달리 생략이 맞다.
        """
        data: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        data["reply_markup"] = build_keyboard(buttons) or _EMPTY_KEYBOARD
        self._post("editMessageText", _PostBody(data=data))

    def send_document(self, chat_id: str, path: Path, caption: str = "") -> None:
        """파일을 문서로 보낸다.

        로컬 Bot API 서버를 쓸 때는 본문을 HTTP 로 싣지 않고 경로만 넘긴다. API
        서버가 같은 장비에서 파일을 직접 읽으므로 큰 파일에서 복사 한 번이 사라진다.

        Raises:
            TelegramError: 파일을 열 수 없거나 전송이 실패했을 때.
        """
        data: dict[str, object] = {"chat_id": chat_id, "caption": caption}

        if self._api_base != _DEFAULT_API_BASE:
            data["document"] = f"file://{path}"
            self._post(
                "sendDocument", _PostBody(data=data, timeout=_UPLOAD_TIMEOUT_SEC)
            )
            return

        try:
            with path.open("rb") as handle:
                self._post(
                    "sendDocument",
                    _PostBody(
                        data=data,
                        files={"document": (path.name, handle)},
                        timeout=_UPLOAD_TIMEOUT_SEC,
                    ),
                )
        except OSError as exc:
            # OSError 에는 토큰이 없으므로 체인을 유지해 디버깅 정보를 남긴다
            raise TelegramError(
                f"cannot read document {path.name}: {exc.strerror}"
            ) from exc

    def answer_callback(self, callback_id: str) -> None:
        """버튼 탭의 로딩 표시를 해제한다."""
        self._post(
            "answerCallbackQuery", _PostBody(data={"callback_query_id": callback_id})
        )

    def _get(self, method: str, params: dict, timeout: int) -> dict:
        """GET 요청을 보내고 JSON 본문을 반환한다."""
        try:
            response = self._session.get(
                self._url(method), params=params, timeout=timeout
            )
        except requests.RequestException as exc:
            # `from None` 으로 체인을 끊는다. requests 예외 메시지에는 요청
            # URL 이 담기고 그 URL 에는 봇 토큰이 들어 있어, 체인이 붙어 있으면
            # logger.exception 이나 미처리 트레이스백으로 토큰이 새어나간다.
            raise TelegramError(
                f"{method} request failed: {type(exc).__name__}"
            ) from None

        return _decode(method, response)

    def _post(self, method: str, body: _PostBody) -> dict:
        """POST 요청을 보내고 JSON 본문을 반환한다."""
        try:
            response = self._session.post(
                self._url(method),
                data=body.data,
                files=body.files,
                timeout=body.timeout,
            )
        except requests.RequestException as exc:
            # 체인을 끊는 이유는 _get 과 같다 (토큰이 담긴 URL 노출 방지)
            raise TelegramError(
                f"{method} request failed: {type(exc).__name__}"
            ) from None

        return _decode(method, response)
