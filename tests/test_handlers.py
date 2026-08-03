from private_sync.bot.handlers import (
    _PAGE_SIZE,
    Context,
    Incoming,
    SendFile,
    SendFolder,
    SendText,
    TokenMap,
    extract,
    handle,
)
from private_sync.bot.store import DirStats, Entry

ROOT = [
    Entry(name="업무 문서", rel="업무 문서", is_dir=True, size=0),
    Entry(name="메모", rel="메모", is_dir=True, size=0),
]
WORK_DOCS = [
    Entry(name="sub", rel="업무 문서/sub", is_dir=True, size=0),
    Entry(name="계약서.docx", rel="업무 문서/계약서.docx", is_dir=False, size=2048),
]


def _ctx(chat_id="123", listing=None, results=None, stats=None):
    tree = {"": ROOT, "업무 문서": WORK_DOCS} if listing is None else listing
    return Context(
        chat_id=chat_id,
        tokens=TokenMap(),
        lister=lambda rel: tree.get(rel, []),
        searcher=lambda kw: results or [],
        stats=stats or (lambda rel: DirStats(files=3, total_bytes=1024)),
    )


def _message(text, chat_id="123"):
    return Incoming(
        kind="message", chat_id=chat_id, text=text, message_id=1, callback_id=None
    )


def test_start_lists_root_labels():
    action = handle(_message("/start"), _ctx())

    assert isinstance(action, SendText)
    assert [label for label, _ in action.buttons] == ["📁 업무 문서", "📁 메모"]


def test_unauthorized_chat_is_ignored():
    action = handle(_message("/start", chat_id="999"), _ctx(chat_id="123"))

    assert action is None


def test_unknown_command_returns_usage():
    action = handle(_message("/whatever"), _ctx())

    assert isinstance(action, SendText)
    assert "/start" in action.text
    assert action.buttons == ()


def test_whitespace_only_message_returns_usage():
    # 공백만 보낸 메시지로 봇이 죽으면 안 된다
    action = handle(_message("   "), _ctx())

    assert isinstance(action, SendText)
    assert "/start" in action.text


def test_directory_button_lists_children_with_up_button():
    ctx = _ctx()
    start = handle(_message("/start"), ctx)
    work_token = {label: data for label, data in start.buttons}["📁 업무 문서"]

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=work_token,
            message_id=5,
            callback_id="cb1",
        ),
        ctx,
    )

    assert isinstance(action, SendText)
    assert action.edit is True
    labels = [label for label, _ in action.buttons]
    assert labels[0] == "⬆️ 상위"
    assert "📁 sub" in labels
    assert "📄 계약서.docx (2.0 KB)" in labels


def test_file_button_requests_send():
    ctx = _ctx()
    token = ctx.tokens.put("file", "업무 문서/계약서.docx")

    action = handle(
        Incoming(
            kind="callback", chat_id="123", text=token, message_id=5, callback_id="cb"
        ),
        ctx,
    )

    assert action == SendFile(rel="업무 문서/계약서.docx", caption="계약서.docx")


def test_expired_token_is_reported():
    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text="nosuchtoken",
            message_id=5,
            callback_id="cb",
        ),
        _ctx(),
    )

    assert isinstance(action, SendText)
    assert "만료" in action.text


def test_find_lists_matches():
    results = [
        Entry(name="계약서.docx", rel="업무 문서/계약서.docx", is_dir=False, size=10)
    ]
    action = handle(_message("/find 계약"), _ctx(results=results))

    assert [label for label, _ in action.buttons] == ["📄 계약서.docx (10 B)"]


def test_find_without_keyword_returns_usage():
    action = handle(_message("/find"), _ctx())

    assert "/find" in action.text
    assert action.buttons == ()


def test_find_with_no_results_says_so():
    action = handle(_message("/find 없는것"), _ctx(results=[]))

    assert "없습니다" in action.text
    assert action.buttons == ()


def _many(count):
    return [
        Entry(name=f"{i:03d}.mp3", rel=f"음악/{i:03d}.mp3", is_dir=False, size=10)
        for i in range(count)
    ]


def test_large_directory_is_split_into_pages():
    ctx = _ctx(listing={"음악": _many(45)})

    first = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    labels = [label for label, _ in first.buttons]
    # 한 화면에 45개를 다 실으면 텔레그램이 400 으로 거부한다
    assert sum(1 for label in labels if label.startswith("📄")) == _PAGE_SIZE
    assert "1/3" in labels
    assert "다음 ▶" in labels
    assert "◀ 이전" not in labels


def test_middle_page_has_both_arrows_and_parent():
    ctx = _ctx(listing={"음악": _many(45)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악", 1),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    labels = [label for label, _ in action.buttons]
    assert "◀ 이전" in labels
    assert "다음 ▶" in labels
    assert "2/3" in labels
    # 3페이지에서도 되돌아갈 수 있어야 한다
    assert labels[0] == "⬆️ 상위"


def test_last_page_holds_the_remainder():
    ctx = _ctx(listing={"음악": _many(45)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악", 2),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    labels = [label for label, _ in action.buttons]
    assert sum(1 for label in labels if label.startswith("📄")) == 5
    assert "다음 ▶" not in labels


def test_page_beyond_the_end_is_clamped():
    ctx = _ctx(listing={"음악": _many(45)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악", 99),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert "3/3" in [label for label, _ in action.buttons]


def test_exact_multiple_of_page_size_has_no_empty_page():
    ctx = _ctx(listing={"음악": _many(40)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악", 0),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert "1/2" in [label for label, _ in action.buttons]


def test_small_directory_has_no_pager():
    action = handle(_message("/start"), _ctx())

    labels = [label for label, _ in action.buttons]
    # 페이지가 하나뿐이면 n/N 표시도 화살표도 없어야 한다
    assert not any(label[0].isdigit() for label in labels)
    assert "다음 ▶" not in labels


def test_page_number_never_reaches_callback_data():
    ctx = _ctx(listing={"음악": _many(45)})

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    # 경로도 페이지도 callback_data 로 새면 안 된다
    for _label, data in action.buttons:
        assert "음악" not in data
        assert data.isascii()


def test_directory_view_offers_a_folder_download():
    ctx = _ctx()

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("dir", "업무 문서"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert any(label.startswith("📦") for label, _ in action.buttons)


def test_root_view_has_no_folder_download():
    action = handle(_message("/start"), _ctx())

    # 저장소 전체를 통째로 받는 것은 의도한 동작이 아니다
    assert not any(label.startswith("📦") for label, _ in action.buttons)


def test_folder_download_asks_before_starting():
    ctx = _ctx(stats=lambda rel: DirStats(files=100, total_bytes=843 * 1024 * 1024))

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("zipask", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert isinstance(action, SendText)
    assert "843.0 MB" in action.text
    assert "100" in action.text
    labels = [label for label, _ in action.buttons]
    assert "받기" in labels
    assert "취소" in labels


def test_confirming_starts_the_folder_download():
    ctx = _ctx()

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("zipgo", "음악"),
            message_id=5,
            callback_id="cb",
        ),
        ctx,
    )

    assert action == SendFolder(rel="음악")


def test_cancelling_returns_to_the_listing():
    ctx = _ctx()

    # 취소 버튼이 실제로 물고 있는 토큰을 확인 화면에서 뽑아와야 배선이 끊겨도 잡힌다
    confirm = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=ctx.tokens.put("zipask", "업무 문서"),
            message_id=5,
            callback_id="cb1",
        ),
        ctx,
    )
    cancel_token = dict(confirm.buttons)["취소"]

    action = handle(
        Incoming(
            kind="callback",
            chat_id="123",
            text=cancel_token,
            message_id=5,
            callback_id="cb2",
        ),
        ctx,
    )

    assert isinstance(action, SendText)
    assert action.edit is True


def test_token_map_evicts_oldest_beyond_limit():
    tokens = TokenMap(limit=2)
    first = tokens.put("file", "a")
    tokens.put("file", "b")
    tokens.put("file", "c")

    assert tokens.get(first) is None
    assert tokens.get(tokens.put("file", "d")) == ("file", "d", 0)


def test_extract_reads_message_and_callback():
    message = extract(
        {"message": {"text": "/start", "chat": {"id": 123}, "message_id": 7}}
    )
    assert message == Incoming(
        kind="message", chat_id="123", text="/start", message_id=7, callback_id=None
    )

    callback = extract(
        {
            "callback_query": {
                "id": "cb9",
                "data": "tok",
                "message": {"chat": {"id": 123}, "message_id": 8},
            }
        }
    )
    assert callback == Incoming(
        kind="callback", chat_id="123", text="tok", message_id=8, callback_id="cb9"
    )

    assert extract({"edited_message": {}}) is None
