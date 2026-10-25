"""Security response headers for every HTTP response.

Applied as the outermost middleware so auth denials, CORS preflights and
error responses carry them too. A strict Content-Security-Policy is
deliberately absent: the SPA uses inline styles and an admin-authored
custom-CSS block, so a useful policy would need nonces/hashes across the
whole design system. The headers here are the ones that are safe for the
current UI.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, MutableMapping

from starlette.datastructures import MutableHeaders

Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class SecurityHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Send,
    ) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message.get('type') == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers.setdefault('X-Content-Type-Options', 'nosniff')
                headers.setdefault('Referrer-Policy', 'no-referrer')
                headers.setdefault('X-Frame-Options', 'DENY')
                headers.setdefault(
                    'Permissions-Policy',
                    'camera=(), microphone=(), geolocation=()',
                )
                if scope.get('scheme') == 'https':
                    headers.setdefault(
                        'Strict-Transport-Security',
                        'max-age=31536000; includeSubDomains',
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)
