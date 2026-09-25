#!/usr/bin/env python3
"""
dropbox_tool.py

rclone을 이용해 Dropbox의 폴더별 중복 파일을 제거하는 스크립트.
--rename 지정 시, 중복 제거 후 남은 파일 이름을 1번부터 순차 부여한다.
cleanup-similar는 단일 폴더 내 이미지를 지각적 유사도(pHash)로 비교해 제거한다.

전제:
  - rclone이 설치되어 있고, Dropbox remote가 이미 설정되어 있어야 함.
  - 중복 판별은 파일 내용의 hash(DropboxHash) 기준.
    (이름이 달라도 내용이 같으면 중복으로 간주)
  - 삭제는 영구삭제가 아니라 Dropbox '휴지통(삭제된 파일)'으로 이동.
  - 삭제는 전 폴더 대상을 모아 1회 일괄 처리(임시 파일 + --files-from).
  - cleanup-similar는 Pillow, imagehash 패키지가 필요.

사용 예:
  python3 dropbox_tool.py cleanup -f 사진 --dry-run
  python3 dropbox_tool.py cleanup -f 사진 --rename
  python3 dropbox_tool.py cleanup-similar -f 사진/여행 --dry-run
  python3 dropbox_tool.py cleanup-similar -f 사진/여행 --threshold 85
  python3 dropbox_tool.py cleanup-similar -f 사진/여행 --full
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import defaultdict


def run_rclone(args, capture=True):
    """rclone 명령을 실행하고 stdout을 반환. 실패 시 RuntimeError 발생."""
    cmd = ["rclone"] + args
    # backend 옵션은 flag 대신 환경변수로 전달 (deletefile 등에서 unknown flag 방지)
    # 영구삭제가 아니라 항상 Dropbox 휴지통으로 이동하도록 강제 (설정과 무관하게 보장)
    env = dict(os.environ)
    env["RCLONE_DROPBOX_PERMANENT_DELETE"] = "false"
    # 한글/일본어 등 비-ASCII 인자·출력이 로케일과 무관하게 처리되도록 UTF-8 고정
    result = subprocess.run(cmd, capture_output=capture, encoding="utf-8", env=env)
    if result.returncode != 0:
        raise RuntimeError(
            f"rclone 명령 실패: {' '.join(cmd)}\n{(result.stderr or '').strip()}"
        )
    return result.stdout


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


def get_hash(entry):
    """entry에서 dropbox hash를 우선 추출. 없으면 사용 가능한 첫 hash."""
    hashes = entry.get("Hashes") or {}
    if hashes.get("dropbox"):
        return hashes["dropbox"]
    for v in hashes.values():
        if v:
            return v
    return None


def plan_moves(name_final_pairs, existing_names):
    """이름변경을 최소 이동 횟수로 수행할 순서를 계산.

    - 충돌이 없는 파일: 원본 -> 최종명 (1회 이동)
    - 충돌(사이클)이 있는 파일만 임시명을 거침 (2회 이동)

    name_final_pairs : [(현재명, 최종명), ...]
    existing_names   : 폴더에 현재 존재하는 모든 보존 파일명 집합
    반환             : 실행 순서대로 정렬된 [(from, to), ...]
    """
    pending = {cur: fin for cur, fin in name_final_pairs if cur != fin}
    occupied = set(existing_names)
    ordered = []
    tmp_counter = 0

    while pending:
        progressed = False
        # 목적지가 비어 있는 이동은 즉시 수행 (1회 이동)
        for src in list(pending):
            fin = pending[src]
            if fin not in occupied:
                ordered.append((src, fin))
                occupied.discard(src)
                occupied.add(fin)
                del pending[src]
                progressed = True
        if not pending:
            break
        # 남았는데 하나도 못 옮겼다면 사이클 → 하나를 임시명으로 빼서 해소
        if not progressed:
            src = next(iter(pending))
            fin = pending.pop(src)
            while True:
                tmp = f".dedup_tmp_{tmp_counter}"
                tmp_counter += 1
                if tmp not in occupied:
                    break
            ordered.append((src, tmp))
            occupied.discard(src)
            occupied.add(tmp)
            pending[tmp] = fin  # 임시명 -> 최종명은 이후 라운드에서 처리
    return ordered


def build_folder_plan(remote, folder, rename):
    """폴더별 (삭제 목록, 논리적 이름변경, 실행 이동순서) 계획 수립.

    rename=True일 때만 보존 파일에 순차 이름을 부여(0-padding 자동 적용).
    rename=False면 이름변경 계획은 비어 있고 중복 삭제만 대상.

    반환: (to_delete, logical_renames, ordered_moves, total, kept, no_hash)
      - to_delete      : [(폴더, entry)]  (전역 일괄 삭제용, 폴더 경로 포함)
      - logical_renames: [(원본명, 최종명)]  (사용자 표시용)
      - ordered_moves  : [(from, to)]        (실제 실행 순서)
    """
    files = list_files(remote, folder)

    # 1) hash 기준 그룹핑 → 중복 판별
    by_hash = defaultdict(list)
    no_hash = []
    for f in files:
        h = get_hash(f)
        (no_hash if h is None else by_hash[h]).append(f)

    to_delete, kept = [], []
    for group in by_hash.values():
        group_sorted = sorted(group, key=lambda x: x["Name"])
        kept.append(group_sorted[0])                       # 첫 파일만 보존
        for dup in group_sorted[1:]:                       # 나머지는 삭제(휴지통 이동)
            to_delete.append((folder, dup))
    kept.extend(no_hash)                                   # hash 없는 파일은 판별 불가 → 보존

    # 2) 이름변경 계획 (--rename 지정 시에만, 0-padding 자동)
    logical_renames, ordered_moves = [], []
    if rename:
        kept_sorted = sorted(kept, key=lambda x: x["Name"])
        width = len(str(len(kept_sorted)))                 # 자릿수 자동 계산
        pairs = []                                         # (현재명, 최종명)
        for i, f in enumerate(kept_sorted, start=1):
            ext = os.path.splitext(f["Name"])[1]
            pairs.append((f["Name"], f"{str(i).zfill(width)}{ext}"))
        logical_renames = [(cur, fin) for cur, fin in pairs if cur != fin]
        existing_names = {f["Name"] for f in kept_sorted}
        ordered_moves = plan_moves(pairs, existing_names)

    return to_delete, logical_renames, ordered_moves, len(files), len(kept), len(no_hash)


def print_folder_renames(folder, logical_renames):
    """--rename 시 폴더별 이름변경 계획 출력 (변경 있는 폴더만)."""
    if not logical_renames:
        return
    label = folder if folder else "(루트)"
    print(f"\n[폴더] {label}")
    for src, final in logical_renames:
        print(f"    - 이름변경 예정: {src} -> {final}")


def print_delete_list(deletes):
    """전역 삭제 대상 목록을 전체 경로 기준으로 한데 모아 출력."""
    if not deletes:
        return
    print("\n[삭제 예정 목록] (휴지통 이동)")
    for folder, entry in deletes:
        print(f"    - {join_path(folder, entry['Name'])}")


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
        mode="w", encoding="utf-8", suffix=".txt",
        prefix="dropbox_del_", delete=False
    )
    try:
        for folder, entry in deletes:
            tmp.write(join_path(folder, entry["Name"]) + "\n")
        tmp.close()
        # base는 remote 루트, files-from 경로는 루트 기준 상대경로
        run_rclone(["delete", remote_path(remote, ""),
                    "--files-from", tmp.name] + extra)
    finally:
        os.unlink(tmp.name)


def rename_folder(remote, folder, ordered_moves, tpslimit):
    """폴더별 이름변경 수행 (계산된 최소 이동 순서대로)."""
    extra = ["--tpslimit", str(tpslimit)] if tpslimit else []
    for src, dst in ordered_moves:
        s = remote_path(remote, join_path(folder, src))
        d = remote_path(remote, join_path(folder, dst))
        run_rclone(["moveto", s, d] + extra)
        print(f"  이름변경 완료: {src} -> {dst}")


def cmd_cleanup(args):
    """cleanup 명령: 폴더별 중복 제거(전역 일괄, 휴지통 이동) + 선택적 순차 이름부여.

    - 중복 검사: 폴더 내부 한정 (폴더별)
    - 삭제: 전 폴더 대상을 모아 1회 일괄 (임시 파일 + --files-from)
    - 이름변경: --rename 지정 시에만, 폴더별 순차 부여(0-padding 자동)
    """
    remote = args.remote.rstrip(":")   # 뒤에 붙은 ':' 제거
    # 한글/일본어 경로의 NFC/NFD 불일치 방지를 위해 조합형(NFC)으로 정규화
    root = unicodedata.normalize("NFC", args.f)

    remotes = run_rclone(["listremotes"]).split()
    if f"{remote}:" not in remotes:
        print(f"[오류] '{remote}' remote를 찾을 수 없습니다. "
              f"설정된 remote: {', '.join(remotes) or '없음'}", file=sys.stderr)
        sys.exit(1)

    folders = list_folders(remote, root)

    all_deletes = []       # [(folder, entry)]  전역 삭제 대상
    rename_plans = []      # [(folder, ordered_moves)]
    total_files = total_ren = total_moves = total_hashless = 0

    for folder in folders:
        to_delete, logical_renames, ordered_moves, total, _kept, no_hash = build_folder_plan(
            remote, folder, args.rename
        )
        if total == 0:
            continue
        total_files += total
        total_hashless += no_hash
        all_deletes.extend(to_delete)
        if ordered_moves:
            rename_plans.append((folder, ordered_moves))
        total_ren += len(logical_renames)
        total_moves += len(ordered_moves)
        print_folder_renames(folder, logical_renames)

    # 삭제 목록은 끝에 한데 모아 출력
    print_delete_list(all_deletes)

    # 요약
    summary = f"\n===== 요약: 전체 {total_files}개 / 삭제(휴지통) {len(all_deletes)}개"
    if args.rename:
        summary += f" / 이름변경 {total_ren}개 (실제 이동 {total_moves}회)"
    if total_hashless:
        summary += f" / hash없음 {total_hashless}개(보존)"
    summary += " ====="
    print(summary)

    if args.dry_run:
        print("[DRY-RUN] 실제 변경은 수행하지 않았습니다.")
        return

    if not all_deletes and total_moves == 0:
        print("변경할 항목이 없습니다.")
        return

    # 1) 전역 일괄 삭제 (임시 파일 + --files-from, rclone 1회)
    if all_deletes:
        print("\n[처리 중] 중복 삭제 (일괄)")
        batch_delete(remote, all_deletes, args.tpslimit)
        print(f"  휴지통 이동 완료: {len(all_deletes)}개")

    # 2) 이름변경 (--rename 시 폴더별)
    for folder, ordered_moves in rename_plans:
        label = folder if folder else "(루트)"
        print(f"\n[이름변경] {label}")
        rename_folder(remote, folder, ordered_moves, args.tpslimit)

    print("\n완료되었습니다. (삭제 파일은 Dropbox 휴지통에서 복구 가능)")


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".heic"}


def is_image(name):
    """확장자 기준 이미지 파일 판정."""
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def human_size(n):
    """바이트 수를 사람이 읽기 좋은 단위로."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def has_subfolders(remote, folder):
    """지정 폴더 바로 아래에 서브폴더가 있으면 True."""
    out = run_rclone(["lsf", remote_path(remote, folder), "--dirs-only"])
    return bool(out.strip())


def download_images(remote, folder, image_entries, tmpdir, tpslimit):
    """이미지 파일만 임시 디렉토리로 원본 다운로드.

    폴더 내 파일명이 유일하므로 --files-from에 파일명만 기록하면 충분.
    (rclone 썸네일 API 미지원으로 원본 다운로드가 불가피)
    """
    extra = ["--tpslimit", str(tpslimit)] if tpslimit else []
    listf = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt",
        prefix="dropbox_img_", delete=False
    )
    try:
        for e in image_entries:
            listf.write(e["Name"] + "\n")
        listf.close()
        run_rclone(["copy", remote_path(remote, folder), tmpdir,
                    "--files-from", listf.name] + extra)
    finally:
        os.unlink(listf.name)


def compute_image_hashes(folder, image_entries, tmpdir, full):
    """다운로드된 이미지의 pHash와 해상도 계산.

    full=True면 hash_size=16(256비트, 더 정밀), 아니면 8(64비트).
    반환: [{folder, name, size, hash, dims}]
    """
    from PIL import Image
    import imagehash

    hash_size = 16 if full else 8
    files = []
    for e in image_entries:
        path = os.path.join(tmpdir, e["Name"])
        try:
            with Image.open(path) as im:
                im.load()
                dims = f"{im.width}x{im.height}"
                h = imagehash.phash(im, hash_size=hash_size)
        except Exception as ex:
            print(f"  [건너뜀] 이미지 열기 실패: {e['Name']} ({ex})", file=sys.stderr)
            continue
        files.append({
            "folder": folder, "name": e["Name"],
            "size": int(e.get("Size", 0)), "hash": h, "dims": dims,
        })
    return files


def group_by_keeper(files, maxdist, bits):
    """보존 기준 직접 비교(b안)로 유사 그룹 구성.

    - 용량 내림차순(동률 시 파일명 오름차순)으로 keeper 선정
    - keeper와 직접 Hamming distance <= maxdist인 파일만 삭제 대상
    - 남은 파일에서 다시 최대 용량을 keeper로 반복 (결정적)

    반환: (groups, singles)
      groups : [(keeper, [(dup_file, 유사도%), ...])]
      singles: 유사 대상이 없던 파일 수
    """
    pending = sorted(files, key=lambda x: (-x["size"], x["name"]))
    groups = []
    singles = 0
    while pending:
        keeper = pending.pop(0)
        dups, rest = [], []
        for f in pending:
            dist = keeper["hash"] - f["hash"]
            if dist <= maxdist:
                dups.append((f, (1 - dist / bits) * 100))
            else:
                rest.append(f)
        pending = rest
        if dups:
            groups.append((keeper, dups))
        else:
            singles += 1
    return groups, singles


def cmd_cleanup_similar(args):
    """cleanup-similar 명령: 단일 폴더 내 이미지의 지각적 유사도 기반 중복 제거.

    - 대상: -f 지정 폴더의 이미지 파일만 (서브폴더 존재 시 오류 중단)
    - 판별: perceptual hash(pHash), 유사도% = (1 - Hamming/bits) * 100
    - 보존: 유사 그룹 내 최대 용량 (동률 시 파일명 오름차순)
    - 삭제: 휴지통 이동 (전역 일괄, --files-from)
    """
    remote = args.remote.rstrip(":")
    folder = unicodedata.normalize("NFC", args.f)

    if not folder:
        print("[오류] cleanup-similar는 -f로 대상 폴더를 반드시 지정해야 합니다.",
              file=sys.stderr)
        sys.exit(1)

    threshold = args.threshold
    if not (0 <= threshold <= 100):
        print("[오류] --threshold는 0~100 사이 정수여야 합니다.", file=sys.stderr)
        sys.exit(1)

    remotes = run_rclone(["listremotes"]).split()
    if f"{remote}:" not in remotes:
        print(f"[오류] '{remote}' remote를 찾을 수 없습니다. "
              f"설정된 remote: {', '.join(remotes) or '없음'}", file=sys.stderr)
        sys.exit(1)

    # 서브폴더 존재 시 오류 중단 (단일 폴더 제약)
    if has_subfolders(remote, folder):
        print(f"[오류] '{folder}' 아래에 서브폴더가 있습니다. "
              f"cleanup-similar는 서브폴더 없는 단일 폴더만 처리합니다.", file=sys.stderr)
        sys.exit(1)

    # 이미지 파일만 대상 (비이미지는 제외하고 개수만 표기)
    entries = list_files(remote, folder)
    image_entries = [e for e in entries if is_image(e["Name"])]
    non_image = len(entries) - len(image_entries)

    if len(image_entries) < 2:
        print(f"비교할 이미지가 부족합니다. (이미지 {len(image_entries)}개"
              + (f", 비이미지 {non_image}개 제외" if non_image else "") + ")")
        return

    # 원본 다운로드 → pHash 계산 (임시 디렉토리는 작업 후 정리)
    tmpdir = tempfile.mkdtemp(prefix="dropbox_sim_")
    try:
        download_images(remote, folder, image_entries, tmpdir, args.tpslimit)
        files = compute_image_hashes(folder, image_entries, tmpdir, args.full)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if len(files) < 2:
        print("유효한 이미지가 부족해 비교를 진행할 수 없습니다.")
        return

    bits = files[0]["hash"].hash.size          # 64(기본) 또는 256(--full)
    maxdist = math.floor(bits * (1 - threshold / 100))
    groups, singles = group_by_keeper(files, maxdist, bits)

    # 리포트 출력
    to_delete = []
    for gi, (keeper, dups) in enumerate(groups, start=1):
        print(f"\n[유사 그룹 #{gi}] 유사도 임계값 {threshold}% 이상")
        print(f"  보존 (최대 용량): {join_path(folder, keeper['name'])}  "
              f"({human_size(keeper['size'])}, {keeper['dims']})")
        print("  삭제 예정:")
        for f, pct in sorted(dups, key=lambda x: -x[1]):
            print(f"    - {join_path(folder, f['name'])}  "
                  f"({human_size(f['size'])}, {f['dims']})  유사도 {pct:.0f}%")
            to_delete.append((folder, {"Name": f["name"]}))

    print(f"\n===== 요약: 대상 이미지 {len(files)}개 / 유사 그룹 {len(groups)}개 / "
          f"삭제(휴지통) {len(to_delete)}개 / 단독 {singles}개"
          + (f" / 비이미지 {non_image}개 제외" if non_image else "") + " =====")

    if args.dry_run:
        print("[DRY-RUN] 실제 변경은 수행하지 않았습니다.")
        return

    if not to_delete:
        print("삭제할 유사 이미지가 없습니다.")
        return

    print("\n[처리 중] 유사 이미지 삭제 (일괄)")
    batch_delete(remote, to_delete, args.tpslimit)
    print(f"  휴지통 이동 완료: {len(to_delete)}개")
    print("\n완료되었습니다. (삭제 파일은 Dropbox 휴지통에서 복구 가능)")


def build_parser():
    """서브커맨드 기반 argument parser 구성."""
    parser = argparse.ArgumentParser(
        description="rclone 기반 Dropbox 관리 도구"
    )
    subparsers = parser.add_subparsers(dest="command", required=True,
                                       help="실행할 명령")

    # 여러 명령이 공통으로 쓰는 옵션 (향후 추가 명령도 재사용)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--remote", default="dropbox",
                        help="rclone에 설정된 Dropbox remote 이름 (기본: dropbox, 뒤 ':' 유무 무관)")
    common.add_argument("--tpslimit", type=int, default=0,
                        help="rclone 초당 트랜잭션 제한 (Dropbox rate limit 완화)")

    # cleanup: 폴더별 중복 제거(전역 일괄, 휴지통 이동) + 선택적 순차 이름부여
    p_cleanup = subparsers.add_parser(
        "cleanup", parents=[common],
        help="폴더별 중복 파일 제거(휴지통 이동). --rename 지정 시 파일명을 1번부터 순차 부여"
    )
    p_cleanup.add_argument("-f", default="", help="처리 시작 경로 (기본: 루트 전체)")
    p_cleanup.add_argument("--dry-run", action="store_true", help="실제 변경 없이 계획만 출력")
    p_cleanup.add_argument("--rename", action="store_true",
                           help="중복 제거 후 보존 파일명을 1번부터 순차 부여 (0-padding 자동)")
    p_cleanup.set_defaults(func=cmd_cleanup)

    # cleanup-similar: 단일 폴더 내 이미지의 지각적 유사도 기반 중복 제거
    p_similar = subparsers.add_parser(
        "cleanup-similar", parents=[common],
        help="단일 폴더 내 이미지를 유사도(pHash) 비교로 중복 제거(휴지통 이동)"
    )
    p_similar.add_argument("-f", required=True,
                           help="처리 대상 폴더 (필수, 서브폴더 있으면 오류)")
    p_similar.add_argument("--threshold", type=int, default=90,
                           help="유사도 임계값(백분율 정수, 기본: 90). 이상이면 유사로 판정")
    p_similar.add_argument("--full", action="store_true",
                           help="원본 해상도 기반 정밀 해시(256비트) 사용. 미지정 시 64비트")
    p_similar.add_argument("--dry-run", action="store_true", help="실제 변경 없이 유사 그룹 리포트만 출력")
    p_similar.set_defaults(func=cmd_cleanup_similar)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)   # 각 서브커맨드에 연결된 함수 실행


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단되었습니다.", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as e:
        print(f"\n[오류] {e}", file=sys.stderr)
        sys.exit(1)
