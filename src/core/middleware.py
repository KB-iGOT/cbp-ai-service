import time
import logging
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from .logger import logger


class APILoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()

        # 1. Extract the real client IP (Handles Reverse Proxies)
        client_ip = "unknown"

        # Check standard headers used by Nginx, AWS, Cloudflare, etc.
        if forwarded_for := request.headers.get("x-forwarded-for"):
            # x-forwarded-for can be a comma-separated list; the first one is the client
            client_ip = forwarded_for.split(",")[0].strip()
        elif real_ip := request.headers.get("x-real-ip"):
            client_ip = real_ip.strip()
        elif request.client:
            # Fallback to direct TCP connection IP
            client_ip = request.client.host

        # 2. Process the request and get the response
        response = await call_next(request)

        # 3. Calculate processing latency
        process_time = round((time.time() - start_time) * 1000, 2)

        # 4. Construct log message and pass the IP inside the 'extra' parameter
        http_version = f"HTTP/{request.scope.get('http_version', 'unknown')}"
        log_dict = {
            "client_ip": client_ip
        }
        message = (
            f"{client_ip} | "
            f"{request.method} | "
            f"{request.url.path} | "
            f"{http_version} | "
            f"{response.status_code} | "
            f"{process_time}ms"
        )

        logger.info(message, extra=log_dict)

        return response
