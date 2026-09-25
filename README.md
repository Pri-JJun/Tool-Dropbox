# dropbox-tool

rclone을 이용해 Dropbox 폴더를 정리하는 CLI 도구입니다.

- `cleanup`: 폴더별로 내용 hash(DropboxHash)가 같은 중복 파일을 제거합니다. `--rename`을 주면 남은 파일 이름을 1번부터 차례로 붙입니다(0-padding 자동).
- `cleanup-similar`: 하위 폴더가 없는 폴더 하나에서 이미지들을 지각적 유사도(pHash)로 비교해, 비슷한 이미지 중 가장 큰 파일만 남기고 나머지를 제거합니다.

삭제는 영구 삭제가 아니라 Dropbox 휴지통으로 이동하므로 복구할 수 있습니다.

## 전제 조건

- [uv](https://docs.astral.sh/uv/)
- Python 3.14 이상 (`.python-version`에 고정, 없으면 `uv sync`가 자동으로 내려받음)
- [rclone](https://rclone.org/) 설치 및 Dropbox remote 설정 (`rclone config`, 기본 remote 이름은 `dropbox`)

## 설치

```bash
uv sync
```

`.venv`가 만들어지고 의존성(Pillow, imagehash)과 이 패키지가 설치됩니다.

어느 위치에서나 `dropbox-tool` 명령으로 쓰려면 전역 도구로 설치합니다.

```bash
uv tool install .
```

## 실행

```bash
# 중복 제거 계획만 출력 (실제 변경 없음)
uv run dropbox-tool cleanup -f 사진 --dry-run

# 중복 제거 + 순차 이름 부여
uv run dropbox-tool cleanup -f 사진 --rename

# 유사 이미지 리포트
uv run dropbox-tool cleanup-similar -f 사진/여행 --dry-run

# 유사도 임계값 변경 (기본 90%)
uv run dropbox-tool cleanup-similar -f 사진/여행 --threshold 85

# 256비트 정밀 해시 사용
uv run dropbox-tool cleanup-similar -f 사진/여행 --full
```

`python -m dropbox_tool ...`로도 실행할 수 있습니다.

공통 옵션:

| 옵션 | 설명 |
| --- | --- |
| `--remote NAME` | rclone remote 이름 (기본: `dropbox`) |
| `--tpslimit N` | rclone 초당 트랜잭션 제한 (Dropbox rate limit 완화) |

전체 옵션은 `uv run dropbox-tool <명령> --help`로 확인하세요.

## 프로젝트 구조

```
src/dropbox_tool/
  cli.py       # argument parser, entry point (main)
  rclone.py    # rclone 호출, 경로 결합, 목록 조회, 일괄 삭제
  cleanup.py   # cleanup 명령 (hash 기준 중복 제거, 순차 이름 부여)
  similar.py   # cleanup-similar 명령 (pHash 유사 이미지 제거)
tests/         # pytest
```

## 개발

```bash
uv run pytest            # 테스트
uv run ruff check        # lint
uv run ruff format       # 포맷
```
