"""명령행 인터페이스: argument parser 구성 및 entry point."""

import argparse
import sys

from dropbox_tool.cleanup import cmd_cleanup
from dropbox_tool.similar import cmd_cleanup_similar


def build_parser():
    """서브커맨드 기반 argument parser 구성."""
    parser = argparse.ArgumentParser(description="rclone 기반 Dropbox 관리 도구")
    subparsers = parser.add_subparsers(dest="command", required=True, help="실행할 명령")

    # 여러 명령이 공통으로 쓰는 옵션 (향후 추가 명령도 재사용)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--remote",
        default="dropbox",
        help="rclone에 설정된 Dropbox remote 이름 (기본: dropbox, 뒤 ':' 유무 무관)",
    )
    common.add_argument(
        "--tpslimit",
        type=int,
        default=0,
        help="rclone 초당 트랜잭션 제한 (Dropbox rate limit 완화)",
    )

    # cleanup: 폴더별 중복 제거(전역 일괄, 휴지통 이동) + 선택적 순차 이름부여
    p_cleanup = subparsers.add_parser(
        "cleanup",
        parents=[common],
        help="폴더별 중복 파일 제거(휴지통 이동). --rename 지정 시 파일명을 1번부터 순차 부여",
    )
    p_cleanup.add_argument("-f", default="", help="처리 시작 경로 (기본: 루트 전체)")
    p_cleanup.add_argument("--dry-run", action="store_true", help="실제 변경 없이 계획만 출력")
    p_cleanup.add_argument(
        "--rename",
        action="store_true",
        help="중복 제거 후 보존 파일명을 1번부터 순차 부여 (0-padding 자동)",
    )
    p_cleanup.set_defaults(func=cmd_cleanup)

    # cleanup-similar: 단일 폴더 내 이미지의 지각적 유사도 기반 중복 제거
    p_similar = subparsers.add_parser(
        "cleanup-similar",
        parents=[common],
        help="단일 폴더 내 이미지를 유사도(pHash) 비교로 중복 제거(휴지통 이동)",
    )
    p_similar.add_argument("-f", required=True, help="처리 대상 폴더 (필수, 서브폴더 있으면 오류)")
    p_similar.add_argument(
        "--threshold",
        type=int,
        default=90,
        help="유사도 임계값(백분율 정수, 기본: 90). 이상이면 유사로 판정",
    )
    p_similar.add_argument(
        "--full",
        action="store_true",
        help="원본 해상도 기반 정밀 해시(256비트) 사용. 미지정 시 64비트",
    )
    p_similar.add_argument(
        "--dry-run", action="store_true", help="실제 변경 없이 유사 그룹 리포트만 출력"
    )
    p_similar.set_defaults(func=cmd_cleanup_similar)

    return parser


def main():
    """entry point: 서브커맨드 실행 + 중단/rclone 오류를 종료 코드로 변환."""
    try:
        parser = build_parser()
        args = parser.parse_args()
        args.func(args)  # 각 서브커맨드에 연결된 함수 실행
    except KeyboardInterrupt:
        print("\n중단되었습니다.", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as e:
        print(f"\n[오류] {e}", file=sys.stderr)
        sys.exit(1)
