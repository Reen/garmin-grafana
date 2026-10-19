# InfluxDB 2.x support (`influxdb2` branch)

This branch of [Reen/garmin-grafana](https://github.com/Reen/garmin-grafana) adds native InfluxDB 2.x support to
[arpanghosh8453/garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana). It is kept as a small patch on top
of upstream `main`; everything else behaves exactly as described in the main [README](README.md).

- The fetcher writes with the official `influxdb-client` library and an API token (`INFLUXDB_VERSION=2`).
- Strength training rows are refreshed via the InfluxDB 2 delete API, so edited exercises don't leave duplicates.
- The CSV exporter (`influxdb_exporter.py`) works with InfluxDB 2.
- The **Grafana dashboards are unchanged**. They use InfluxQL, which InfluxDB 2.x answers for every bucket through a
  virtual database of the same name, so no DBRP mapping or v1 user is needed.

## Image

```
ghcr.io/reen/garmin-fetch-data:influxdb2              # latest build of this branch
ghcr.io/reen/garmin-fetch-data:influxdb2-<short sha>  # pinned build
```

Built for `linux/amd64` and `linux/arm64` by [`.github/workflows/influxdb2-image.yml`](.github/workflows/influxdb2-image.yml)
after the backend tests pass against a real InfluxDB 2 instance.

## Setup

1. Create a bucket and two tokens (replace `Home` with your organization):

   ```bash
   influx bucket create -o Home -n GarminStats -r 0
   BUCKET_ID=$(influx bucket list -o Home -n GarminStats --hide-headers | cut -f1)
   # fetcher: reads the last sync time, writes data, deletes stale strength rows
   influx auth create -o Home --read-bucket $BUCKET_ID --write-bucket $BUCKET_ID -d garmin-fetch-data
   # Grafana: read only
   influx auth create -o Home --read-bucket $BUCKET_ID -d grafana-garmin
   ```

2. Configure the fetcher:

   ```yaml
   garmin-fetch-data:
     image: ghcr.io/reen/garmin-fetch-data:influxdb2
     volumes:
       - ./garminconnect-tokens:/home/appuser/.garminconnect # chown 1000:1000
     environment:
       - INFLUXDB_VERSION=2
       - INFLUXDB_HOST=influxdb
       - INFLUXDB_PORT=8086
       - INFLUXDB_ORG=Home
       - INFLUXDB_DATABASE=GarminStats            # the bucket name
       - INFLUXDB_V2_ACCESS_TOKEN=<fetcher token>
       # all other variables as in compose-example.yml
   ```

   `INFLUXDB_USERNAME`, `INFLUXDB_PASSWORD` and `INFLUXDB_V3_ACCESS_TOKEN` are not used with `INFLUXDB_VERSION=2`.
   Set `INFLUXDB_ENDPOINT_IS_HTTP=false` to connect over HTTPS.

3. Add the Grafana datasource, either by provisioning
   [`Grafana_Datasource/influxdb2.yaml`](Grafana_Datasource/influxdb2.yaml) or in the UI: type *InfluxDB*, query language
   *InfluxQL*, database `GarminStats`, no user/password, and a custom HTTP header `Authorization` with the value
   `Token <grafana token>`. Then import dashboard `23245` as described in the main README.

## Maintaining the branch

`main` mirrors upstream (use *Sync fork* on GitHub, never commit to it). `influxdb2` is rebased onto it:

```bash
git fetch upstream
git checkout influxdb2
git rebase upstream/main
git push --force-with-lease origin influxdb2
```

The patch touches upstream files only in a few places (`garmin_fetch.py`, `influxdb_exporter.py`, `pyproject.toml`,
`uv.lock`); everything else lives in new files. If `uv.lock` conflicts, take upstream's version and regenerate it:

```bash
git checkout upstream/main -- uv.lock
uv lock   # re-adds influxdb-client from pyproject.toml
git add uv.lock && git rebase --continue
```

Run the tests locally against a disposable InfluxDB 2:

```bash
docker run -d --rm --name influx2-test -p 8086:8086 \
  -e DOCKER_INFLUXDB_INIT_MODE=setup -e DOCKER_INFLUXDB_INIT_USERNAME=admin \
  -e DOCKER_INFLUXDB_INIT_PASSWORD=adminpassword -e DOCKER_INFLUXDB_INIT_ORG=test \
  -e DOCKER_INFLUXDB_INIT_BUCKET=GarminStats -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=test-token influxdb:2
INFLUXDB2_TEST_URL=http://localhost:8086 INFLUXDB2_TEST_TOKEN=test-token \
  uv run --locked python -m unittest discover -s tests -v
```
