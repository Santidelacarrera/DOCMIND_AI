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
        import io

        try:
            import clamd

            # clamd's instream reads a file-like object; passing bytes raises AttributeError on every scan.
            result = clamd.ClamdNetworkSocket(settings().clamav_host, settings().clamav_port).instream(
                io.BytesIO(content)
            )
        except Exception as exc:
            raise RuntimeError("ANTIVIRUS_UNAVAILABLE") from exc
        status = result.get("stream", ("ERROR",))[0]
        if status == "OK":
            return
        if status == "FOUND":
            raise RuntimeError("MALWARE_DETECTED")
        # ERROR or anything unexpected: the scanner could not decide, so the upload must not pass.
        raise RuntimeError("ANTIVIRUS_UNAVAILABLE")


def antivirus() -> AntivirusProvider:
    provider = settings().antivirus_provider.lower()
    if provider == "clamav":
        return ClamAVProvider()
    if provider == "disabled":
        return DisabledAntivirus()
    raise RuntimeError("ANTIVIRUS_PROVIDER_INVALID")
