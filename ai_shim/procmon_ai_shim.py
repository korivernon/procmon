"""
Loaded into every process procmon launches. Sits under the Anthropic and OpenAI SDKs (they send through httpx or its fork httpx2) so
AI calls are counted, and refused when procmon's lever is off, with no change to the app.

Stdlib only, since it loads into every app's venv. Nothing here may raise into the app.

Levers are read in the background, so an AI call never waits on procmon. If procmon is unreachable the last known
levers hold (kept on disk, so an "off" survives an app restart during an outage); with none known, AI is allowed.
A procmon restart must not stop trading AI.

Apps can name what a call is for. `import procmon_ai_shim` only succeeds under procmon, which is how an app senses
it is being managed:
    with procmon_ai_shim.category("pm_decision"): client.messages.create(...)
A call with no name is categorized by the module that made it.
"""
from __future__ import annotations

import atexit
import collections
import contextlib
import contextvars
import importlib.abc
import importlib.util
import json
import zlib
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

POLL_SECONDS = 15
_AI_HOSTS = {"api.anthropic.com": "anthropic", "api.openai.com": "openai", "api.x.ai": "xai",
             "generativelanguage.googleapis.com": "google"}

_category = contextvars.ContextVar("procmon_ai_category", default=None)
_events = collections.deque(maxlen=5000)
_declared = set()
_lock = threading.Lock()
_wake = threading.Event()
_policy = {"enabled": True, "disabled": set(), "known": False}
_thread_pid = None
_fetched = False


def _project():
    return os.environ.get("PROCMON_AI_PROJECT", "")


def _cache_path():
    return os.path.join(os.path.expanduser("~/.cache/procmon_ai"), _project() + ".json")


def _load_cached_policy():
    try:
        with open(_cache_path()) as f:
            saved = json.load(f)
        _policy.update(enabled=bool(saved.get("enabled", True)), disabled=set(saved.get("disabled", [])), known=True)
    except Exception:
        pass


def _save_policy():
    try:
        os.makedirs(os.path.dirname(_cache_path()), exist_ok=True)
        tmp = "%s.%d" % (_cache_path(), os.getpid())
        with open(tmp, "w") as f:
            json.dump({"enabled": _policy["enabled"], "disabled": sorted(_policy["disabled"])}, f)
        os.replace(tmp, _cache_path())
    except Exception:
        pass


def _sync(timeout=5.0):
    with _lock:
        batch = list(_events)
        _events.clear()
        categories = sorted(_declared)
    body = json.dumps({"project_id": _project(), "process": os.environ.get("PROCMON_AI_PROCESS", ""),
                       "categories": categories, "events": batch}).encode()
    request = urllib.request.Request(os.environ.get("PROCMON_AI_URL", "") + "/api/ai/sync", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Procmon-AI-Token": os.environ.get("PROCMON_AI_TOKEN", "")})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode())
    except Exception:
        with _lock:
            _events.extendleft(reversed(batch))    # procmon is restarting or down: keep them for the next try
        return False
    if answer.get("managed"):
        changed = (answer["enabled"], set(answer["disabled"])) != (_policy["enabled"], _policy["disabled"])
        _policy.update(enabled=answer["enabled"], disabled=set(answer["disabled"]), known=True)
        if changed:
            _save_policy()
    return True


def _loop():
    while True:
        woken = _wake.wait(POLL_SECONDS)
        _wake.clear()
        if woken:
            time.sleep(2)       # let a burst of calls share one request
        _sync()


def _ensure_thread():
    # A forked child (multiprocessing) inherits the pid check but not the thread, so it starts its own.
    global _thread_pid
    if _thread_pid != os.getpid():
        _thread_pid = os.getpid()
        threading.Thread(target=_loop, daemon=True, name="procmon-ai").start()


def _ensure_policy():
    global _fetched
    _ensure_thread()
    if not _fetched:
        _fetched = True
        _load_cached_policy()
        _sync(timeout=1.5)


def _record(name, status, **fields):
    event = {"ts": time.time(), "category": name, "status": status}
    event.update(fields)
    with _lock:
        _declared.add(name)
        _events.append(event)
    _wake.set()
    _ensure_thread()


# ---- what apps call -----------------------------------------------------------------------------------------------

@contextlib.contextmanager
def category(name):
    """Names what the AI calls inside this block are for."""
    token = _category.set(name)
    try:
        yield
    finally:
        _category.reset(token)


def check(name=None):
    """False if procmon's levers refuse this right now, counted as a blocked call. name=None asks about the whole
    project. Lets an app refuse up front with its own message instead of failing at the HTTP call."""
    try:
        if _is_on(name):
            return True
        _record(name or "general", "blocked")
        return False
    except Exception:
        return True


def _is_on(name):
    _ensure_policy()
    return _policy["enabled"] and name not in _policy["disabled"]


def declare(names):
    """Registers an app's categories so their levers exist before the first call of each."""
    try:
        with _lock:
            _declared.update(names)
        _wake.set()
        _ensure_thread()
    except Exception:
        pass


# ---- the hook under the SDKs ---------------------------------------------------------------------------------------

def _classify(request):
    """The provider if this request is a billable AI call, else None."""
    try:
        if request.method != "POST":
            return None
        host, path = request.url.host, request.url.path
        if path.endswith("/count_tokens"):
            return None
        if host in _AI_HOSTS:
            return _AI_HOSTS[host]
        if path.endswith(("/chat/completions", "/v1/messages")):
            return host
    except Exception:
        pass
    return None


def _caller_category():
    """The module that made the call, skipping the SDKs and the standard library: prediction_mm.news.sentiment ->
    news.sentiment."""
    frame = sys._getframe(2)
    for _ in range(80):
        if frame is None:
            break
        filename = frame.f_code.co_filename
        if (filename != __file__ and "site-packages" not in filename and "/lib/python3" not in filename
                and not filename.startswith("<")):
            module = frame.f_globals.get("__name__", "")
            if module == "__main__":
                return os.path.splitext(os.path.basename(filename))[0]
            return module.split(".", 1)[-1] or "general"
        frame = frame.f_back
    return "general"


def _usage_fields(usage):
    if not isinstance(usage, dict):
        return {}
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    fields = {
        "input_tokens": int(usage.get("input_tokens") or 0) or max(int(usage.get("prompt_tokens") or 0) - cached, 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0) or cached,
        "web_searches": int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0),
    }
    return {key: value for key, value in fields.items() if value}


def _request_model(request):
    try:
        return json.loads(request.content).get("model")
    except Exception:
        return None


def _decoder(encoding):
    """A function turning one wire chunk into plain bytes, or None when the stream isn't compressed."""
    encoding = (encoding or "").lower().strip()
    if encoding in ("", "identity"):
        return None
    if encoding in ("gzip", "x-gzip", "deflate"):
        d = zlib.decompressobj(zlib.MAX_WBITS | 32)      # 32: gzip or zlib header, whichever it is
        return d.decompress
    if encoding == "br":
        try:
            import brotli
        except ImportError:
            try:
                import brotlicffi as brotli
            except ImportError:
                return lambda chunk: b""
        d = brotli.Decompressor()
        return getattr(d, "process", None) or d.decompress
    if encoding == "zstd":
        try:
            import zstandard
        except ImportError:
            return lambda chunk: b""
        return zstandard.ZstdDecompressor().decompressobj().decompress
    return lambda chunk: b""


class _StreamScan:
    """Reads the token counts out of a streamed response as the app consumes it."""

    def __init__(self, on_done, encoding=""):
        self.on_done, self.carry, self.fields, self.model, self.done = on_done, b"", {}, None, False
        self.decoder = _decoder(encoding)

    def feed(self, chunk):
        # The tee sees the bytes as they came off the wire, so a gzip/deflate/br stream is unpacked here first (the
        # app's own copy is untouched). Without this every compressed stream recorded 0 tokens.
        try:
            if self.decoder is not None:
                chunk = self.decoder(chunk)
            lines = (self.carry + chunk).split(b"\n")
            self.carry = lines.pop()
            for line in lines:
                if line.startswith(b"data:") and b'"usage"' in line:
                    self._read(json.loads(line[5:]))
        except Exception:
            self.carry = b""

    def _read(self, data):
        message = data.get("message") if isinstance(data.get("message"), dict) else data
        self.model = message.get("model") or self.model
        for key, value in _usage_fields(message.get("usage")).items():
            self.fields[key] = max(self.fields.get(key, 0), value)      # message_delta's counts are cumulative

    def finish(self):
        if not self.done:
            self.done = True
            self.on_done(self.fields, self.model)


def _blocked_response(httpx, request, name):
    # A refusal the SDKs already understand: raises their PermissionDeniedError, never retried, and lands in whatever
    # the app already does when an AI call fails.
    return httpx.Response(403, request=request, headers={"x-should-retry": "false"},
                          json={"type": "error", "error": {"type": "permission_error",
                                                           "message": "AI is turned off in procmon (%s)" % name}})


def _before(httpx, request):
    """(provider, category, blocked response or None); provider None means not an AI call."""
    provider = _classify(request)
    if not provider:
        return None, None, None
    name = _category.get() or _caller_category()
    if not _is_on(name):
        _record(name, "blocked", provider=provider, model=_request_model(request))
        return provider, name, _blocked_response(httpx, request, name)
    return provider, name, None


def _after(response, request, provider, name, scan_factory):
    base = {"provider": provider, "model": _request_model(request)}
    status = "ok" if response.status_code < 400 else "error"
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type and status == "ok":
        def done(fields, model):
            _record(name, status, **dict(base, model=model or base["model"], **fields))
        scan_factory(_StreamScan(done, response.headers.get("content-encoding", "")))
        return
    fields = {}
    try:
        data = json.loads(response.content)
        fields = _usage_fields(data.get("usage"))
        base["model"] = data.get("model") or base["model"]
    except Exception:
        pass
    _record(name, status, **dict(base, **fields))


def _patch(httpx):
    if getattr(httpx.Client.send, "_procmon", False):      # once per library, however many times install() runs
        return
    class _SyncTee(httpx.SyncByteStream):
        def __init__(self, inner, scan):
            self.inner, self.scan = inner, scan

        def __iter__(self):
            for chunk in self.inner:
                self.scan.feed(chunk)
                yield chunk
            self.scan.finish()

        def close(self):
            self.scan.finish()
            self.inner.close()

    class _AsyncTee(httpx.AsyncByteStream):
        def __init__(self, inner, scan):
            self.inner, self.scan = inner, scan

        async def __aiter__(self):
            async for chunk in self.inner:
                self.scan.feed(chunk)
                yield chunk
            self.scan.finish()

        async def aclose(self):
            self.scan.finish()
            await self.inner.aclose()

    sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

    def send(self, request, **kwargs):
        try:
            provider, name, blocked = _before(httpx, request)
        except Exception:
            return sync_send(self, request, **kwargs)
        if blocked is not None:
            return blocked
        if not provider:
            return sync_send(self, request, **kwargs)
        try:
            response = sync_send(self, request, **kwargs)
        except Exception:
            _record(name, "error", provider=provider, model=_request_model(request))
            raise
        try:
            def wrap(scan):
                response.stream = _SyncTee(response.stream, scan)
            _after(response, request, provider, name, wrap)
        except Exception:
            pass
        return response

    async def asend(self, request, **kwargs):
        try:
            provider, name, blocked = _before(httpx, request)
        except Exception:
            return await async_send(self, request, **kwargs)
        if blocked is not None:
            return blocked
        if not provider:
            return await async_send(self, request, **kwargs)
        try:
            response = await async_send(self, request, **kwargs)
        except Exception:
            _record(name, "error", provider=provider, model=_request_model(request))
            raise
        try:
            def wrap(scan):
                response.stream = _AsyncTee(response.stream, scan)
            _after(response, request, provider, name, wrap)
        except Exception:
            pass
        return response

    send._procmon = asend._procmon = True
    httpx.Client.send = send
    httpx.AsyncClient.send = asend


class _PatchHttpxOnImport(importlib.abc.MetaPathFinder):
    # Newer Anthropic SDKs send through a fork named httpx2, older ones and OpenAI's through httpx.
    NAMES = {"httpx", "httpx2"}

    def __init__(self):
        self.resolving = set()

    def find_spec(self, name, path=None, target=None):
        if name not in self.NAMES or name in self.resolving:
            return None
        self.resolving.add(name)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            self.resolving.discard(name)
        if spec and spec.loader:
            load = spec.loader.exec_module

            def exec_module(module):
                load(module)
                try:
                    _patch(module)
                except Exception:
                    pass
            spec.loader.exec_module = exec_module
        return spec


def _flush_at_exit():
    if _events:
        _sync(timeout=1.0)


def install():
    sys.meta_path.insert(0, _PatchHttpxOnImport())
    atexit.register(_flush_at_exit)
