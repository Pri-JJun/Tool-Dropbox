# dropbox-tool

## 개요
- 목적: rclone을 감싼 Dropbox 정리 CLI(`dropbox-tool`). 서브커맨드 두 개:
  - `cleanup`: 폴더별로 내용 hash(DropboxHash)가 같은 파일을 휴지통으로 이동, `--rename` 시 남은 파일을 `1.jpg, 2.jpg…`로 순차 이름 부여
  - `cleanup-similar`: 서브폴더 없는 단일 폴더의 이미지를 pHash로 비교해 유사 이미지 제거
- 기술 스택: Python 3.14(`.python-version` 고정), uv(src layout, `uv_build`), Pillow, imagehash, pytest, ruff. 외부 의존: rclone 설치와 Dropbox remote 설정(`rclone config`, 기본 이름 `dropbox`)

## 명령어
- 설치: `uv sync` (.venv 생성)
- 실행:
  ```bash
  uv run dropbox-tool cleanup -f 사진 --dry-run
  uv run dropbox-tool cleanup-similar -f 사진/여행 --dry-run
  uv run python -m dropbox_tool ...         # 동일 (VS Code launch.json이 이 방식 사용)
  ```
- 테스트:
  ```bash
  uv run pytest                             # 전체 테스트
  uv run pytest tests/test_cleanup.py::test_plan_moves_resolves_swap_cycle_via_temp_name
  ```
- 린트: `uv run ruff check --fix && uv run ruff format`

## 구조
- `cli.py`(argparse, `main`) → `cleanup.py` / `similar.py`(명령 로직) → `rclone.py`(모든 rclone 호출).
- **모든 외부 호출은 `rclone.run_rclone`을 거친다.** 여기서 `RCLONE_DROPBOX_PERMANENT_DELETE=false`를 환경변수로 강제해 삭제가 항상 휴지통 이동이 되도록 보장하고, UTF-8 인코딩을 고정한다.
- **오류 처리 규약:** rclone 실패(및 rclone 미설치)는 `RuntimeError`로 올리고 `cli.main`이 `[오류] ...` 출력 후 exit 1로 변환한다. 입력 검증 오류는 각 명령에서 stderr 출력 후 `sys.exit(1)`. Ctrl+C는 exit 130.
- **삭제는 전역 1회 일괄:** 모든 폴더의 삭제 대상을 모아 임시 파일에 remote 루트 기준 상대경로로 기록하고 `rclone delete <remote>: --files-from`으로 한 번에 처리한다(`batch_delete`).
- **이름변경 순서:** `cleanup.plan_moves`가 덮어쓰기 없이 최소 이동 순서를 계산한다. 목적지가 비어 있으면 바로 이동하고, 사이클(예: 1↔2 스왑)만 `.dedup_tmp_N` 임시명을 거친다.
- **중복 판정 규칙:** 같은 hash 그룹에서 파일명 오름차순 첫 파일을 보존, hash 없는 파일은 항상 보존. 유사 이미지는 용량이 가장 큰 파일을 keeper로 두고 keeper와의 직접 Hamming distance(`maxdist = floor(bits * (1 - threshold/100))`)로 판정한다.
- 경로 인자는 `unicodedata.normalize("NFC", ...)`로 정규화한다(macOS NFD 한글 경로 대응).
- `Pillow`/`imagehash`는 `similar.compute_image_hashes` 안에서 지연 import한다(`cleanup`만 쓸 때 로딩 비용 회피).
- `cleanup-similar`는 rclone에 썸네일 API가 없어 이미지 원본을 임시 디렉토리로 내려받아 해시를 계산한다.

## 동작 호환성
이 패키지는 단일 스크립트 `tool.py`(git 첫 커밋 `a411947`)를 분리한 것으로, 출력 문자열·종료 코드·rclone 호출 인자가 원본과 동일하도록 검증했다. 사용자 출력은 한국어이며 의도하지 않은 출력 변경은 피한다.

## 규칙
- rclone 호출을 새로 추가할 때도 반드시 `rclone.run_rclone`을 사용한다(휴지통 이동 보장·UTF-8 고정).
- 테스트는 rclone 없이 순수 함수만 다룬다. 명령 전체 흐름을 확인할 때는 실제 Dropbox를 건드리지 말고 `--dry-run`을 쓰거나, 호출 인자를 기록하고 가짜 JSON을 돌려주는 rclone 스텁 스크립트를 PATH 앞에 두고 실행한다.
