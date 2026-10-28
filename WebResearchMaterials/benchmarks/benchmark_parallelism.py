"""Đo thông lượng Ollama hiện có với 1, 2 và 4 yêu cầu ngắn."""

from concurrent.futures import ThreadPoolExecutor
import json
import statistics
import time
from pathlib import Path

import httpx


URL = "http://127.0.0.1:11435/api/generate"
PAYLOAD = {
    "model": "qwen3:4b",
    "prompt": "Return exactly this JSON object: {\"ok\":true}",
    "format": "json",
    "stream": False,
    "think": False,
    "keep_alive": "10m",
    "options": {"temperature": 0, "num_predict": 24, "num_ctx": 512},
}


def one_call(client):
    started = time.perf_counter()
    response = client.post(URL, json=PAYLOAD)
    response.raise_for_status()
    payload = response.json()
    return {
        "seconds": round(time.perf_counter() - started, 3),
        "eval_count": payload.get("eval_count"),
        "prompt_eval_count": payload.get("prompt_eval_count"),
    }


def main():
    report = []
    with httpx.Client(timeout=180, limits=httpx.Limits(max_connections=4)) as client:
        for count in (1, 2, 4):
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=count) as executor:
                calls = list(executor.map(lambda _: one_call(client), range(count)))
            wall = time.perf_counter() - started
            result = {
                "parallelism": count, "wall_seconds": round(wall, 3),
                "throughput_per_minute": round(60 * count / wall, 2),
                "median_request_seconds": statistics.median(item["seconds"] for item in calls),
                "p95_request_seconds": max(item["seconds"] for item in calls),
                "calls": calls,
            }
            report.append(result)
            print(json.dumps(result), flush=True)
    Path("logs/ai_parallelism.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
