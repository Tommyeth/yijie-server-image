import importlib.util
import json
import os
from pathlib import Path
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


MODULE_PATH = Path(__file__).parents[1] / "image" / "server.py"
os.environ.setdefault("YIJIE_MAX_CONCURRENT", "10")
SPEC = importlib.util.spec_from_file_location("yijie_gpu_server", MODULE_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(SERVER)


class NormalizeQueryTests(unittest.TestCase):
    def base(self):
        return {
            "id": "client-controlled",
            "boardXSize": 19,
            "boardYSize": 19,
            "rules": "chinese",
            "komi": 7.5,
            "moves": [],
        }

    def test_removes_control_fields_and_caps_time(self):
        value = self.base()
        value.update({
            "maxTime": 999,
            "maxVisits": 999999,
            "maxPlayouts": 999999,
            "overrideSettings": {"numAnalysisThreads": 1000},
            "reportDuringSearchEvery": 0.001,
        })
        got = SERVER.normalize_query(value)
        self.assertNotIn("id", got)
        self.assertNotIn("overrideSettings", got)
        self.assertNotIn("reportDuringSearchEvery", got)
        self.assertNotIn("maxPlayouts", got)
        self.assertEqual(got["maxTime"], float(SERVER.MAX_SEARCH_SECONDS))
        self.assertEqual(got["maxVisits"], SERVER.MAX_VISITS)

    def test_accepts_supported_board_sizes(self):
        for size in (9, 13, 19):
            value = self.base()
            value["boardXSize"] = value["boardYSize"] = size
            SERVER.normalize_query(value)

    def test_rejects_bad_board_size(self):
        value = self.base()
        value["boardXSize"] = 21
        with self.assertRaises(SERVER.BadQuery):
            SERVER.normalize_query(value)

    def test_status_reports_slot_usage(self):
        engine = SERVER.Engine(4)
        status = engine.status()
        self.assertEqual(status["activeSearches"], 0)
        self.assertEqual(status["availableSlots"], 4)

    def test_only_analysis_or_error_is_a_final_engine_response(self):
        self.assertFalse(SERVER.is_final_engine_response({
            "id": "q",
            "field": "maxTime",
            "warning": "Unexpected or unused field",
        }))
        self.assertFalse(SERVER.is_final_engine_response({
            "id": "q",
            "isDuringSearch": True,
            "rootInfo": {},
        }))
        self.assertTrue(SERVER.is_final_engine_response({
            "id": "q",
            "rootInfo": {},
            "moveInfos": [],
        }))
        self.assertTrue(SERVER.is_final_engine_response({
            "id": "q",
            "error": "bad query",
        }))


class FakeEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def status(self):
        return {"ok": True, "capacity": 10}

    def query(self, value):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.03)
            return {"marker": value["marker"]}
        finally:
            with self.lock:
                self.active -= 1


class HTTPBatchTests(unittest.TestCase):
    def setUp(self):
        self.old_engine = SERVER.ENGINE
        self.engine = FakeEngine()
        SERVER.ENGINE = self.engine
        self.httpd = SERVER.ThreadingHTTPServer(("127.0.0.1", 0), SERVER.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        SERVER.ENGINE = self.old_engine

    def post_batch(self, count):
        body = json.dumps({"positions": [{"marker": i} for i in range(count)]}).encode()
        request = Request(
            self.base_url + "/v1/analyze/batch",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        return urlopen(request, timeout=3)

    def test_dispatches_ten_positions_concurrently_and_preserves_order(self):
        with self.post_batch(10) as response:
            value = json.load(response)
        self.assertEqual([item["marker"] for item in value["results"]], list(range(10)))
        self.assertEqual(self.engine.max_active, 10)

    def test_rejects_more_than_ten_positions(self):
        with self.assertRaises(HTTPError) as raised:
            self.post_batch(11)
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
