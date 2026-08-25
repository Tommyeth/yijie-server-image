#!/usr/bin/env python3
"""Small, local-only HTTP bridge for one b11c768 KataGo analysis process."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


MODEL = "/opt/katago/models/b11c768h12nbt3tflrs-fson-silu.bin.gz"
CONFIG = "/opt/katago/analysis.cfg"
KATAGO = "/opt/katago/bin/katago"
MAX_BODY = 2 * 1024 * 1024
MAX_BATCH = 10


def env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    return max(low, min(high, value))


MAX_CONCURRENT = env_int("YIJIE_MAX_CONCURRENT", 10, 1, MAX_BATCH)
QUERY_TIMEOUT = env_int("YIJIE_QUERY_TIMEOUT", 45, 5, 300)
MAX_SEARCH_SECONDS = env_int("YIJIE_MAX_SEARCH_SECONDS", 30, 1, 120)
QUEUE_TIMEOUT = env_int("YIJIE_QUEUE_TIMEOUT", 15, 1, 120)
DEFAULT_MAX_VISITS = env_int("YIJIE_DEFAULT_MAX_VISITS", 1000, 1, 5000)
MAX_VISITS = 5000


class BadQuery(ValueError):
    pass


class Busy(RuntimeError):
    pass


class EngineUnavailable(RuntimeError):
    pass


def is_final_engine_response(value: Any) -> bool:
    """Return true only for a terminal KataGo analysis or error response.

    KataGo may emit warning-only JSON objects with the same query id before the
    real analysis. Treating those as terminal drops the subsequent result.
    """
    if not isinstance(value, dict) or value.get("isDuringSearch") is True:
        return False
    return "error" in value or "rootInfo" in value or "moveInfos" in value


def normalize_query(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BadQuery("position must be a JSON object")
    query_value = dict(value)
    query_value.pop("id", None)
    query_value.pop("reportDuringSearchEvery", None)
    query_value.pop("overrideSettings", None)
    query_value.pop("maxPlayouts", None)

    for key in ("boardXSize", "boardYSize"):
        size = query_value.get(key)
        if isinstance(size, bool) or not isinstance(size, int) or size not in (9, 13, 19):
            raise BadQuery(f"{key} must be 9, 13, or 19")
    if not isinstance(query_value.get("moves"), list):
        raise BadQuery("moves must be an array")
    if len(query_value["moves"]) > 1000:
        raise BadQuery("moves is too long")
    if "initialStones" in query_value and not isinstance(query_value["initialStones"], list):
        raise BadQuery("initialStones must be an array")
    if "komi" not in query_value or isinstance(query_value["komi"], bool) \
            or not isinstance(query_value["komi"], (int, float)):
        raise BadQuery("komi must be a number")
    if "rules" not in query_value:
        raise BadQuery("rules is required")

    max_time = query_value.get("maxTime", 5.0)
    if isinstance(max_time, bool) or not isinstance(max_time, (int, float)) or max_time <= 0:
        raise BadQuery("maxTime must be a positive number")
    query_value["maxTime"] = min(float(max_time), float(MAX_SEARCH_SECONDS))
    max_visits = query_value.get("maxVisits", DEFAULT_MAX_VISITS)
    if isinstance(max_visits, bool) or not isinstance(max_visits, int) or max_visits <= 0:
        raise BadQuery("maxVisits must be a positive integer")
    query_value["maxVisits"] = min(max_visits, MAX_VISITS)
    return query_value


class Engine:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._slots = threading.BoundedSemaphore(capacity)
        self._active_lock = threading.Lock()
        self._active = 0
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._process: subprocess.Popen[str] | None = None
        self._started_at = 0.0
        self._ready = threading.Event()
        self._warmup_lock = threading.Lock()
        self._recovery_scheduled = False
        self._stopping = False

    def start(self) -> None:
        with self._state_lock:
            if self._stopping:
                raise EngineUnavailable("KataGo bridge is stopping")
            if self._process is not None and self._process.poll() is None:
                return
            self._ready.clear()
            command = [KATAGO, "analysis", "-config", CONFIG, "-model", MODEL]
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._process = process
            self._started_at = time.monotonic()
            threading.Thread(target=self._read_stdout, args=(process,), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
            print(f"KataGo started pid={process.pid} capacity={self._capacity}", file=sys.stderr)

    def stop(self) -> None:
        with self._state_lock:
            self._stopping = True
            self._ready.clear()
            process = self._process
            self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def alive(self) -> bool:
        with self._state_lock:
            return self._process is not None and self._process.poll() is None

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            process = self._process
            alive = process is not None and process.poll() is None
            ready = alive and self._ready.is_set()
            pid = process.pid if alive else None
            uptime = int(time.monotonic() - self._started_at) if alive else 0
        with self._active_lock:
            active = self._active
        return {
            "ok": ready,
            "processAlive": alive,
            "engine": "katago-trt",
            "model": "b11c768h12nbt3tflrs-fson-silu",
            "capacity": self._capacity,
            "activeSearches": active,
            "availableSlots": max(0, self._capacity - active),
            "pid": pid,
            "uptimeSeconds": uptime,
        }

    def query(
            self,
            raw_query: Any,
            timeout: int = QUERY_TIMEOUT,
            _allow_unready: bool = False,
    ) -> dict[str, Any]:
        query_value = normalize_query(raw_query)
        self.start()
        if not _allow_unready:
            self._schedule_recovery()
            if not self._ready.wait(timeout=timeout):
                raise EngineUnavailable("KataGo is not ready")
        if not self._slots.acquire(timeout=QUEUE_TIMEOUT):
            raise Busy("all analysis slots are busy")
        with self._active_lock:
            self._active += 1
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        query_id = uuid.uuid4().hex
        query_value["id"] = query_id
        try:
            with self._pending_lock:
                self._pending[query_id] = response_queue
            raw = json.dumps(query_value, separators=(",", ":"), ensure_ascii=False)
            with self._write_lock:
                process = self._process
                if process is None or process.poll() is not None or process.stdin is None:
                    raise EngineUnavailable("KataGo is not running")
                process.stdin.write(raw + "\n")
                process.stdin.flush()
            try:
                response = response_queue.get(timeout=timeout)
            except queue.Empty as exc:
                raise EngineUnavailable("KataGo query timed out") from exc
            if bridge_error := response.get("_bridgeError"):
                raise EngineUnavailable(str(bridge_error))
            return response
        except (BrokenPipeError, OSError) as exc:
            raise EngineUnavailable("lost connection to KataGo") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(query_id, None)
            with self._active_lock:
                self._active -= 1
            self._slots.release()

    def warmup(self) -> None:
        with self._warmup_lock:
            if self._ready.is_set() and self.alive():
                return
            self.start()
            result = self.query({
                "boardXSize": 9,
                "boardYSize": 9,
                "rules": "chinese",
                "komi": 7.5,
                "moves": [],
                "analyzeTurns": [0],
                "maxVisits": 1,
                "maxTime": 1.0,
            }, timeout=900, _allow_unready=True)
            if result.get("error"):
                raise EngineUnavailable(str(result["error"]))
            with self._state_lock:
                if self._process is None or self._process.poll() is not None:
                    raise EngineUnavailable("KataGo exited during warm-up")
                self._ready.set()
            print("KataGo warm-up complete", file=sys.stderr)

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    print(f"KataGo stdout (non-JSON): {line.rstrip()}", file=sys.stderr)
                    continue
                query_id = value.get("id")
                if not isinstance(query_id, str):
                    continue
                if not is_final_engine_response(value):
                    if value.get("warning"):
                        print(f"katago warning: {value.get('warning')}", file=sys.stderr)
                    continue
                with self._pending_lock:
                    target = self._pending.get(query_id)
                if target is not None:
                    try:
                        target.put_nowait(value)
                    except queue.Full:
                        pass
        finally:
            code = process.poll()
            self._fail_pending(f"KataGo exited (code={code})")
            should_recover = False
            with self._state_lock:
                if self._process is process:
                    self._process = None
                    self._ready.clear()
                    should_recover = not self._stopping
            if should_recover:
                self._schedule_recovery()

    @staticmethod
    def _read_stderr(process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            print(f"katago: {line.rstrip()}", file=sys.stderr)

    def _fail_pending(self, message: str) -> None:
        with self._pending_lock:
            targets = list(self._pending.values())
        for target in targets:
            try:
                target.put_nowait({"_bridgeError": message})
            except queue.Full:
                pass

    def _schedule_recovery(self) -> None:
        with self._state_lock:
            if self._stopping or self._ready.is_set() or self._recovery_scheduled:
                return
            self._recovery_scheduled = True
        threading.Thread(target=self._recover, daemon=True).start()

    def _recover(self) -> None:
        try:
            delay = 1
            while True:
                with self._state_lock:
                    if self._stopping:
                        return
                try:
                    self.warmup()
                    return
                except Exception as exc:
                    print(f"KataGo recovery failed: {exc}; retrying in {delay}s", file=sys.stderr)
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
        finally:
            retry = False
            with self._state_lock:
                self._recovery_scheduled = False
                retry = not self._stopping and not self._ready.is_set()
            if retry:
                self._schedule_recovery()


ENGINE = Engine(MAX_CONCURRENT)


class Handler(BaseHTTPRequestHandler):
    server_version = "yijie-gpu/1"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        status = ENGINE.status()
        self._json(HTTPStatus.OK if status["ok"] else HTTPStatus.SERVICE_UNAVAILABLE, status)

    def do_POST(self) -> None:
        try:
            body = self._read_json()
            if self.path in ("/analyze", "/v1/analyze"):
                self._json(HTTPStatus.OK, ENGINE.query(body))
                return
            if self.path == "/v1/analyze/batch":
                positions = body.get("positions") if isinstance(body, dict) else None
                if not isinstance(positions, list) or not 1 <= len(positions) <= MAX_BATCH:
                    raise BadQuery("positions must contain between 1 and 10 items")
                started = time.monotonic()
                with ThreadPoolExecutor(max_workers=len(positions)) as pool:
                    results = list(pool.map(ENGINE.query, positions))
                self._json(HTTPStatus.OK, {
                    "results": results,
                    "elapsedMs": round((time.monotonic() - started) * 1000),
                })
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except BadQuery as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Busy as exc:
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
        except EngineUnavailable as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
        except Exception as exc:  # Keep the bridge alive; log details only server-side.
            print(f"request failed: {exc!r}", file=sys.stderr)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise BadQuery("invalid Content-Length") from exc
        if length <= 0 or length > MAX_BODY:
            raise BadQuery("request body is empty or too large")
        return json.loads(self.rfile.read(length))

    def _json(self, status: HTTPStatus, value: Any) -> None:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"http: {self.address_string()} {fmt % args}", file=sys.stderr)


def main() -> None:
    host = os.getenv("YIJIE_LISTEN_HOST", "127.0.0.1")
    port = env_int("YIJIE_LISTEN_PORT", 2718, 1, 65535)
    ENGINE.warmup()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True

    def shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"yijie GPU bridge listening on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        ENGINE.stop()


if __name__ == "__main__":
    main()
