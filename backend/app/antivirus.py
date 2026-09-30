from typing import Protocol

from app.core import settings


class AntivirusProvider(Protocol):
    def scan(self, content: bytes) -> None: ...


class DisabledAntivirus:
    def scan(self, content: bytes) -> None:
        return None


class ClamAVProvider:
    """Stream content to clamd without persisting a temporary file."""

    def scan(self, content: bytes) -> None:
        try:
            import clamd

            result = clamd.ClamdNetworkSocket(settings().clamav_host, settings().clamav_port).instream(content)
        except Exception as exc:
            raise RuntimeError("ANTIVIRUS_UNAVAILABLE") from exc
        if result.get("stream", ("ERROR",))[0] != "OK":
            raise RuntimeError("MALWARE_DETECTED")


def antivirus() -> AntivirusProvider:
    provider = settings().antivirus_provider.lower()
    if provider == "clamav":
        return ClamAVProvider()
    if provider == "disabled":
        return DisabledAntivirus()
    raise RuntimeError("ANTIVIRUS_PROVIDER_INVALID")
