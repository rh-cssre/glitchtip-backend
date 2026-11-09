from ..utils import base32_decode, base32_encode


def test_base32_encode():
    assert base32_encode(10) == "A"


def test_base32_decode():
    assert base32_decode("A") == 10
