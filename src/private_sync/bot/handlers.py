"""텔레그램 명령·콜백을 처리하는 순수 로직.

파일시스템과 네트워크에 직접 접근하지 않는다. 저장소 조회는 Context에 주입된
콜러블을 통해서만 하므로 텔레그램 없이 전체 동작을 테스트할 수 있다.
"""

from __future__ import annotations

import logging
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from private_sync.bot.store import Entry, parent_rel

logger = logging.getLogger(__name__)

_USAGE = "사용법: /start 로 목록 보기, /find <키워드> 로 파일명 검색"
_TOKEN_BYTES = 6


class TokenMap:
    """짧은 토큰과 저장소 상대경로를 잇는다.

    텔레그램 callback_data 는 64바이트 제한이 있어 경로를 직접 담을 수 없다.
    토큰만 노출하므로 경로 조작 시도도 함께 차단된다.
    """

    def __init__(self, limit: int = 500) -> None:
        self._limit = limit
        self._entries: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def put(self, kind: str, rel: str) -> str:
        """(kind, rel)에 대한 토큰을 발급한다."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._entries[token] = (kind, rel)
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return token

    def get(self, token: str) -> tuple[str, str] | None:
        """토큰에 해당하는 (kind, rel)을 반환한다. 없으면 None."""
        return self._entries.get(token)


@dataclass(frozen=True)
class Incoming:
    """파싱된 텔레그램 입력."""

    kind: str
    chat_id: str
    text: str
    message_id: int | None
    callback_id: str | None


@dataclass(frozen=True)
class SendText:
    """텍스트(및 버튼)를 보내거나 기존 메시지를 수정하라는 지시."""

    text: str
    buttons: tuple[tuple[str, str], ...] = ()
    edit: bool = False


@dataclass(frozen=True)
class SendFile:
    """저장소의 파일을 포장해 보내라는 지시."""

    rel: str
    caption: str


Action = SendText | SendFile | None


@dataclass
class Context:
    """핸들러가 쓰는 주입 의존성."""

    chat_id: str
    tokens: TokenMap
    lister: Callable[[str], list[Entry]]
    searcher: Callable[[str], list[Entry]]


def extract(update: dict) -> Incoming | None:
    """텔레그램 update에서 처리 대상만 뽑는다. 대상이 아니면 None."""
    message = update.get("message")
    if isinstance(message, dict) and message.get("text"):
        chat_id = (message.get("chat") or {}).get("id")
        return Incoming(
            kind="message",
            chat_id=str(chat_id),
            text=str(message["text"]),
            message_id=message.get("message_id"),
            callback_id=None,
        )

    callback = update.get("callback_query")
    if isinstance(callback, dict) and callback.get("data"):
        inner = callback.get("message") or {}
        chat_id = (inner.get("chat") or {}).get("id")
        return Incoming(
            kind="callback",
            chat_id=str(chat_id),
            text=str(callback["data"]),
            message_id=inner.get("message_id"),
            callback_id=str(callback.get("id")),
        )

    return None


def format_size(size: int) -> str:
    """사람이 읽을 크기 문자열을 만든다."""
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _entry_button(entry: Entry, tokens: TokenMap) -> tuple[str, str]:
    """항목 하나를 (버튼 라벨, callback_data)로 만든다."""
    if entry.is_dir:
        return (f"📁 {entry.name}", tokens.put("dir", entry.rel))
    label = f"📄 {entry.name} ({format_size(entry.size)})"
    return (label, tokens.put("file", entry.rel))


def _browse(rel: str, ctx: Context, edit: bool) -> SendText:
    """디렉토리 내용을 버튼 목록으로 만든다."""
    entries = ctx.lister(rel)
    buttons: list[tuple[str, str]] = []

    parent = parent_rel(rel)
    if parent is not None:
        buttons.append(("⬆️ 상위", ctx.tokens.put("dir", parent)))
    buttons += [_entry_button(entry, ctx.tokens) for entry in entries]

    title = f"📂 /{rel}" if rel else "📂 저장소"
    if not entries:
        title = f"{title}\n(비어 있습니다)"
    return SendText(text=title, buttons=tuple(buttons), edit=edit)


def _find(keyword: str, ctx: Context) -> SendText:
    """검색 결과를 버튼 목록으로 만든다."""
    if not keyword:
        return SendText(text=_USAGE)

    results = ctx.searcher(keyword)
    if not results:
        return SendText(text=f"'{keyword}' 와 일치하는 파일이 없습니다.")

    buttons = tuple(_entry_button(entry, ctx.tokens) for entry in results)
    return SendText(text=f"🔍 '{keyword}' 검색 결과 {len(results)}건", buttons=buttons)


def _handle_message(incoming: Incoming, ctx: Context) -> Action:
    """텍스트 명령을 처리한다."""
    parts = incoming.text.strip().split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/start", "/ls"):
        return _browse("", ctx, edit=False)
    if command == "/find":
        return _find(argument, ctx)
    return SendText(text=_USAGE)


def _handle_callback(incoming: Incoming, ctx: Context) -> Action:
    """버튼 콜백을 처리한다."""
    resolved = ctx.tokens.get(incoming.text)
    if resolved is None:
        # 봇 재시작이나 LRU 축출로 토큰이 사라진 경우다
        return SendText(text="목록이 만료되었습니다. /start 로 다시 시작해 주세요.")

    kind, rel = resolved
    if kind == "dir":
        return _browse(rel, ctx, edit=True)
    return SendFile(rel=rel, caption=rel.rsplit("/", 1)[-1])


def handle(incoming: Incoming, ctx: Context) -> Action:
    """입력을 인가 검사한 뒤 종류에 맞게 처리한다."""
    if incoming.chat_id != ctx.chat_id:
        logger.warning("Ignoring input from unauthorized chat %s", incoming.chat_id)
        return None

    if incoming.kind == "message":
        return _handle_message(incoming, ctx)
    return _handle_callback(incoming, ctx)
