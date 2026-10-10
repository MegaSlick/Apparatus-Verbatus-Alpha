"""A stand-in for `vllm serve` in the runner's tests: no GPU, no model, canned answers.

Run as `python fake_vllm_server.py serve <weights> ... --port N --served-model-name M`.
It answers `/v1/models` and `/v1/chat/completions` in vLLM's OpenAI shape, choosing a
synthetic answer by the prompt it is sent, and refuses a request without an image (except
a Perlector page prompt that says no image is shown). `"stream": true` is answered as
server-sent events, a few characters a chunk, with a final usage chunk and `[DONE]`. A
Perlector prompt containing `LOOP-TEST` is answered with one line over and over, so a
client's repetition-loop guard can be tested.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PERLECTOR_ANSWER = {
    "acts": [
        {
            "n": 1,
            "kind": "act",
            "label": "synthetic",
            "cites": ["A1"],
            "text": "Le dix mai",
            "continues_from_previous_page": False,
            "continues_to_next_page": False,
        }
    ],
    "set_aside": [],
}


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
    if "witness regime:" in joined:  # the Perlector's page prompt (common/page_prompt.py)
        if "LOOP-TEST" in joined:
            return '{"acts": [{"n": 1, "kind": "act", "text": "' + "same row\\n" * 200
        return json.dumps(PERLECTOR_ANSWER, ensure_ascii=False)
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
        chunks = [content[i : i + 7] for i in range(0, len(content), 7)]
        events = [{"choices": [{"index": 0, "delta": {"content": c}}]} for c in chunks]
        events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        events.append(
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": len(chunks)}}
        )
        try:
            for event in events:
                payload = {"model": SERVED, "object": "chat.completion.chunk", **event}
                self.wfile.write(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):  # the client stopped reading
            pass

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
        text_only_ok = "page image: not shown." in json.dumps(body["messages"])
        if body.get("model") != SERVED or not (has_image or text_only_ok):
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
    ThreadingHTTPServer(("127.0.0.1", int(_flag("--port"))), Handler).serve_forever()
