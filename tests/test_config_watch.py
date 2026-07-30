import logging
import os

from private_sync.agent.config_watch import ConfigWatcher


def _write(path, text="a"):
    path.write_text(text, encoding="utf-8")
    return path


def test_first_check_reports_no_change(tmp_path):
    watcher = ConfigWatcher(_write(tmp_path / "agent.yaml"))

    assert watcher.changed() is False


def test_change_is_detected_exactly_once(tmp_path):
    config = _write(tmp_path / "agent.yaml")
    watcher = ConfigWatcher(config)

    # mtime 해상도에 기대지 않도록 직접 설정한다
    os.utime(config, (2_000_000_000, 2_000_000_000))

    assert watcher.changed() is True
    # 같은 상태를 반복해서 리로드하면 안 된다
    assert watcher.changed() is False


def test_missing_file_reports_no_change(tmp_path):
    config = _write(tmp_path / "agent.yaml")
    watcher = ConfigWatcher(config)
    config.unlink()

    # 설정이 사라졌다고 이전 설정을 버리지는 않는다
    assert watcher.changed() is False


def test_file_restored_after_deletion_is_detected(tmp_path):
    config = _write(tmp_path / "agent.yaml")
    watcher = ConfigWatcher(config)
    config.unlink()
    watcher.changed()
    _write(config, "b")

    assert watcher.changed() is True


def test_watcher_on_never_existing_file_does_not_raise(tmp_path):
    watcher = ConfigWatcher(tmp_path / "없음.yaml")

    assert watcher.changed() is False


def test_missing_file_warns_only_once(tmp_path, caplog):
    config = _write(tmp_path / "agent.yaml")
    watcher = ConfigWatcher(config)
    config.unlink()

    with caplog.at_level(logging.WARNING):
        watcher.changed()
        watcher.changed()

    # 매 틱마다 같은 경고를 쌓으면 로그가 못 쓰게 된다
    assert caplog.text.count("is unreadable") == 1
