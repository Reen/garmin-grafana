"""Integration tests for the InfluxDB 2.x backend.

Run against a disposable InfluxDB 2 instance:

    INFLUXDB2_TEST_URL=http://localhost:8086 INFLUXDB2_TEST_TOKEN=... \
    INFLUXDB2_TEST_ORG=test INFLUXDB2_TEST_BUCKET=GarminStats \
    uv run python -m unittest discover -s tests -v

The tests are skipped when INFLUXDB2_TEST_URL is not set.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import pytz
import requests
from influxdb.exceptions import InfluxDBClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "garmin_grafana"))
from influxdb2_backend import InfluxDB2Client  # noqa: E402

URL = os.getenv("INFLUXDB2_TEST_URL")
TOKEN = os.getenv("INFLUXDB2_TEST_TOKEN", "")
ORG = os.getenv("INFLUXDB2_TEST_ORG", "test")
BUCKET = os.getenv("INFLUXDB2_TEST_BUCKET", "GarminStats")


@unittest.skipUnless(URL, "INFLUXDB2_TEST_URL not set")
class InfluxDB2BackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = InfluxDB2Client(url=URL, token=TOKEN, org=ORG, bucket=BUCKET)
        cls.base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def influxql(self, query):
        """Run an InfluxQL query through the 1.x compatibility API, the way Grafana does."""
        resp = requests.get(
            f"{URL}/query",
            params={"db": BUCKET, "q": query},
            headers={"Authorization": f"Token {TOKEN}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["results"][0]

    def test_write_points_in_fetcher_format(self):
        # Shapes produced by garmin_fetch.py: pytz-localized isoformat() with
        # microseconds, plain isoformat(timespec='seconds'), None values that
        # Garmin leaves empty, and mixed field types.
        points = [
            {
                "measurement": "HeartRateIntraday",
                "time": pytz.utc.localize(self.base.replace(tzinfo=None) + timedelta(microseconds=250000)).isoformat(),
                "tags": {"Device": "Forerunner 965", "Database_Name": BUCKET},
                "fields": {"HeartRate": 61},
            },
            {
                "measurement": "HeartRateIntraday",
                "time": (self.base + timedelta(minutes=2)).isoformat(timespec="seconds"),
                "tags": {"Device": "Forerunner 965", "Database_Name": BUCKET},
                "fields": {"HeartRate": 64},
            },
            {
                "measurement": "DailyStats",
                "time": self.base.isoformat(),
                "tags": {"Device": "Forerunner 965", "Database_Name": BUCKET, "Unset": None},
                "fields": {
                    "totalSteps": 12345,
                    "totalDistanceMeters": 9876.5,
                    "isVigorousDay": True,
                    "sleepStatus": "GOOD",
                    "floorsAscended": None,
                },
            },
        ]
        self.client.write_points(points)

        self.assertEqual(
            self.client.last_point_time("HeartRateIntraday"),
            self.base + timedelta(minutes=2),
        )

        frame = self.client.query_measurement_frame("DailyStats", self.base - timedelta(minutes=1), self.base + timedelta(minutes=1))
        self.assertEqual(len(frame), 1)
        row = frame.iloc[0]
        self.assertEqual(row["totalSteps"], 12345)
        self.assertAlmostEqual(row["totalDistanceMeters"], 9876.5)
        self.assertEqual(bool(row["isVigorousDay"]), True)
        self.assertEqual(row["sleepStatus"], "GOOD")
        self.assertNotIn("floorsAscended", frame.columns)
        self.assertNotIn("Unset", frame.columns)

    def test_point_without_fields_is_ignored(self):
        self.client.write_points([
            {
                "measurement": "EmptyPoint",
                "time": self.base.isoformat(),
                "tags": {"Device": "x"},
                "fields": {"onlyNone": None},
            }
        ])
        self.assertIsNone(self.client.last_point_time("EmptyPoint"))

    def test_last_point_time_of_missing_measurement(self):
        self.assertIsNone(self.client.last_point_time("DoesNotExist"))

    def test_delete_series_only_removes_matching_tags(self):
        t = self.base + timedelta(minutes=10)
        self.client.write_points([
            {"measurement": "StrengthExerciseSet", "time": t.isoformat(),
             "tags": {"ActivityID": "111", "Exercise": "SQUAT"}, "fields": {"reps": 5}},
            {"measurement": "StrengthExerciseSet", "time": t.isoformat(),
             "tags": {"ActivityID": "222", "Exercise": "BENCH"}, "fields": {"reps": 8}},
        ])
        self.client.delete_series(measurement="StrengthExerciseSet", tags={"ActivityID": "111"})

        frame = self.client.query_measurement_frame("StrengthExerciseSet", t - timedelta(minutes=1), t + timedelta(minutes=1))
        self.assertEqual(sorted(frame["ActivityID"]), ["222"])

    def test_measurements(self):
        self.client.write_points([
            {"measurement": "SleepSummary", "time": self.base.isoformat(), "tags": {}, "fields": {"sleepScore": 80}},
        ])
        found = self.client.measurements(self.base - timedelta(minutes=1), self.base + timedelta(minutes=1))
        self.assertIn("SleepSummary", found)

    def test_bucket_is_queryable_with_influxql(self):
        # The Grafana dashboards query the bucket as an InfluxQL database of the same name.
        self.client.write_points([
            {"measurement": "BodyBatteryIntraday", "time": self.base.isoformat(), "tags": {}, "fields": {"BodyBatteryLevel": 42}},
        ])
        result = self.influxql("SELECT last(BodyBatteryLevel) FROM BodyBatteryIntraday")
        self.assertEqual(result["series"][0]["values"][0][1], 42)

    def test_errors_are_raised_as_influxdb_client_error(self):
        bad = InfluxDB2Client(url=URL, token="invalid", org=ORG, bucket=BUCKET)
        try:
            with self.assertRaises(InfluxDBClientError) as ctx:
                bad.write_points([{"measurement": "X", "time": self.base.isoformat(), "tags": {}, "fields": {"v": 1}}])
            self.assertEqual(ctx.exception.code, 401)
        finally:
            bad.close()


if __name__ == "__main__":
    unittest.main()
