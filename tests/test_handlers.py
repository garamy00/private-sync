from private_sync.bot.handlers import (
    Context,
    Incoming,
    SendFile,
    SendText,
    TokenMap,
    extract,
    handle,
)
from private_sync.bot.store import Entry

ROOT = [
    Entry(name="업무 문서", rel="업무 문서", is_dir=True, size=0),
    Entry(name="메모", rel="메모", is_dir=True, size=0),
]
WORK_DOCS = [
    Entry(name="sub", rel="업무 문서/sub", is_dir=True, size=0),
    Entry(name="계약서.docx", rel="업무 문서/계약서.docx", is_dir=False, size=2048),
]


def _ctx(chat_id="123", listing=None, results=None):
    tree = {"": ROOT, "업무 문서": WORK_DOCS} if listing is None else listing
    return Context(
        chat_id=chat_id,
        tokens=TokenMap(),
        lister=lambda rel: tree.get(rel, []),
        searcher=lambda kw: results or [],
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


def test_token_map_evicts_oldest_beyond_limit():
    tokens = TokenMap(limit=2)
    first = tokens.put("file", "a")
    tokens.put("file", "b")
    tokens.put("file", "c")

    assert tokens.get(first) is None
    assert tokens.get(tokens.put("file", "d")) == ("file", "d")


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
