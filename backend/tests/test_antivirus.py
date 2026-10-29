"""Antivirus decision logic, without a daemon.

Regression for a defect found by the live ClamAV check: ``clamd.instream`` reads a file-like object, and the
provider passed ``bytes``, so every scan failed and every upload was refused. The fake below reads its input
the way ``clamd`` does, so passing bytes fails here too.
"""

import sys
import types
from typing import Any, ClassVar

import pytest

from app import antivirus
from app.core import settings


class FakeClamd:
    reply: ClassVar[dict[str, Any]] = {"stream": ("OK", None)}
    seen: ClassVar[list[Any]] = []

    def __init__(self, host: str, port: int) -> None:
        pass

    def instream(self, buff: Any) -> dict[str, Any]:
        FakeClamd.seen.append(buff)
        buff.read(1)  # clamd reads from the object; a bytes value has no read() and fails here
        return FakeClamd.reply


@pytest.fixture
def clamd_fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeClamd]:
    monkeypatch.setitem(sys.modules, "clamd", types.SimpleNamespace(ClamdNetworkSocket=FakeClamd))
    monkeypatch.setattr(settings(), "clamav_host", "clamd.test")
    FakeClamd.seen = []
    FakeClamd.reply = {"stream": ("OK", None)}
    return FakeClamd


def test_a_clean_result_passes(clamd_fake: type[FakeClamd]) -> None:
    antivirus.ClamAVProvider().scan(b"clean content")
    assert len(clamd_fake.seen) == 1


def test_the_scanner_receives_a_file_like_object_not_bytes(clamd_fake: type[FakeClamd]) -> None:
    antivirus.ClamAVProvider().scan(b"clean content")
    assert not isinstance(clamd_fake.seen[0], bytes) and hasattr(clamd_fake.seen[0], "read")


def test_a_detection_is_refused_as_malware(clamd_fake: type[FakeClamd]) -> None:
    clamd_fake.reply = {"stream": ("FOUND", "Eicar-Test-Signature")}
    with pytest.raises(RuntimeError, match="MALWARE_DETECTED"):
        antivirus.ClamAVProvider().scan(b"x")


@pytest.mark.parametrize("reply", [{"stream": ("ERROR", "lstat failed")}, {}, {"stream": ("SOMETHING", None)}])
def test_an_undecided_scan_is_refused_as_unavailable_not_as_malware(clamd_fake: type[FakeClamd], reply: dict[str, Any]) -> None:
    clamd_fake.reply = reply
    with pytest.raises(RuntimeError, match="ANTIVIRUS_UNAVAILABLE"):
        antivirus.ClamAVProvider().scan(b"x")


def test_a_daemon_that_cannot_be_reached_is_refused_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def __init__(self, host: str, port: int) -> None:
            raise ConnectionRefusedError

    monkeypatch.setitem(sys.modules, "clamd", types.SimpleNamespace(ClamdNetworkSocket=Down))
    with pytest.raises(RuntimeError, match="ANTIVIRUS_UNAVAILABLE"):
        antivirus.ClamAVProvider().scan(b"x")
