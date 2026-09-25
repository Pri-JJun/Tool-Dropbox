import pytest

from dropbox_tool.rclone import run_rclone


def test_run_rclone_without_rclone_installed_raises_runtime_error(monkeypatch):
    monkeypatch.setenv("PATH", "")  # rclone을 찾을 수 없는 환경

    with pytest.raises(RuntimeError, match="rclone을 찾을 수 없습니다"):
        run_rclone(["listremotes"])
