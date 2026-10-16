"""End-to-end tests against a disposable PostgreSQL database and local PHP.

Set SQL_API_TEST_ADMIN_DSN plus SQL_API_TEST_HOST/PORT/DATABASE. Tests create
and remove their own schema and role; never point them at a production database.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid

import pandas as pd
import requests

from client.sql_api import SQLAPIError, SQLConnection


@unittest.skipUnless(os.environ.get("SQL_API_TEST_ADMIN_DSN"), "Set SQL_API_TEST_ADMIN_DSN for disposable PostgreSQL integration tests")
class APIIntegrationTests(unittest.TestCase):
    @classmethod
    def admin(cls, sql):
        return subprocess.run(
            ["psql", "-X", "--no-password", "--dbname", os.environ["SQL_API_TEST_ADMIN_DSN"], "--set", "ON_ERROR_STOP=1", "--command", sql],
            check=True, capture_output=True, text=True,
        ).stdout

    @classmethod
    def setUpClass(cls):
        if not shutil.which("php") or not shutil.which("psql"):
            raise unittest.SkipTest("Integration tests require php with pgsql and psql")
        cls.schema = "sql_api_test_" + uuid.uuid4().hex[:12]
        cls.role = cls.schema + "_reader"
        cls.password = "test'p\\a:ssword"
        cls.admin(f"""
            CREATE ROLE {cls.role} LOGIN PASSWORD $pw${cls.password}$pw$;
            CREATE SCHEMA {cls.schema};
            CREATE TABLE {cls.schema}.sample_events (
                id bigint, time timestamp, ps bigint, channels double precision[]
            );
            INSERT INTO {cls.schema}.sample_events VALUES
                (9007199254740993, '2000-01-01 17:00:00.123456', 123456789000, ARRAY[0.2,500]),
                (9007199254740994, '2000-01-01 17:00:01', 123000, ARRAY[0.3,1000]),
                (9007199254740995, '2000-01-01 17:00:02', 456000, ARRAY[0.8,2000]);
            GRANT USAGE ON SCHEMA {cls.schema} TO {cls.role};
            GRANT SELECT ON {cls.schema}.sample_events TO {cls.role};
        """)
        cls.addClassCleanup(cls.admin, f"DROP SCHEMA {cls.schema} CASCADE; DROP ROLE {cls.role};")
        directory = tempfile.TemporaryDirectory(prefix="sql-api-test-")
        cls.addClassCleanup(directory.cleanup)
        config = {
            "db_host": os.environ.get("SQL_API_TEST_HOST", "127.0.0.1"),
            "db_port": int(os.environ.get("SQL_API_TEST_PORT", "5432")),
            "db_name": os.environ.get("SQL_API_TEST_DATABASE", "postgres"),
            "allow_http_on_loopback": True,
        }
        config_file = Path(directory.name) / "config.php"
        encoded = base64.b64encode(json.dumps(config).encode()).decode()
        config_file.write_text(f"<?php return json_decode(base64_decode('{encoded}'), true);\n")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        log = open(Path(directory.name) / "php.log", "w+")
        cls.addClassCleanup(log.close)
        environment = dict(os.environ, SQL_API_CONFIG=str(config_file))
        root = Path(__file__).resolve().parents[1]
        cls.server = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{port}", "-t", str(root / "server/public")],
            env=environment, stdout=log, stderr=log,
        )

        def stop_server():
            cls.server.terminate()
            cls.server.wait(timeout=10)

        cls.addClassCleanup(stop_server)
        cls.url = f"http://127.0.0.1:{port}/query.php"
        for _ in range(100):
            if cls.server.poll() is not None:
                log.seek(0)
                raise RuntimeError("PHP test server failed to start: " + log.read())
            try:
                requests.get(cls.url, timeout=0.2)
                break
            except requests.ConnectionError:
                time.sleep(0.05)
        else:
            raise RuntimeError("PHP test server did not start")

    def setUp(self):
        self.con = SQLConnection(self.url, self.role, self.password, allow_local_http=True)
        self.addCleanup(self.con.close)

    def test_event_query_and_high_resolution_timestamps(self):
        query = f"""
            SELECT * FROM {self.schema}.sample_events
            WHERE channels[1] > 0.25 AND channels[1] < 1 AND channels[2] > 0
            AND time BETWEEN '2000-01-01 17:00:00' AND '2000-01-01 17:00:02'
            ORDER BY time;
        """
        frame = self.con.read_sql(query, index_col="time")
        self.assertEqual(list(frame["id"]), [9007199254740994, 9007199254740995])
        self.assertEqual(frame["channels"].iloc[0], [0.3, 1000.0])
        stamps = frame.index.floor("s") + pd.to_timedelta(frame["ps"] // 1000, unit="ns")
        self.assertEqual(stamps.iloc[0], pd.Timestamp("2000-01-01 17:00:01.000000123"))

    def test_postgresql_histogram(self):
        query = f"""
            SELECT width_bucket(channels[1], 0, 1, 128) AS psp_bin,
                   width_bucket(channels[2], 0, 4000, 512) AS energy_bin,
                   COUNT(*) AS count
            FROM {self.schema}.sample_events
            WHERE time BETWEEN $1::timestamp AND $2::timestamp
            GROUP BY psp_bin, energy_bin ORDER BY psp_bin, energy_bin;
        """
        frame = self.con.read_sql(query, params=["2000-01-01 17:00:00", "2000-01-01 17:00:03"])
        self.assertEqual(frame.to_dict("list"), {"psp_bin": [26, 39, 103], "energy_bin": [65, 129, 257], "count": [1, 1, 1]})
        self.assertTrue(all(dtype == "int64" for dtype in frame.dtypes))

    def test_arbitrary_cte_parameters_nulls_arrays_and_json(self):
        frame = self.con.read_sql("""
            WITH data AS (SELECT $1::bigint AS n, $2::text AS label)
            SELECT n, label, $3::boolean AS flag, NULL::bigint AS missing,
                   ARRAY['a,b', 'NULL', NULL, 'a"b'] AS names,
                   '{"a":[1,2]}'::jsonb AS json, '\\x00ff'::bytea AS bytes
            FROM data
        """, params=[9223372036854775807, "O'Reilly", False])
        self.assertEqual(frame["n"].iloc[0], 9223372036854775807)
        self.assertEqual(frame["label"].iloc[0], "O'Reilly")
        self.assertFalse(frame["flag"].iloc[0])
        self.assertTrue(pd.isna(frame["missing"].iloc[0]))
        self.assertEqual(frame["names"].iloc[0], ["a,b", "NULL", None, 'a"b'])
        self.assertEqual(frame["json"].iloc[0], {"a": [1, 2]})
        self.assertEqual(frame["bytes"].iloc[0], b"\x00\xff")

    def test_empty_result_and_chunked_download(self):
        frame = self.con.read_sql(f"SELECT * FROM {self.schema}.sample_events WHERE false", index_col="time")
        self.assertTrue(frame.empty)
        self.assertIsInstance(frame.index, pd.DatetimeIndex)
        chunks = list(self.con.read_sql("SELECT generate_series(1, 10003)::bigint AS id", chunksize=4000))
        self.assertEqual([len(chunk) for chunk in chunks], [4000, 4000, 2003])
        self.assertEqual(chunks[-1]["id"].iloc[-1], 10003)

    def test_postgresql_permissions_reject_writes(self):
        with self.assertRaises(SQLAPIError) as caught:
            self.con.read_sql(f"DELETE FROM {self.schema}.sample_events RETURNING id")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.sqlstate, "42501")
        count = self.con.read_sql(f"SELECT COUNT(*) AS count FROM {self.schema}.sample_events")
        self.assertEqual(count["count"].iloc[0], 3)

    def test_wrong_password_is_rejected_by_postgresql(self):
        with SQLConnection(self.url, self.role, "incorrect", allow_local_http=True) as wrong:
            with self.assertRaises(SQLAPIError) as caught:
                wrong.read_sql("SELECT 1")
        self.assertEqual(caught.exception.status_code, 401)

    def test_authentication_and_request_errors(self):
        response = requests.post(self.url, json={"sql": "SELECT 1"}, timeout=5)
        self.assertEqual(response.status_code, 401)
        response = requests.get(self.url, timeout=5)
        self.assertEqual(response.status_code, 405)
        response = requests.post(self.url, data="not-json", headers={"Content-Type": "application/json"}, auth=(self.role, self.password), timeout=5)
        self.assertEqual(response.status_code, 400)

    def test_syntax_error_has_sqlstate(self):
        with self.assertRaises(SQLAPIError) as caught:
            self.con.read_sql("SELEC invalid")
        self.assertEqual(caught.exception.sqlstate, "42601")

    def test_multiple_statements_rejected_by_postgresql(self):
        with self.assertRaises(SQLAPIError):
            self.con.read_sql("SELECT 1; SELECT 2")


if __name__ == "__main__":
    unittest.main()
