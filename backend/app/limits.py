"""Request-size enforcement that does not trust ``Content-Length``.

Starlette parses multipart bodies completely before a handler runs, so a client using
chunked transfer encoding could otherwise stream an arbitrarily large upload to disk.
This pure-ASGI middleware counts bytes as they arrive and aborts with ``413``.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

JSON_BODY_LIMIT = 1_048_576  # 1 MiB for every non-upload request
MULTIPART_OVERHEAD = 65_536


class BodyTooLarge(Exception):
    pass


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, upload_limit: int, upload_path: str) -> None:
        self.app = app
        self.upload_limit = upload_limit + MULTIPART_OVERHEAD
        self.upload_path = upload_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS", "DELETE"}:
            await self.app(scope, receive, send)
            return
        is_upload = scope["method"] == "POST" and scope["path"] == self.upload_path
        limit = self.upload_limit if is_upload else JSON_BODY_LIMIT
        headers = {k.lower(): v for k, v in scope["headers"]}
        declared = headers.get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await self._reject(send)
            return

        received = 0
        started = False

        async def guarded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"detail":"REQUEST_TOO_LARGE"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
