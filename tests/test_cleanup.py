from dropbox_tool.cleanup import plan_moves


def apply_moves(names, moves):
    """이동 순서를 파일명 집합에 순서대로 적용 (충돌 시 실패)."""
    names = set(names)
    for src, dst in moves:
        assert src in names
        assert dst not in names, f"덮어쓰기 발생: {src} -> {dst}"
        names.remove(src)
        names.add(dst)
    return names


def test_plan_moves_without_conflict_moves_once():
    pairs = [("a.jpg", "1.jpg"), ("b.jpg", "2.jpg"), ("3.jpg", "3.jpg")]
    existing = {"a.jpg", "b.jpg", "3.jpg"}

    moves = plan_moves(pairs, existing)

    assert moves == [("a.jpg", "1.jpg"), ("b.jpg", "2.jpg")]


def test_plan_moves_resolves_swap_cycle_via_temp_name():
    pairs = [("1.jpg", "2.jpg"), ("2.jpg", "1.jpg")]
    existing = {"1.jpg", "2.jpg"}

    moves = plan_moves(pairs, existing)

    assert len(moves) == 3  # 사이클 1개 → 임시명 경유로 1회 추가
    assert any(dst.startswith(".dedup_tmp_") for _, dst in moves)
    assert apply_moves(existing, moves) == {"1.jpg", "2.jpg"}
