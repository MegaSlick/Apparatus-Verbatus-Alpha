"""A stand-in for `vllm serve` in the runner's tests: no GPU, no model, canned answers.

Run as `python fake_vllm_server.py serve <weights> ... --port N --served-model-name M`.
It answers `/v1/models` and `/v1/chat/completions` in vLLM's OpenAI shape, choosing a
synthetic answer by the prompt it is sent, and refuses a request without an image. A
request with `"stream": true` is answered as server-sent events, one line per chunk.
Two prompts stand for a misbehaving page: one containing `LOOP` repeats one line a
thousand times, one containing `HANG` waits 30 s before answering. With
`FAKE_VLLM_LAUNCHES` set, each start appends a line to that file, so a test can count
model loads.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _flag(name: str) -> str:
    return sys.argv[sys.argv.index(name) + 1]


SERVED = _flag("--served-model-name")


def answer(body: dict) -> str:
    texts = [
        part.get("text", "")
        for message in body["messages"]
        for part in (message["content"] if isinstance(message["content"], list) else [])
        if part.get("type") == "text"
    ]
    joined = " ".join(texts)
    if "LOOP" in joined:
        return "Le dix mai\n" * 1000
    if "HANG" in joined:
        time.sleep(30)
    if joined.startswith("OCR this image"):
        return '<div data-bbox="10 10 990 990" data-label="Text"><p>Le dix mai</p></div>'
    if "Please output the layout information" in joined:  # dots.mocr's layout prompt
        return json.dumps(
            [
                {"bbox": [10, 10, 390, 40], "category": "Page-header", "text": "Folio 1"},
                {"bbox": [10, 50, 390, 120], "category": "Text", "text": "Le dix mai"},
            ]
        )
    if "historical document" in joined:
        return (
            "<HistoricalDocument><Page><Body><Line>Le dix mai</Line></Body></Page>"
            "</HistoricalDocument>"
        )
    return "Le dix mai"  # DAI and the plain-prompt arm


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep the test output quiet
        pass

    def _send(self, status: int, value: dict) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, content: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        pieces = [line + "\n" for line in content.split("\n")]
        pieces[-1] = pieces[-1][:-1]
        chunks = [{"delta": {"content": piece}, "finish_reason": None} for piece in pieces]
        chunks.append({"delta": {}, "finish_reason": "stop"})
        try:
            for chunk in chunks:
                event = {"model": SERVED, "choices": [{"index": 0, **chunk}]}
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            usage = {"prompt_tokens": 100, "completion_tokens": len(pieces), "total_tokens": 0}
            self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client abandoned the reply, as on a loop

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/models":
            self._send(200, {"data": [{"id": SERVED}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        has_image = any(
            part.get("type") == "image_url"
            for message in body["messages"]
            for part in (message["content"] if isinstance(message["content"], list) else [])
        )
        if body.get("model") != SERVED or not has_image:
            self._send(400, {"error": "wrong model or no image"})
            return
        content = answer(body)
        if body.get("stream"):
            self._stream(content)
            return
        self._send(
            200,
            {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
            },
        )


if __name__ == "__main__":
    if os.environ.get("FAKE_VLLM_LAUNCHES"):
        with open(os.environ["FAKE_VLLM_LAUNCHES"], "a", encoding="utf-8") as launches:
            launches.write(f"{os.getpid()}\n")
    ThreadingHTTPServer(("127.0.0.1", int(_flag("--port"))), Handler).serve_forever()
