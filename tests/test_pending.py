from private_sync.agent.pending import PendingItem, PendingStore


def test_pending_items_survive_restart(tmp_path):
    state = tmp_path / "pending.json"
    store = PendingStore(state)
    store.load()
    store.add(PendingItem(label="문서", path="/Users/me/a.md"))
    store.add(PendingItem(label="문서", path="/Users/me/b.md"))

    # 데몬 재시작을 흉내낸다
    reopened = PendingStore(state)
    reopened.load()

    assert reopened.items() == [
        PendingItem(label="문서", path="/Users/me/a.md"),
        PendingItem(label="문서", path="/Users/me/b.md"),
    ]


def test_discard_removes_item_from_disk(tmp_path):
    state = tmp_path / "pending.json"
    store = PendingStore(state)
    store.load()
    item = PendingItem(label="메모", path="/Users/me/n.md")
    store.add(item)

    store.discard(item)

    reopened = PendingStore(state)
    reopened.load()
    assert reopened.items() == []


def test_add_is_idempotent(tmp_path):
    store = PendingStore(tmp_path / "pending.json")
    store.load()
    item = PendingItem(label="메모", path="/Users/me/n.md")

    store.add(item)
    store.add(item)

    assert store.items() == [item]


def test_corrupt_state_file_starts_empty(tmp_path):
    state = tmp_path / "pending.json"
    state.write_text("{ not json", encoding="utf-8")
    store = PendingStore(state)

    store.load()

    assert store.items() == []


def test_missing_state_file_starts_empty(tmp_path):
    store = PendingStore(tmp_path / "never-written.json")

    store.load()

    assert store.items() == []
