"""cleanup 명령: 폴더별 hash 기준 중복 제거 + 선택적 순차 이름부여."""

import os
import unicodedata
from collections import defaultdict

from dropbox_tool.rclone import (
    batch_delete,
    ensure_remote,
    get_hash,
    join_path,
    list_files,
    list_folders,
    remote_path,
    run_rclone,
)


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
        kept.append(group_sorted[0])  # 첫 파일만 보존
        for dup in group_sorted[1:]:  # 나머지는 삭제(휴지통 이동)
            to_delete.append((folder, dup))
    kept.extend(no_hash)  # hash 없는 파일은 판별 불가 → 보존

    # 2) 이름변경 계획 (--rename 지정 시에만, 0-padding 자동)
    logical_renames, ordered_moves = [], []
    if rename:
        kept_sorted = sorted(kept, key=lambda x: x["Name"])
        width = len(str(len(kept_sorted)))  # 자릿수 자동 계산
        pairs = []  # (현재명, 최종명)
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
    remote = args.remote.rstrip(":")  # 뒤에 붙은 ':' 제거
    # 한글/일본어 경로의 NFC/NFD 불일치 방지를 위해 조합형(NFC)으로 정규화
    root = unicodedata.normalize("NFC", args.f)

    ensure_remote(remote)

    folders = list_folders(remote, root)

    all_deletes = []  # [(folder, entry)]  전역 삭제 대상
    rename_plans = []  # [(folder, ordered_moves)]
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
