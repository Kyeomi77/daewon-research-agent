"""공통 유틸리티.

- HTTP 클라이언트(재시도 / 캐시 / 호스트별 레이트리밋)
- 외부 질의 가드(사내 코드·기밀 표시 차단)  ← 연구소 IP 보호의 핵심
- 감사 로그(Cloud Logging 으로 stdout JSON 출력)
- FastMCP 서버 부트스트랩(Streamable HTTP, /healthz)

"""
import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

KST = ZoneInfo("Asia/Seoul")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
USER_AGENT = f"DaewonResearchAgent/1.0 (+mailto:{CONTACT_EMAIL})" if CONTACT_EMAIL else "DaewonResearchAgent/1.0"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout, format="%(message)s")
logger = logging.getLogger("daewon-mcp")


# --------------------------------------------------------------------------- 예외
class ToolError(Exception):
    """사용자(에이전트)에게 그대로 전달해도 안전한 오류."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class NotFound(ToolError):
    pass


# --------------------------------------------------------------------------- 시간/응답 형식
def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def envelope(source: str, data: Any, *, source_url: str | None = None, note: str | None = None) -> dict[str, Any]:
    """모든 도구 응답의 공통 포맷. 에이전트가 출처·조회일을 반드시 인용하도록 강제한다."""
    out: dict[str, Any] = {"source": source, "retrieved_at": now_iso(), "data": data}
    if source_url:
        out["source_url"] = source_url
    if note:
        out["note"] = note
    return out


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + " …(이하 생략)"


# --------------------------------------------------------------------------- 질의 가드
_BASE_BLOCK = [r"대외비", r"사내\s*기밀", r"confidential", r"internal\s+use\s+only", r"do\s+not\s+distribute"]


def _block_patterns() -> list[re.Pattern]:
    extra = [p for p in os.environ.get("BLOCK_PATTERNS", "").split(";;") if p.strip()]
    pats = []
    for p in _BASE_BLOCK + extra:
        try:
            pats.append(re.compile(p, re.IGNORECASE))
        except re.error:
            logger.warning(json.dumps({"event": "bad_block_pattern", "pattern": p}, ensure_ascii=False))
    return pats


_BLOCKS = _block_patterns()
MAX_QUERY_CHARS = int(os.environ.get("MAX_QUERY_CHARS", "500"))


def guard_query(*texts: str | None) -> None:
    """외부 API/검색으로 나가기 전에 호출. 사내 코드·기밀 표시·과도하게 긴 질의를 차단한다."""
    for t in texts:
        if not t:
            continue
        if len(t) > MAX_QUERY_CHARS:
            raise ToolError(
                f"질의가 {MAX_QUERY_CHARS}자를 초과했습니다. 외부 시스템으로 문서 본문을 보내지 말고 핵심 키워드만 사용하세요."
            )
        for p in _BLOCKS:
            if p.search(t):
                raise ToolError(
                    "사내 코드 또는 기밀 표시로 보이는 문자열이 포함되어 외부 질의를 차단했습니다.",
                    hint="미공개 화합물·제형 정보는 일반명/공개된 용어로 바꿔서 질의하세요. 필요 시 IP 담당자와 상의하세요.",
                )


# --------------------------------------------------------------------------- 감사 로그
_AUDIT_QUERIES = os.environ.get("AUDIT_LOG_QUERIES", "true").lower() == "true"


def audit(tool: str, **fields: Any) -> None:
    if not _AUDIT_QUERIES and "args" in fields:
        fields["args"] = "(omitted)"
    logger.info(json.dumps({"event": "tool_call", "tool": tool, "ts": now_iso(), **fields}, ensure_ascii=False, default=str))


def _short_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:300] if isinstance(v, str) else v) for k, v in kwargs.items()}


def safe_tool(fn):
    """도구 예외를 {'error':...} 로 변환 + 감사 로그. FastMCP 스키마 생성을 위해 wraps 사용."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            res = await fn(*args, **kwargs)
            audit(fn.__name__, status="ok", ms=int((time.monotonic() - t0) * 1000), args=_short_args(kwargs))
            return res
        except ToolError as e:
            audit(fn.__name__, status="rejected", error=str(e), args=_short_args(kwargs))
            return {"error": str(e), "hint": e.hint, "retrieved_at": now_iso()}
        except Exception as e:  # noqa: BLE001
            logger.exception("unhandled error in %s", fn.__name__)
            return {"error": f"내부 오류({type(e).__name__})가 발생했습니다. 잠시 후 다시 시도하세요.", "retrieved_at": now_iso()}

    return wrapper


# --------------------------------------------------------------------------- HTTP
_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_MAX = 512


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


class RateLimiter:
    """초당 N회 이하로 호출을 직렬 지연시키는 간단한 리미터."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            await asyncio.sleep(start - now)


async def fetch(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    as_: str = "json",
    cache_ttl: int = 300,
    limiter: RateLimiter | None = None,
    retries: int = 3,
) -> Any:
    """외부 API 호출. 오류 메시지에는 URL/키를 절대 포함하지 않는다(호스트만)."""
    host = httpx.URL(url).host
    key = None
    if cache_ttl and method == "GET":
        raw = json.dumps([url, sorted((params or {}).items())], default=str)
        key = hashlib.sha256(raw.encode()).hexdigest()
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]

    last = "알 수 없는 오류"
    for attempt in range(retries):
        if limiter:
            await limiter.wait()
        try:
            r = await _get_client().request(method, url, params=params, data=data, headers=headers)
        except httpx.TransportError as e:
            last = f"{host} 네트워크 오류({type(e).__name__})"
        else:
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{host} 일시 오류 HTTP {r.status_code}"
            elif r.status_code == 404:
                raise NotFound(f"{host} 에서 결과를 찾지 못했습니다(404).")
            elif r.status_code >= 400:
                raise ToolError(f"{host} 호출 실패 HTTP {r.status_code}", hint="API 키/파라미터/허용 IP 설정을 확인하세요.")
            else:
                if as_ == "json":
                    try:
                        out = r.json()
                    except ValueError:
                        raise ToolError(f"{host} 응답을 JSON으로 해석하지 못했습니다.") from None
                elif as_ == "text":
                    out = r.text
                else:
                    out = r.content
                if key:
                    if len(_cache) >= _CACHE_MAX:
                        _cache.pop(next(iter(_cache)))
                    _cache[key] = (time.time() + cache_ttl, out)
                return out
        await asyncio.sleep(min(2**attempt, 8))
    raise ToolError(last + " (재시도 초과)")


# --------------------------------------------------------------------------- 서버
def build_server(name: str, instructions: str) -> FastMCP:
    mcp = FastMCP(
        name,
        instructions=instructions,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        stateless_http=True,  # Cloud Run 다중 인스턴스에서 세션 고정 불필요
        json_response=True,
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def _health(request: Request) -> PlainTextResponse:  # noqa: ARG001
        return PlainTextResponse("ok")

    return mcp


def run(mcp: FastMCP) -> None:
    mcp.run(transport="streamable-http")  # 엔드포인트: /mcp
