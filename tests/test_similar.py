import pytest

from dropbox_tool.similar import group_by_keeper, human_size, is_image


class FakeHash:
    """imagehash처럼 `a - b`가 Hamming distance를 돌려주는 테스트용 hash."""

    def __init__(self, value):
        self.value = value

    def __sub__(self, other):
        return bin(self.value ^ other.value).count("1")


def image(name, size, value):
    return {"name": name, "size": size, "hash": FakeHash(value)}


@pytest.mark.parametrize(
    ("n", "expected"),
    [(0, "0B"), (1023, "1023B"), (1024, "1.0KB"), (1536, "1.5KB"), (5 * 1024**3, "5.0GB")],
)
def test_human_size(n, expected):
    assert human_size(n) == expected


def test_is_image_is_case_insensitive():
    assert is_image("IMG_001.JPG")
    assert is_image("photo.heic")
    assert not is_image("memo.txt")


def test_group_by_keeper_keeps_largest_file():
    files = [
        image("small.jpg", 100, 0b0000),
        image("big.jpg", 300, 0b0001),  # small과 거리 1
        image("other.jpg", 200, 0b1110),  # big과 거리 4
    ]

    groups, singles = group_by_keeper(files, maxdist=1, bits=4)

    assert singles == 1  # other.jpg
    assert len(groups) == 1
    keeper, dups = groups[0]
    assert keeper["name"] == "big.jpg"
    assert [(f["name"], pct) for f, pct in dups] == [("small.jpg", 75.0)]
