import logging

import pytest

from private_sync.bot.store import (
    list_dir,
    parent_rel,
    resolve_safe,
    search,
)
from private_sync.errors import StoreError


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "store"
    (root / "SKT 문서" / "sub").mkdir(parents=True)
    (root / "SKT 문서" / "계약서.docx").write_text("c", encoding="utf-8")
    (root / "SKT 문서" / "sub" / "회의록.md").write_text("m", encoding="utf-8")
    (root / "메모").mkdir()
    (root / "메모" / "계약_메모.txt").write_text("n", encoding="utf-8")
    return root


def test_list_root_returns_labels(store):
    entries = list_dir(store, "")

    assert [e.name for e in entries] == ["SKT 문서", "메모"]
    assert all(e.is_dir for e in entries)


def test_list_dir_sorts_directories_before_files(store):
    entries = list_dir(store, "SKT 문서")

    assert [e.name for e in entries] == ["sub", "계약서.docx"]
    assert entries[0].is_dir is True
    assert entries[1].is_dir is False
    assert entries[1].rel == "SKT 문서/계약서.docx"
    assert entries[1].size == 1


def test_search_matches_filename_substring_across_labels(store):
    results = search(store, "계약")

    assert sorted(e.rel for e in results) == [
        "SKT 문서/계약서.docx",
        "메모/계약_메모.txt",
    ]


def test_search_is_case_insensitive(store):
    (store / "메모" / "Report.PDF").write_text("r", encoding="utf-8")

    assert [e.name for e in search(store, "report")] == ["Report.PDF"]


def test_search_respects_limit(store):
    for index in range(10):
        (store / "메모" / f"bulk{index}.txt").write_text("b", encoding="utf-8")

    assert len(search(store, "bulk", limit=3)) == 3


def test_resolve_safe_rejects_parent_traversal(store):
    with pytest.raises(StoreError, match="outside the store"):
        resolve_safe(store, "../../etc/passwd")


def test_resolve_safe_rejects_absolute_path(store):
    with pytest.raises(StoreError, match="outside the store"):
        resolve_safe(store, "/etc/passwd")


def test_resolve_safe_rejects_missing_target(store):
    with pytest.raises(StoreError, match="not found"):
        resolve_safe(store, "메모/없는파일.txt")


def test_resolve_safe_accepts_valid_relative_path(store):
    resolved = resolve_safe(store, "SKT 문서/계약서.docx")

    assert resolved == (store / "SKT 문서" / "계약서.docx").resolve()


def test_parent_rel_walks_up_to_root():
    assert parent_rel("SKT 문서/sub") == "SKT 문서"
    assert parent_rel("SKT 문서") == ""
    assert parent_rel("") is None


def test_list_dir_rejects_non_directory(store):
    with pytest.raises(StoreError, match="not a directory"):
        list_dir(store, "SKT 문서/계약서.docx")


def test_symlink_escaping_store_is_hidden_and_unreadable(store, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET", encoding="utf-8")
    (store / "메모" / "link.txt").symlink_to(outside)

    # 내용은 물론 이름·크기조차 노출되지 않아야 한다
    assert "link.txt" not in [e.name for e in list_dir(store, "메모")]
    assert search(store, "link") == []
    with pytest.raises(StoreError, match="outside the store"):
        resolve_safe(store, "메모/link.txt")


def test_symlink_inside_store_stays_visible(store):
    (store / "메모" / "바로가기.docx").symlink_to(store / "SKT 문서" / "계약서.docx")

    assert "바로가기.docx" in [e.name for e in list_dir(store, "메모")]


def test_symlink_loop_is_skipped_instead_of_raising(store):
    # rsync -a 는 심볼릭 링크를 그대로 옮기므로 자기 참조 링크가 저장소에 들어올 수 있다.
    # Path.resolve() 는 여기서 OSError 가 아니라 RuntimeError 를 던진다.
    loop = store / "메모" / "loop"
    loop.symlink_to(loop)

    names = [e.name for e in list_dir(store, "메모")]

    assert "loop" not in names
    # 루프 옆의 멀쩡한 파일은 계속 보여야 한다
    assert "계약_메모.txt" in names


def test_broken_symlink_does_not_hide_sibling_files(store):
    # 저장소 안을 가리키지만 대상이 없는 링크. 격리 필터는 통과하고 stat 에서 터진다.
    (store / "메모" / "broken.txt").symlink_to(store / "메모" / "없는대상.txt")

    names = [e.name for e in list_dir(store, "메모")]

    assert "broken.txt" not in names
    assert "계약_메모.txt" in names


def test_sibling_directory_sharing_name_prefix_is_rejected(store):
    evil = store.parent / (store.name + "-evil")
    evil.mkdir()
    (evil / "leak.txt").write_text("x", encoding="utf-8")

    # 문자열 접두사 비교였다면 통과했을 경로다
    with pytest.raises(StoreError, match="outside the store"):
        resolve_safe(store, f"../{evil.name}/leak.txt")


def test_search_does_not_report_truncation_when_results_fit(store, caplog):
    # 마지막 순회 항목이 '비일치'여야 오탐 분기를 실제로 밟는다.
    # 정렬상 zz_other.txt 는 두 일치 항목 뒤, 한글 이름 앞에 온다.
    (store / "메모" / "aa_match.txt").write_text("1", encoding="utf-8")
    (store / "메모" / "ab_match.txt").write_text("2", encoding="utf-8")
    (store / "메모" / "zz_other.txt").write_text("3", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        results = search(store, "match", limit=2)

    assert len(results) == 2
    assert "truncated" not in caplog.text


def test_search_reports_truncation_when_matches_exceed_limit(store, caplog):
    for index in range(4):
        (store / "메모" / f"bulk{index}.txt").write_text("b", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        results = search(store, "bulk", limit=2)

    assert len(results) == 2
    assert "truncated" in caplog.text
