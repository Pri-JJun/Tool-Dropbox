"""rclone 호출 및 Dropbox 경로/목록 관련 공통 함수."""

import json
import os
import subprocess
import sys
import tempfile


def run_rclone(args, capture=True):
    """rclone 명령을 실행하고 stdout을 반환. 실패 시 RuntimeError 발생."""
    cmd = ["rclone"] + args
    # backend 옵션은 flag 대신 환경변수로 전달 (deletefile 등에서 unknown flag 방지)
    # 영구삭제가 아니라 항상 Dropbox 휴지통으로 이동하도록 강제 (설정과 무관하게 보장)
    env = dict(os.environ)
    env["RCLONE_DROPBOX_PERMANENT_DELETE"] = "false"
    # 한글/일본어 등 비-ASCII 인자·출력이 로케일과 무관하게 처리되도록 UTF-8 고정
    try:
        result = subprocess.run(cmd, capture_output=capture, encoding="utf-8", env=env)
    except FileNotFoundError:
        raise RuntimeError(
            "rclone을 찾을 수 없습니다. 설치 후 Dropbox remote를 설정하세요.\n"
            "  brew install rclone\n"
            "  rclone config"
        ) from None
    if result.returncode != 0:
        raise RuntimeError(f"rclone 명령 실패: {' '.join(cmd)}\n{(result.stderr or '').strip()}")
    return result.stdout


def ensure_remote(remote):
    """remote가 rclone에 설정되어 있는지 확인. 없으면 오류 출력 후 종료(exit 1)."""
    remotes = run_rclone(["listremotes"]).split()
    if f"{remote}:" not in remotes:
        print(
            f"[오류] '{remote}' remote를 찾을 수 없습니다. "
            f"설정된 remote: {', '.join(remotes) or '없음'}",
            file=sys.stderr,
        )
        sys.exit(1)


def remote_path(remote, path):
    """remote와 경로를 rclone 형식(remote:path)으로 결합."""
    path = path.strip("/")
    return f"{remote}:{path}" if path else f"{remote}:"


def join_path(folder, name):
    """폴더 경로와 파일명을 안전하게 결합."""
    folder = folder.strip("/")
    return f"{folder}/{name}" if folder else name


def list_folders(remote, root):
    """root 이하 모든 하위 폴더 경로를 재귀 조회 (root 자신 포함)."""
    out = run_rclone(["lsf", remote_path(remote, root), "--dirs-only", "-R"])
    folders = [root]  # root 자신도 하나의 폴더로 처리
    for line in out.splitlines():
        sub = line.strip().rstrip("/")
        if sub:
            folders.append(join_path(root, sub))
    return folders


def list_files(remote, folder):
    """해당 폴더의 파일 목록(비재귀)을 hash 포함하여 조회."""
    out = run_rclone(["lsjson", remote_path(remote, folder), "--hash"])
    entries = json.loads(out)
    return [e for e in entries if not e.get("IsDir", False)]


def has_subfolders(remote, folder):
    """지정 폴더 바로 아래에 서브폴더가 있으면 True."""
    out = run_rclone(["lsf", remote_path(remote, folder), "--dirs-only"])
    return bool(out.strip())


def get_hash(entry):
    """entry에서 dropbox hash를 우선 추출. 없으면 사용 가능한 첫 hash."""
    hashes = entry.get("Hashes") or {}
    if hashes.get("dropbox"):
        return hashes["dropbox"]
    for v in hashes.values():
        if v:
            return v
    return None


def batch_delete(remote, deletes, tpslimit):
    """전역 삭제 대상을 임시 파일 목록으로 만들어 한 번에 삭제.

    - rclone delete --files-from <임시파일> 로 rclone 1회 호출
    - 경로는 remote 루트 기준 상대경로로 UTF-8 기록
    - 휴지통 이동은 run_rclone의 환경변수로 보장
    - 작업 후 임시 파일 삭제 (오류가 나도 finally로 정리)
    """
    if not deletes:
        return
    extra = ["--tpslimit", str(tpslimit)] if tpslimit else []
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", prefix="dropbox_del_", delete=False
    )
    try:
        for folder, entry in deletes:
            tmp.write(join_path(folder, entry["Name"]) + "\n")
        tmp.close()
        # base는 remote 루트, files-from 경로는 루트 기준 상대경로
        run_rclone(["delete", remote_path(remote, ""), "--files-from", tmp.name] + extra)
    finally:
        os.unlink(tmp.name)
