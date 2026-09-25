"""cleanup-similar 명령: 단일 폴더 내 이미지의 지각적 유사도(pHash) 기반 중복 제거."""

import math
import os
import shutil
import sys
import tempfile
import unicodedata

from dropbox_tool.rclone import (
    batch_delete,
    ensure_remote,
    has_subfolders,
    join_path,
    list_files,
    remote_path,
    run_rclone,
)

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


def download_images(remote, folder, image_entries, tmpdir, tpslimit):
    """이미지 파일만 임시 디렉토리로 원본 다운로드.

    폴더 내 파일명이 유일하므로 --files-from에 파일명만 기록하면 충분.
    (rclone 썸네일 API 미지원으로 원본 다운로드가 불가피)
    """
    extra = ["--tpslimit", str(tpslimit)] if tpslimit else []
    listf = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", prefix="dropbox_img_", delete=False
    )
    try:
        for e in image_entries:
            listf.write(e["Name"] + "\n")
        listf.close()
        run_rclone(
            ["copy", remote_path(remote, folder), tmpdir, "--files-from", listf.name] + extra
        )
    finally:
        os.unlink(listf.name)


def compute_image_hashes(folder, image_entries, tmpdir, full):
    """다운로드된 이미지의 pHash와 해상도 계산.

    full=True면 hash_size=16(256비트, 더 정밀), 아니면 8(64비트).
    반환: [{folder, name, size, hash, dims}]
    """
    # 무거운 의존성이라 cleanup-similar 실행 시에만 import
    import imagehash
    from PIL import Image

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
        files.append(
            {
                "folder": folder,
                "name": e["Name"],
                "size": int(e.get("Size", 0)),
                "hash": h,
                "dims": dims,
            }
        )
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
        print("[오류] cleanup-similar는 -f로 대상 폴더를 반드시 지정해야 합니다.", file=sys.stderr)
        sys.exit(1)

    threshold = args.threshold
    if not (0 <= threshold <= 100):
        print("[오류] --threshold는 0~100 사이 정수여야 합니다.", file=sys.stderr)
        sys.exit(1)

    ensure_remote(remote)

    # 서브폴더 존재 시 오류 중단 (단일 폴더 제약)
    if has_subfolders(remote, folder):
        print(
            f"[오류] '{folder}' 아래에 서브폴더가 있습니다. "
            f"cleanup-similar는 서브폴더 없는 단일 폴더만 처리합니다.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 이미지 파일만 대상 (비이미지는 제외하고 개수만 표기)
    entries = list_files(remote, folder)
    image_entries = [e for e in entries if is_image(e["Name"])]
    non_image = len(entries) - len(image_entries)

    if len(image_entries) < 2:
        print(
            f"비교할 이미지가 부족합니다. (이미지 {len(image_entries)}개"
            + (f", 비이미지 {non_image}개 제외" if non_image else "")
            + ")"
        )
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

    bits = files[0]["hash"].hash.size  # 64(기본) 또는 256(--full)
    maxdist = math.floor(bits * (1 - threshold / 100))
    groups, singles = group_by_keeper(files, maxdist, bits)

    # 리포트 출력
    to_delete = []
    for gi, (keeper, dups) in enumerate(groups, start=1):
        print(f"\n[유사 그룹 #{gi}] 유사도 임계값 {threshold}% 이상")
        print(
            f"  보존 (최대 용량): {join_path(folder, keeper['name'])}  "
            f"({human_size(keeper['size'])}, {keeper['dims']})"
        )
        print("  삭제 예정:")
        for f, pct in sorted(dups, key=lambda x: -x[1]):
            print(
                f"    - {join_path(folder, f['name'])}  "
                f"({human_size(f['size'])}, {f['dims']})  유사도 {pct:.0f}%"
            )
            to_delete.append((folder, {"Name": f["name"]}))

    print(
        f"\n===== 요약: 대상 이미지 {len(files)}개 / 유사 그룹 {len(groups)}개 / "
        f"삭제(휴지통) {len(to_delete)}개 / 단독 {singles}개"
        + (f" / 비이미지 {non_image}개 제외" if non_image else "")
        + " ====="
    )

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
