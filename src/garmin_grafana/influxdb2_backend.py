"""InfluxDB 2.x backend for garmin-grafana.

Wraps the official ``influxdb-client`` library behind the small subset of the
InfluxDB 1.x ``InfluxDBClient`` interface that garmin_fetch.py relies on
(``write_points`` and ``delete_series``), so the fetcher only needs a third
branch at client creation time. Errors are re-raised as the 1.x
``InfluxDBClientError`` so the existing ``except`` clauses keep working.

Data is written to the bucket named by INFLUXDB_DATABASE. The Grafana
dashboards keep using InfluxQL: InfluxDB 2.x exposes every bucket as a
virtual InfluxQL database of the same name, so no DBRP mapping is needed.
"""
from datetime import datetime, timezone

from influxdb.exceptions import InfluxDBClientError
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException
from urllib3.exceptions import HTTPError

_EPOCH = "1970-01-01T00:00:00Z"


def _quote(value):
    """Quote a value as a string literal for Flux queries and delete predicates."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _wrap(err):
    if isinstance(err, ApiException):
        return InfluxDBClientError(f"{err.status} {err.reason}: {err.body}", err.status)
    return InfluxDBClientError(str(err))


class InfluxDB2Client:
    def __init__(self, url, token, org, bucket, verify_ssl=True, timeout_ms=60_000):
        self.org = org
        self.bucket = bucket
        self._client = InfluxDBClient(url=url, token=token, org=org, verify_ssl=verify_ssl, timeout=timeout_ms)
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    def write_points(self, points):
        """Write a list of 1.x-style point dicts (measurement, time, tags, fields)."""
        try:
            self._write_api.write(bucket=self.bucket, org=self.org, record=points)
        except (ApiException, HTTPError) as err:
            raise _wrap(err) from err
        return True

    def delete_series(self, measurement, tags=None):
        """Delete all points of a measurement matching the given tags."""
        predicate = f"_measurement={_quote(measurement)}"
        for key, value in (tags or {}).items():
            predicate += f" AND {key}={_quote(value)}"
        try:
            self._client.delete_api().delete(
                start=_EPOCH,
                stop=datetime.now(timezone.utc),
                predicate=predicate,
                bucket=self.bucket,
                org=self.org,
            )
        except (ApiException, HTTPError) as err:
            raise _wrap(err) from err

    def last_point_time(self, measurement):
        """Return the timestamp (UTC datetime) of the newest point of a measurement, or None."""
        query = (
            f"from(bucket: {_quote(self.bucket)})"
            f" |> range(start: 0)"
            f" |> filter(fn: (r) => r._measurement == {_quote(measurement)})"
            f" |> last()"
        )
        try:
            tables = self._client.query_api().query(query, org=self.org)
        except (ApiException, HTTPError) as err:
            raise _wrap(err) from err
        times = [record.get_time() for table in tables for record in table.records]
        return max(times) if times else None

    def measurements(self, start, stop):
        """List the measurements that have data between start and stop."""
        query = (
            'import "influxdata/influxdb/schema"\n'
            f"schema.measurements(bucket: {_quote(self.bucket)},"
            f" start: {start.isoformat()}, stop: {stop.isoformat()})"
        )
        try:
            tables = self._client.query_api().query(query, org=self.org)
        except (ApiException, HTTPError) as err:
            raise _wrap(err) from err
        return [record.get_value() for table in tables for record in table.records]

    def query_measurement_frame(self, measurement, start, stop):
        """Return all points of a measurement as a DataFrame with one column per tag and field."""
        query = (
            f"from(bucket: {_quote(self.bucket)})"
            f" |> range(start: {start.isoformat()}, stop: {stop.isoformat()})"
            f" |> filter(fn: (r) => r._measurement == {_quote(measurement)})"
            f' |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")'
            f" |> group()"
            f' |> sort(columns: ["_time"])'
        )
        try:
            df = self._client.query_api().query_data_frame(query, org=self.org)
        except (ApiException, HTTPError) as err:
            raise _wrap(err) from err
        if isinstance(df, list):  # one frame per table shape
            import pandas as pd
            df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
        if df.empty:
            return df
        df = df.drop(columns=["result", "table", "_start", "_stop", "_measurement"], errors="ignore")
        df = df.rename(columns={"_time": "time"})
        df["time"] = df["time"].map(lambda t: t.isoformat().replace("+00:00", "Z"))  # RFC3339, like the 1.x client
        return df

    def close(self):
        self._client.close()
