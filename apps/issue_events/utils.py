import string

digs = string.digits + string.ascii_uppercase


def base32_decode(base32_value: str) -> int:
    """
    Convert base32 string to integer
    Example 'A' -> 10
    """
    return int(base32_value, 32)


def int2base(x: int, base: int) -> str:
    """
    Convert base 10 integer to any base string that can be represented with numbers and
    upper case letters
    Example int2base(10, 32) -> 'A'
    Source: https://stackoverflow.com/a/2267446/443457
    """
    if x < 0:
        sign = -1
    elif x == 0:
        return digs[0]
    else:
        sign = 1
    x *= sign
    digits = []
    while x:
        digits.append(digs[int(x % base)])
        x = int(x / base)
    if sign < 0:
        digits.append("-")
    digits.reverse()
    return "".join(digits)


def base32_encode(base10_value: int) -> str:
    """
    Convert base 10 integer to base32 string
    Example 10 -> 'A'
    """
    return int2base(base10_value, 32)
