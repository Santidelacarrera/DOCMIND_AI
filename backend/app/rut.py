"""Chilean RUT/RUN: check digit, validation and canonical form.

A RUT is a body of up to 8 digits plus a check digit (``0-9`` or ``K``) computed with
modulo 11 over the body digits weighted 2, 3, 4, 5, 6, 7, 2, 3, ... from the right.
It is printed as ``12.345.678-5`` or ``12345678-5``. The canonical form used for
comparison and storage is ``12345678-5`` (no dots, upper-case ``K``).
"""

import re

# Dotted (12.345.678-5) or plain (12345678-5) bodies; the hyphen before the check digit is required,
# because without it any 8-9 digit number would be a candidate.
PATTERN = re.compile(r"(?<![\d.])(?:\d{1,2}\.\d{3}\.\d{3}|\d{7,8})-[\dkK](?![\w])")


def check_digit(body: str) -> str:
    total, weight = 0, 2
    for digit in reversed(body):
        total += int(digit) * weight
        weight = 2 if weight == 7 else weight + 1
    rest = 11 - total % 11
    return {11: "0", 10: "K"}.get(rest, str(rest))


def normalize(value: str) -> str | None:
    """``12345678-5`` for any valid spelling of a RUT, else ``None``."""
    compact = re.sub(r"[.\s]", "", str(value)).upper()
    match = re.fullmatch(r"(\d{7,8})-?([\dK])", compact)
    if not match:
        return None
    body, dv = match.groups()
    body = body.lstrip("0") or "0"
    return f"{body}-{dv}" if check_digit(body) == dv else None


def is_valid(value: str) -> bool:
    return normalize(value) is not None
