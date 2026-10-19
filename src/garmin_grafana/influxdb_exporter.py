import os
import argparse
import zipfile
import io
from influxdb import InfluxDBClient
from influxdb_client_3 import InfluxDBClient3, InfluxDBError
import pandas as pd
from datetime import datetime, timedelta, timezone


parser = argparse.ArgumentParser(description="Export InfluxDB measurements to individual CSVs in a ZIP archive")
parser.add_argument("--last-n-days", type=int, default=None, help="Query data from the last N days (overrides date range)")
parser.add_argument("--start-date", type=str, default=None, help="Start date in YYYY-MM-DD")
parser.add_argument("--end-date", type=str, default=None, help="End date in YYYY-MM-DD (defaults to today)")
args = parser.parse_args()


if args.last_n_days is not None:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=args.last_n_days)
    time_label = f"Last{args.last_n_days}Days"
else:
    today = datetime.now(timezone.utc).date()
    start_date_str = args.start_date or str(today - timedelta(days=30))
    end_date_str = args.end_date or str(today)

    try:
        start_time = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_time = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(f"Invalid date format: {e}")

    if start_time > end_time:
        raise ValueError("Start date must be before end date.")

    time_label = f"{start_time.date()}_to_{end_time.date()}"

time_clause = f"time >= '{start_time.isoformat()}' AND time <= '{end_time.isoformat()}'"
timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
zip_filename = f"/tmp/GarminStats_Export_{timestamp_str}_{time_label}.zip"


INFLUXDB_VERSION = os.getenv("INFLUXDB_VERSION",'1') # Your influxdb database version (accepted values are '1', '2' or '3')
assert INFLUXDB_VERSION in ['1','2','3'], "Only InfluxDB version 1, 2 or 3 is allowed - please ensure to set this value to either 1, 2 or 3"
INFLUXDB_HOST = os.getenv("INFLUXDB_HOST", "your.influxdb.hostname")
INFLUXDB_PORT = int(os.getenv("INFLUXDB_PORT", 8086))
INFLUXDB_USERNAME = os.getenv("INFLUXDB_USERNAME", "influxdb_username")
INFLUXDB_PASSWORD = os.getenv("INFLUXDB_PASSWORD", "influxdb_access_password")
INFLUXDB_DATABASE = os.getenv("INFLUXDB_DATABASE", "GarminStats")
INFLUXDB_V3_ACCESS_TOKEN = os.getenv("INFLUXDB_V3_ACCESS_TOKEN",'') # InfluxDB V3 Access token, required only for InfluxDB V3
INFLUXDB_V2_ACCESS_TOKEN = os.getenv("INFLUXDB_V2_ACCESS_TOKEN",'') # InfluxDB V2 API token, required only for InfluxDB V2
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", 'default') # required only for InfluxDB V2
INFLUXDB_ENDPOINT_IS_HTTP = False if os.getenv("INFLUXDB_ENDPOINT_IS_HTTP") in ['False','false','FALSE','f','F','no','No','NO','0'] else True # optional


if INFLUXDB_VERSION == '2':
    try:
        from influxdb2_backend import InfluxDB2Client
    except ImportError:
        from .influxdb2_backend import InfluxDB2Client
    influxdbclient = InfluxDB2Client(
    url=f"{'http' if INFLUXDB_ENDPOINT_IS_HTTP else 'https'}://{INFLUXDB_HOST}:{INFLUXDB_PORT}",
    token=INFLUXDB_V2_ACCESS_TOKEN,
    org=INFLUXDB_ORG,
    bucket=INFLUXDB_DATABASE
    )
elif INFLUXDB_ENDPOINT_IS_HTTP:
    if INFLUXDB_VERSION == '1':
        influxdbclient = InfluxDBClient(host=INFLUXDB_HOST, port=INFLUXDB_PORT, username=INFLUXDB_USERNAME, password=INFLUXDB_PASSWORD)
        influxdbclient.switch_database(INFLUXDB_DATABASE)
    else:
        influxdbclient = InfluxDBClient3(
        host=f"http://{INFLUXDB_HOST}:{INFLUXDB_PORT}",
        token=INFLUXDB_V3_ACCESS_TOKEN,
        database=INFLUXDB_DATABASE
        )
else:
    if INFLUXDB_VERSION == '1':
        influxdbclient = InfluxDBClient(host=INFLUXDB_HOST, port=INFLUXDB_PORT, username=INFLUXDB_USERNAME, password=INFLUXDB_PASSWORD, ssl=True, verify_ssl=True)
        influxdbclient.switch_database(INFLUXDB_DATABASE)
    else:
        influxdbclient = InfluxDBClient3(
        host=f"https://{INFLUXDB_HOST}:{INFLUXDB_PORT}",
        token=INFLUXDB_V3_ACCESS_TOKEN,
        database=INFLUXDB_DATABASE
        )

# --- Measurement exclusion list ---
excluded_measurements = {"%", "DemoPoint", "DeviceSync"}

# --- Fetch all measurements ---
measurements_query = "SHOW MEASUREMENTS"
if INFLUXDB_VERSION == "1":
    measurements = [m["name"] for m in influxdbclient.query(measurements_query).get_points()]
elif INFLUXDB_VERSION == "2":
    measurements = influxdbclient.measurements(start_time, end_time)
else:
    measurements = [m["name"] for m in influxdbclient.query(measurements_query, language="influxql").get_points()]

print(f"Found {len(measurements)} measurements. Skipping: {excluded_measurements}")

files_written = 0

with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
    for measurement in measurements:
        if measurement in excluded_measurements:
            print(f" !! Skipping: {measurement}")
            continue

        print(f" >> Querying: {measurement}")
        query = f'SELECT * FROM "{measurement}" WHERE {time_clause}'

        try:
            
            if INFLUXDB_VERSION == "2":
                df = influxdbclient.query_measurement_frame(measurement, start_time, end_time)
            else:
                if INFLUXDB_VERSION == "1":
                    result = influxdbclient.query(query)
                else:
                    result = influxdbclient.query(query, language="influxql")
                df = pd.DataFrame(list(result.get_points()))

            if df.empty:
                print(" -- ⚠️ No data within given period.")
                continue

            df.insert(0, "measurement", measurement)

            # Write to an in-memory CSV and add to ZIP
            csv_buffer = io.StringIO()
            df.to_csv(csv_buffer, index=False)
            csv_bytes = csv_buffer.getvalue().encode("utf-8")
            zipf.writestr(f"{measurement}.csv", csv_bytes)
            files_written += 1

        except Exception as e:
            print(f"  ❌ Query failed for {measurement}: {e}")

if files_written:
    print(f"\n✅ Exported {files_written} measurement CSVs into {zip_filename}. ")
else:
    print("⚠️ No data collected or exported.")
