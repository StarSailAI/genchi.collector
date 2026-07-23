from __future__ import annotations

import ipaddress
import socket
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from allfeeds_sdk import PermanentError, RateLimitError, TransientError

UTC = UTC


class SafeHttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        retries: int,
        max_response_bytes: int,
        obey_robots: bool,
        allow_private_network: bool,
        allowed_hosts: tuple[str, ...],
        rate_limit_seconds: float,
        headers: dict[str, str] | None = None,
    ):
        self.user_agent = user_agent
        self.timeout = timeout_seconds
        self.retries = retries
        self.max_response_bytes = max_response_bytes
        self.obey_robots = obey_robots
        self.allow_private_network = allow_private_network
        self.allowed_hosts = {host.lower() for host in allowed_hosts}
        self.rate_limit_seconds = rate_limit_seconds
        self.session = requests.Session()
        self.headers = {"User-Agent": user_agent, "Accept": "*/*", **(headers or {})}
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}

    def validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PermanentError(f"unsupported URL: {url!r}")
        hostname = parsed.hostname.lower()
        if hostname in self.allowed_hosts or self.allow_private_network:
            return
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443)}
        except OSError as exc:
            raise TransientError(f"DNS resolution failed for {hostname}: {exc}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise PermanentError(
                    f"private or non-routable address blocked for {hostname}; use allowed_hosts explicitly"
                )

    def _respect_rate(self, hostname: str) -> None:
        wait = self.rate_limit_seconds - (time.monotonic() - self._last_request.get(hostname, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_request[hostname] = time.monotonic()

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        parsed = urlsplit(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        robots = self._robots.get(root)
        if robots is None:
            robots = RobotFileParser()
            robots.set_url(urljoin(root, "/robots.txt"))
            try:
                response = self.session.get(
                    robots.url,
                    headers=self.headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                robots.parse(response.text.splitlines() if response.ok else [])
            except requests.RequestException:
                robots.parse([])
            self._robots[root] = robots
        return robots.can_fetch(self.user_agent, url)

    @staticmethod
    def _retry_after(response: requests.Response, fallback: float) -> float:
        raw = response.headers.get("Retry-After")
        if not raw:
            return fallback
        try:
            return max(0, min(3600, float(raw)))
        except ValueError:
            try:
                value = parsedate_to_datetime(raw)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
                return max(0, min(3600, (value - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError):
                return fallback

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        allowed_content_types: tuple[str, ...] = (),
    ) -> requests.Response:
        self.validate_url(url)
        if not self._allowed_by_robots(url):
            raise PermanentError(f"robots.txt disallows {url}")
        hostname = urlsplit(url).hostname or ""
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._respect_rate(hostname)
            try:
                response = self.session.get(
                    url,
                    headers={**self.headers, **(headers or {})},
                    timeout=self.timeout,
                    allow_redirects=True,
                    stream=True,
                )
                self.validate_url(response.url)
                if response.status_code == 429:
                    delay = self._retry_after(response, 2**attempt)
                    if attempt >= self.retries:
                        raise RateLimitError(
                            f"rate limited by {hostname}", retry_after_seconds=delay
                        )
                    time.sleep(delay)
                    continue
                if response.status_code >= 500:
                    if attempt >= self.retries:
                        raise TransientError(f"upstream returned HTTP {response.status_code}")
                    time.sleep(min(30, 2**attempt))
                    continue
                if response.status_code >= 400 and response.status_code != 304:
                    raise PermanentError(f"upstream returned HTTP {response.status_code}")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if (
                    allowed_content_types
                    and content_type
                    and not any(
                        content_type == allowed
                        or content_type.startswith(f"{allowed}+")
                        or (allowed == "application/json" and content_type.endswith("+json"))
                        for allowed in allowed_content_types
                    )
                ):
                    raise PermanentError(f"unexpected content type {content_type!r}")
                chunks, size = [], 0
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise PermanentError(
                            f"response exceeds max_response_bytes={self.max_response_bytes}"
                        )
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
                return response
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise TransientError(f"HTTP request failed for {url}: {exc}") from exc
                time.sleep(min(30, 2**attempt))
        raise TransientError(f"HTTP request failed: {last_error}")
