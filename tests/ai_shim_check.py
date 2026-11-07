"""Runs the shim against the real SDKs with a stub lever server and fake AI responses: nothing is sent or billed.
    cd procmon && PYTHONPATH=ai_shim PROCMON_AI_PROJECT=shim-check PROCMON_AI_PROCESS=check PROCMON_AI_TOKEN=t \
        PROCMON_AI_URL=http://127.0.0.1:18699 <venv>/bin/python tests/ai_shim_check.py
Needs httpx and anthropic in that venv; the OpenAI case is skipped without openai."""
import asyncio
import http.server
import json
import os
import threading
import time

received, policy = [], {"managed": True, "enabled": True, "disabled": []}


class Stub(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received.extend(body["events"])
        answer = json.dumps(policy).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


def start_stub():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 18699), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


server = start_stub()

import procmon_ai_shim as shim  # noqa: E402  (only importable under the shim's PYTHONPATH)
import anthropic  # noqa: E402
import anthropic._base_client  # noqa: E402

httpx = getattr(anthropic._base_client, "httpx2", None) or anthropic._base_client.httpx   # whichever this SDK uses
print("anthropic", anthropic.__version__, "on", httpx.__name__)

hits = []
BODY = {"id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5", "stop_reason": "end_turn",
        "stop_sequence": None, "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 10,
                  "cache_read_input_tokens": 20, "server_tool_use": {"web_search_requests": 2}}}
SSE = "".join("event: %s\ndata: %s\n\n" % (name, json.dumps(data)) for name, data in [
    ("message_start", {"type": "message_start", "message": {**BODY, "content": [], "stop_reason": None,
                                                            "usage": {"input_tokens": 7, "output_tokens": 1,
                                                                      "cache_read_input_tokens": 3}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "yo"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 9}}),
    ("message_stop", {"type": "message_stop"}),
]).encode()


def handler(request, lib=None):
    lib = lib or httpx
    hits.append(request.url.path)
    if request.url.path.endswith("/chat/completions"):
        return lib.Response(200, json={"id": "c", "object": "chat.completion", "created": 1, "model": "gpt-5",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "hi"}}],
                                         "usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34,
                                                   "prompt_tokens_details": {"cached_tokens": 10}}})
    if json.loads(request.content).get("stream"):
        return lib.Response(200, headers={"content-type": "text/event-stream"}, stream=lib.ByteStream(SSE))
    return lib.Response(200, json=BODY)


ARGS = dict(model="claude-sonnet-5", max_tokens=5, messages=[{"role": "user", "content": "x"}])
client = anthropic.Anthropic(api_key="k", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
aclient = anthropic.AsyncAnthropic(api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def wait_for(n):
    deadline = time.time() + 25
    while len(received) < n and time.time() < deadline:
        time.sleep(0.2)
    assert len(received) >= n, "only %d of %d events arrived: %s" % (len(received), n, received)
    return received[n - 1]


def expect(event, **fields):
    for key, value in fields.items():
        assert event.get(key) == value, "%s: wanted %r, got %r in %s" % (key, value, event.get(key), event)


# plain call, named category, every token type
with shim.category("pm_decision"):
    assert client.messages.create(**ARGS).content[0].text == "hi"
expect(wait_for(1), category="pm_decision", status="ok", model="claude-sonnet-5", provider="anthropic",
       input_tokens=100, output_tokens=50, cache_write_tokens=10, cache_read_tokens=20, web_searches=2)
print("ok  plain call: tokens, cache, web searches, named category")

# no name: the calling module
client.messages.create(**ARGS)
assert wait_for(2)["category"] == "ai_shim_check", received[-1]
print("ok  unnamed call: categorized by calling module")

# streaming, the way coffee-chats and rooted do it
with shim.category("stream"), client.messages.stream(**ARGS) as stream:
    assert "".join(stream.text_stream) == "yo"
expect(wait_for(3), category="stream", input_tokens=7, output_tokens=9, cache_read_tokens=3)
print("ok  sync stream: app gets its text, tokens read from the stream")


async def run_async():
    with shim.category("async"):
        await aclient.messages.create(**ARGS)
    with shim.category("astream"):
        async with aclient.messages.stream(**ARGS) as stream:
            assert "".join([t async for t in stream.text_stream]) == "yo"

asyncio.run(run_async())
expect(wait_for(4), category="async", output_tokens=50)
expect(wait_for(5), category="astream", output_tokens=9)
print("ok  async plain + async stream (prediction_mm's client)")

try:
    import openai
except ImportError:
    openai = None
if openai:
    import openai._base_client
    olib = getattr(openai._base_client, "httpx2", None) or openai._base_client.httpx
    print("openai", openai.__version__, "on", olib.__name__)
    oclient = openai.OpenAI(api_key="k", http_client=olib.Client(
        transport=olib.MockTransport(lambda r: handler(r, olib))))
    oclient.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "x"}])
    expect(wait_for(6), provider="openai", model="gpt-5", input_tokens=20, output_tokens=4, cache_read_tokens=10)
    print("ok  OpenAI: cached prompt tokens not double-counted")
offset = 6 if openai else 5

# a request that isn't AI passes untouched and isn't counted
with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="plain"))) as plain:
    assert plain.post("https://example.com/v1/other", content=b"{}").text == "plain"
print("ok  non-AI request untouched")

# subcategory lever off: only that one refused, SDK's own error, no retry, nothing sent
policy["disabled"] = ["pm_decision"]
shim._sync()
before = len(hits)
try:
    with shim.category("pm_decision"):
        client.messages.create(**ARGS)
    raise AssertionError("should have been refused")
except anthropic.PermissionDeniedError as e:
    assert "turned off in procmon" in str(e), e
assert len(hits) == before, "a blocked call reached the provider"
expect(wait_for(offset + 1), category="pm_decision", status="blocked")
with shim.category("news"):
    client.messages.create(**ARGS)
assert shim.check("pm_decision") is False and shim.check("news") is True and shim.check() is True
print("ok  subcategory lever: refused with the SDK's error, never sent, siblings unaffected")

# whole-project lever off, and async
policy.update(enabled=False, disabled=[])
shim._sync()
assert shim.check() is False
try:
    asyncio.run(aclient.messages.create(**ARGS))
    raise AssertionError("should have been refused")
except anthropic.PermissionDeniedError:
    pass
print("ok  project lever: everything refused, sync and async")

# procmon goes down: last levers hold, usage waits and is delivered later
server.shutdown()
server.server_close()
assert shim._sync(timeout=0.5) is False
assert shim.check() is False
print("ok  procmon down: 'off' still holds")
policy.update(enabled=True, disabled=[])
server = start_stub()
shim._sync()
assert shim.check("anything") is True
server.shutdown()
server.server_close()
count = len(received)
with shim.category("during_outage"):
    client.messages.create(**ARGS)
assert shim._sync(timeout=0.5) is False
server = start_stub()
shim._sync()
assert any(e["category"] == "during_outage" for e in received[count:]), received[count:]
print("ok  procmon down: 'on' keeps working, usage delivered when it returns")

# a cached "off" survives an app restart with procmon down: a new interpreter must read it from disk
policy["enabled"] = False
shim._sync()
assert json.load(open(shim._cache_path()))["enabled"] is False
print("ok  'off' saved to disk at", shim._cache_path())
print("ALL PASSED")
os.remove(shim._cache_path())
