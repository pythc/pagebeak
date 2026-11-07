from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Bound requests before multipart/JSON parsing, including chunked bodies."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        # Large files use bounded raw chunks. Multipart is retained only for small legacy clients.
        path = scope.get("path", "")
        limit = 10 * 1024**2 + 100000 if path == "/v1/sources/upload" else 8 * 1024**2 if path.startswith("/v1/uploads/") and path.endswith("/content") else 1024**2
        if path.startswith("/v2/host-transfers/") and scope["method"] == "PUT":
            # Catalog tickets allow 16 MiB; file grants additionally enforce the
            # configured chunk size before persisting any bytes.
            limit = 16 * 1024**2
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > limit:
                return await JSONResponse({"detail": "请求内容超过本地版本大小限制"}, 413)(
                    scope, receive, send
                )
            chunks.append(body)
            if not message.get("more_body", False):
                break
        sent = False

        async def bounded_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
