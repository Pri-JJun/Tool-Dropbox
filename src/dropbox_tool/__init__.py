"""
dropbox_tool

rclone을 이용해 Dropbox의 폴더별 중복 파일을 제거하는 도구.
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
  dropbox-tool cleanup -f 사진 --dry-run
  dropbox-tool cleanup -f 사진 --rename
  dropbox-tool cleanup-similar -f 사진/여행 --dry-run
  dropbox-tool cleanup-similar -f 사진/여행 --threshold 85
  dropbox-tool cleanup-similar -f 사진/여행 --full
"""

__version__ = "0.1.0"
