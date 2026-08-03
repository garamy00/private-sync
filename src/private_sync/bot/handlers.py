"""텔레그램 명령·콜백을 처리하는 순수 로직.

파일시스템과 네트워크에 직접 접근하지 않는다. 저장소 조회는 Context에 주입된
콜러블을 통해서만 하므로 텔레그램 없이 전체 동작을 테스트할 수 있다.
"""

from __future__ import annotations

import logging
import math
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from private_sync.bot.store import DirStats, Entry, parent_rel

logger = logging.getLogger(__name__)

_USAGE = "사용법: /start 로 목록 보기, /find <키워드> 로 파일명 검색"
_TOKEN_BYTES = 6
# 한 화면에 담을 항목 수. 텔레그램은 버튼이 너무 많은 reply_markup 을 400 으로
# 거부하며, 그 실패는 사용자에게 "눌러도 반응 없음" 으로 보인다.
_PAGE_SIZE = 20


class TokenMap:
    """짧은 토큰과 저장소 상대경로를 잇는다.

    텔레그램 callback_data 는 64바이트 제한이 있어 경로를 직접 담을 수 없다.
    토큰만 노출하므로 경로 조작 시도도 함께 차단된다.

    한도를 넘으면 가장 먼저 발급된 토큰부터 버린다(입력 순서 FIFO). 조회는
    순서를 갱신하지 않으므로 LRU 가 아니다.
    """

    def __init__(self, limit: int = 500) -> None:
        self._limit = limit
        self._entries: OrderedDict[str, tuple[str, str, int]] = OrderedDict()

    def put(self, kind: str, rel: str, page: int = 0) -> str:
        """(kind, rel, page)에 대한 토큰을 발급한다."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._entries[token] = (kind, rel, page)
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return token

    def get(self, token: str) -> tuple[str, str, int] | None:
        """토큰에 해당하는 (kind, rel, page)를 반환한다. 없으면 None."""
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


@dataclass(frozen=True)
class SendFolder:
    """폴더를 통째로 압축해 보내라는 지시."""

    rel: str


Action = SendText | SendFile | SendFolder | None


@dataclass
class Context:
    """핸들러가 쓰는 주입 의존성."""

    chat_id: str
    tokens: TokenMap
    lister: Callable[[str], list[Entry]]
    searcher: Callable[[str], list[Entry]]
    stats: Callable[[str], DirStats]
    max_part_bytes: int


@dataclass(frozen=True)
class BrowseView:
    """탐색 화면 한 장을 가리키는 좌표."""

    rel: str
    page: int = 0
    edit: bool = False


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


@dataclass(frozen=True)
class PageState:
    """현재 페이지와 전체 페이지 수."""

    page: int
    total: int


def _pager_buttons(ctx: Context, rel: str, state: PageState) -> list[tuple[str, str]]:
    """페이지 이동 버튼을 만든다. 한 페이지뿐이면 아무것도 만들지 않는다."""
    if state.total <= 1:
        return []

    buttons: list[tuple[str, str]] = []
    if state.page > 0:
        buttons.append(("◀ 이전", ctx.tokens.put("dir", rel, state.page - 1)))
    buttons.append(
        (f"{state.page + 1}/{state.total}", ctx.tokens.put("dir", rel, state.page))
    )
    if state.page < state.total - 1:
        buttons.append(("다음 ▶", ctx.tokens.put("dir", rel, state.page + 1)))
    return buttons


def _browse(ctx: Context, view: BrowseView) -> SendText:
    """디렉토리 내용을 한 페이지 분량의 버튼 목록으로 만든다."""
    entries = ctx.lister(view.rel)
    total_pages = max(1, (len(entries) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(view.page, total_pages - 1))
    start = page * _PAGE_SIZE

    buttons: list[tuple[str, str]] = []
    parent = parent_rel(view.rel)
    if parent is not None:
        # 몇 페이지에 있든 되돌아갈 수 있어야 한다
        buttons.append(("⬆️ 상위", ctx.tokens.put("dir", parent)))
    if view.rel:
        # 저장소 전체를 통째로 받는 것은 의도한 동작이 아니므로 루트에서는 넣지 않는다
        buttons.append(("📦 이 폴더 통째로 받기", ctx.tokens.put("zipask", view.rel)))

    buttons += [
        _entry_button(entry, ctx.tokens)
        for entry in entries[start : start + _PAGE_SIZE]
    ]
    buttons += _pager_buttons(ctx, view.rel, PageState(page, total_pages))

    title = f"📂 /{view.rel}" if view.rel else "📂 저장소"
    if not entries:
        title = f"{title}\n(비어 있습니다)"
    return SendText(text=title, buttons=tuple(buttons), edit=view.edit)


def _confirm_folder(ctx: Context, rel: str) -> SendText:
    """폴더 압축 전에 크기를 보여주고 확인을 받는다.

    843MB 짜리 전송을 실수로 시작하면 되돌릴 수 없다.
    """
    stats = ctx.stats(rel)
    # 원본 바이트 기준 근사치다. 압축이 되면 보통 이보다 적게 나가지만, 경계
    # 부근에서는 zip 헤더·AES 암호화 오버헤드로 이보다 많이 나갈 수도 있다.
    # 실제 파트 수는 압축해보기 전에는 알 수 없으므로 상한("최대")이라고
    # 단정하지 않는다 — 놀라움을 막으려는 화면이 이해 못하는 방향으로 틀리면
    # 안 된다.
    estimated_parts = max(1, math.ceil(stats.total_bytes / ctx.max_part_bytes))
    return SendText(
        text=(
            f"📦 /{rel}\n"
            f"파일 {stats.files}개, {format_size(stats.total_bytes)}\n"
            f"예상 파트 수 약 {estimated_parts}개\n"
            "압축해서 보낼까요?"
        ),
        buttons=(
            ("받기", ctx.tokens.put("zipgo", rel)),
            ("취소", ctx.tokens.put("dir", rel)),
        ),
        edit=True,
    )


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
    if not parts:
        # 공백만 있는 메시지도 텔레그램에서는 유효한 text 로 도착한다
        return SendText(text=_USAGE)

    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/start", "/ls"):
        return _browse(ctx, BrowseView(rel=""))
    if command == "/find":
        return _find(argument, ctx)
    return SendText(text=_USAGE)


def _handle_callback(incoming: Incoming, ctx: Context) -> Action:
    """버튼 콜백을 처리한다."""
    resolved = ctx.tokens.get(incoming.text)
    if resolved is None:
        # 봇 재시작이나 오래된 토큰 축출로 토큰이 사라진 경우다
        return SendText(text="목록이 만료되었습니다. /start 로 다시 시작해 주세요.")

    kind, rel, page = resolved
    if kind == "dir":
        return _browse(ctx, BrowseView(rel=rel, page=page, edit=True))
    if kind == "zipask":
        return _confirm_folder(ctx, rel)
    if kind == "zipgo":
        return SendFolder(rel=rel)
    return SendFile(rel=rel, caption=rel.rsplit("/", 1)[-1])


def handle(incoming: Incoming, ctx: Context) -> Action:
    """입력을 인가 검사한 뒤 종류에 맞게 처리한다."""
    if incoming.chat_id != ctx.chat_id:
        logger.warning("Ignoring input from unauthorized chat %s", incoming.chat_id)
        return None

    if incoming.kind == "message":
        return _handle_message(incoming, ctx)
    return _handle_callback(incoming, ctx)
