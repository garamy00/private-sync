from private_sync.agent.watcher import (
    Debouncer,
    build_targets,
    is_excluded,
    match_target,
)
from private_sync.config import Source


def test_directory_target_watches_itself_recursively(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = Source(label="문서", paths=(docs,), exclude=())

    targets = build_targets((source,))

    assert len(targets) == 1
    assert targets[0].watch_dir == docs
    assert targets[0].recursive is True


def test_file_target_watches_parent_directory(tmp_path):
    quote = tmp_path / "견적서.xlsx"
    quote.write_text("x", encoding="utf-8")
    source = Source(label="메모", paths=(quote,), exclude=())

    targets = build_targets((source,))

    assert targets[0].watch_dir == tmp_path
    assert targets[0].recursive is False


def test_file_target_ignores_siblings(tmp_path):
    quote = tmp_path / "견적서.xlsx"
    quote.write_text("x", encoding="utf-8")
    other = tmp_path / "무관.txt"
    other.write_text("y", encoding="utf-8")
    targets = build_targets((Source(label="메모", paths=(quote,), exclude=()),))

    assert match_target(quote, targets) is not None
    assert match_target(other, targets) is None


def test_directory_target_matches_nested_file(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    nested = docs / "sub" / "a.md"
    nested.write_text("a", encoding="utf-8")
    targets = build_targets((Source(label="문서", paths=(docs,), exclude=()),))

    assert match_target(nested, targets).label == "문서"


def test_excluded_file_does_not_match(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    junk = docs / ".DS_Store"
    junk.write_text("j", encoding="utf-8")
    targets = build_targets(
        (Source(label="문서", paths=(docs,), exclude=(".DS_Store",)),)
    )

    assert match_target(junk, targets) is None


def test_specific_file_target_survives_broader_exclude(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    quote = work / "견적서.xlsx"
    quote.write_text("q", encoding="utf-8")
    targets = build_targets(
        (
            Source(label="프로젝트", paths=(work,), exclude=("*.xlsx",)),
            Source(label="견적서", paths=(quote,), exclude=()),
        )
    )

    # 앞선 대상의 exclude가 뒤에 개별 등록된 파일을 가려서는 안 된다
    assert match_target(quote, targets).label == "견적서"


def test_is_excluded_matches_glob_on_any_part():
    assert is_excluded(("work", "~$보고서.docx"), ("~$*",)) is True
    assert is_excluded(("work", "보고서.docx"), ("~$*",)) is False


def test_is_excluded_matches_directory_pattern_only_on_parents():
    assert is_excluded((".git", "config"), (".git/",)) is True
    # 파일 자신의 이름과는 매칭하지 않는다
    assert is_excluded(("work", ".git"), (".git/",)) is False


def test_debouncer_collapses_repeated_touches():
    debouncer = Debouncer(delay=3.0)

    debouncer.touch("a", now=100.0)
    debouncer.touch("a", now=101.0)
    debouncer.touch("a", now=102.0)

    # 마지막 touch 기준으로 3초를 기다린다
    assert debouncer.due(now=104.0) == []
    assert debouncer.due(now=105.0) == ["a"]
    # 한 번 꺼낸 키는 다시 나오지 않는다
    assert debouncer.due(now=200.0) == []


def test_debouncer_releases_independent_keys():
    debouncer = Debouncer(delay=1.0)
    debouncer.touch("a", now=10.0)
    debouncer.touch("b", now=20.0)

    assert debouncer.due(now=11.0) == ["a"]
    assert debouncer.pending_count() == 1
    assert debouncer.due(now=21.0) == ["b"]
