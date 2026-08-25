"""Run one real b11c768 position on Modal and report cold-start timings."""

from __future__ import annotations

import json
import time

import modal


app = modal.App("yijie-katago-one-position")
image = modal.Image.from_name("yijie-server-image:b11c768")

POSITION = {
    "boardXSize": 19,
    "boardYSize": 19,
    "rules": "chinese",
    "komi": 7.5,
    "moves": [
        ["B", "Q16"],
        ["W", "D4"],
        ["B", "D16"],
        ["W", "Q4"],
        ["B", "C6"],
        ["W", "R14"],
    ],
    "maxVisits": 1000,
    "maxTime": 30,
}


@app.function(
    image=image,
    gpu="L4",
    timeout=15 * 60,
    min_containers=0,
    scaledown_window=2,
)
def analyze_once(position: dict) -> dict:
    import subprocess
    import urllib.error
    import urllib.request

    container_started = time.perf_counter()
    log_path = "/tmp/yijie-katago-benchmark.log"
    log_file = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        ["/opt/katago/bin/start.sh"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 12 * 60
        last_error = "not started"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log_file.flush()
                with open(log_path, encoding="utf-8", errors="replace") as source:
                    tail = source.read()[-8000:]
                raise RuntimeError(f"KataGo exited with {process.returncode}:\n{tail}")
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:2718/health", timeout=3
                ) as response:
                    health = json.loads(response.read())
                if health.get("ok"):
                    break
            except (OSError, urllib.error.URLError) as exc:
                last_error = repr(exc)
            time.sleep(1)
        else:
            raise TimeoutError(f"KataGo did not become healthy: {last_error}")

        ready_at = time.perf_counter()
        request = urllib.request.Request(
            "http://127.0.0.1:2718/v1/analyze",
            data=json.dumps(position).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        search_started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=120) as response:
            analysis = json.loads(response.read())
        search_finished = time.perf_counter()

        if analysis.get("error"):
            raise RuntimeError(f"KataGo analysis error: {analysis['error']}")

        root = analysis.get("rootInfo") or {}
        moves = analysis.get("moveInfos") or []
        return {
            "gpu": "L4",
            "engine_ready_seconds": round(ready_at - container_started, 3),
            "analysis_seconds": round(search_finished - search_started, 3),
            "function_seconds": round(search_finished - container_started, 3),
            "visits": root.get("visits"),
            "winrate": root.get("winrate"),
            "score_lead": root.get("scoreLead"),
            "best_move": moves[0].get("move") if moves else None,
            "top_moves": [item.get("move") for item in moves[:5]],
            "response_keys": sorted(analysis),
            "response_preview": json.dumps(analysis, ensure_ascii=False)[:2000],
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        log_file.close()


@app.local_entrypoint()
def main() -> None:
    submitted = time.perf_counter()
    result = analyze_once.remote(POSITION)
    result["modal_roundtrip_seconds"] = round(time.perf_counter() - submitted, 3)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
