import datetime as dt
import json
import unittest
from decimal import Decimal
from types import MappingProxyType
from unittest.mock import Mock

import pandas as pd

from client.sql_api import SQLAPIError, SQLConnection, _array, read_sql


def response_for(columns, rows, *, complete=True):
    records = [{"type": "schema", "version": 1, "columns": [
        {"name": name, "type": pg_type} for name, pg_type in columns
    ]}]
    records += [{"type": "row", "values": row} for row in rows]
    if complete:
        records.append({"type": "complete", "row_count": len(rows)})
    response = Mock(status_code=200, headers={"Content-Type": "application/x-ndjson; charset=utf-8"})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_lines.return_value = iter(json.dumps(record).encode() for record in records)
    return response


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.con = SQLConnection("https://example.test/sql-api/query.php", "reader", "secret", database="sample_database")
        self.addCleanup(self.con.close)
        self.con._session.post = Mock()

    def respond(self, columns, rows, **kwargs):
        response = response_for(columns, rows, **kwargs)
        self.con._session.post.return_value = response
        return response

    def test_event_types_and_timestamp_reconstruction(self):
        self.respond(
            [("id", "int8"), ("time", "timestamp"), ("ps", "int8"), ("channels", "_float8")],
            [["9007199254740993", "2000-01-01 17:00:00.123456", "123456789000", "{0.2,500}"],
             [None, "2000-01-01 17:00:01", "1", "{0.3,NULL}"]],
        )
        frame = read_sql("SELECT * FROM sample_events", self.con, index_col="time")
        self.assertIsInstance(frame.index, pd.DatetimeIndex)
        self.assertEqual(frame.index[0].microsecond, 123456)
        self.assertEqual(frame["id"].iloc[0], 9007199254740993)
        self.assertEqual(str(frame["id"].dtype), "Int64")
        self.assertEqual(frame["channels"].iloc[0], [0.2, 500.0])
        self.assertEqual(frame["channels"].iloc[1], [0.3, None])
        reconstructed = frame.index.floor("s") + pd.to_timedelta(frame["ps"] // 1000, unit="ns")
        self.assertEqual(reconstructed.iloc[0], pd.Timestamp("2000-01-01 17:00:00.123456789"))

    def test_histogram_columns_remain_integer(self):
        self.respond([("psp_bin", "int4"), ("energy_bin", "int4"), ("count", "int8")], [["1", "2", "123"]])
        frame = self.con.read_sql("SELECT histogram")
        self.assertTrue(all(dtype == "int64" for dtype in frame.dtypes))

    def test_empty_result_preserves_schema_and_datetime_index(self):
        self.respond([("time", "timestamp"), ("count", "int8")], [])
        frame = self.con.read_sql("SELECT empty", index_col="time")
        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), ["count"])
        self.assertIsInstance(frame.index, pd.DatetimeIndex)
        self.assertEqual(str(frame["count"].dtype), "int64")

    def test_duplicate_column_names_are_retained(self):
        self.respond([("value", "int8"), ("value", "text")], [["1", "a"]])
        frame = self.con.read_sql("SELECT 1 AS value, 'a' AS value")
        self.assertEqual(list(frame.columns), ["value", "value"])
        self.assertEqual(frame.iloc[0, 1], "a")

    def test_chunks_and_empty_chunk(self):
        self.respond([("id", "int8")], [[str(i)] for i in range(5)])
        chunks = list(self.con.read_sql("SELECT ids", chunksize=2))
        self.assertEqual([len(frame) for frame in chunks], [2, 2, 1])
        self.respond([("id", "int8")], [])
        chunks = list(self.con.read_sql("SELECT empty", chunksize=2))
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].empty)

    def test_truncated_full_result_is_rejected(self):
        self.respond([("id", "int8")], [["1"]], complete=False)
        with self.assertRaisesRegex(SQLAPIError, "incomplete"):
            self.con.read_sql("SELECT ids")

    def test_truncated_chunk_iteration_is_rejected(self):
        self.respond([("id", "int8")], [["1"], ["2"]], complete=False)
        chunks = self.con.read_sql("SELECT ids", chunksize=1)
        self.assertEqual(len(next(chunks)), 1)
        self.assertEqual(len(next(chunks)), 1)
        with self.assertRaisesRegex(SQLAPIError, "incomplete"):
            next(chunks)

    def test_completion_count_must_match(self):
        response = self.respond([("id", "int8")], [["1"]])
        response.iter_lines.return_value = iter([
            b'{"type":"schema","version":1,"columns":[{"name":"id","type":"int8"}]}',
            b'{"type":"complete","row_count":1}',
        ])
        with self.assertRaisesRegex(SQLAPIError, "row count"):
            self.con.read_sql("SELECT ids")

    def test_database_error_details(self):
        response = self.respond([], [])
        response.status_code = 403
        response.json.return_value = {"error": {"code": "query_failed", "message": "permission denied", "sqlstate": "42501"}}
        with self.assertRaises(SQLAPIError) as caught:
            self.con.read_sql("DELETE FROM events")
        self.assertEqual(caught.exception.sqlstate, "42501")
        self.assertEqual(caught.exception.status_code, 403)

    def test_transport_failure_does_not_expose_credentials(self):
        import requests
        self.con._session.post.side_effect = requests.ConnectionError("secret")
        with self.assertRaises(SQLAPIError) as caught:
            self.con.read_sql("SELECT 1")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("secret", repr(self.con))

    def test_credentials_and_safe_transport_options(self):
        self.respond([("value", "text")], [["x"]])
        self.con.read_sql("SELECT $1::text", params=["O'Reilly"])
        kwargs = self.con._session.post.call_args.kwargs
        self.assertEqual(kwargs["auth"], ("reader", "secret"))
        self.assertTrue(kwargs["verify"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"], {"database": "sample_database", "sql": "SELECT $1::text", "params": ["O'Reilly"]})

    def test_parameter_conversion_keeps_precision_and_bool(self):
        self.respond([], [[]])
        params = [2**63 - 1, Decimal("1.1234567890123456789"), False, None, dt.datetime(2000, 1, 1, 17)]
        self.con.read_sql("SELECT params", params=params)
        sent = self.con._session.post.call_args.kwargs["json"]["params"]
        self.assertEqual(sent, [str(2**63 - 1), "1.1234567890123456789", False, None, "2000-01-01T17:00:00"])

    def test_named_query_reuses_values_in_order_of_first_occurrence(self):
        self.respond([("file", "text")], [["example.root"]])
        query = """
            SELECT * FROM root_files
            WHERE time > %(event_time)s AND file LIKE %(file_pattern)s
            ORDER BY ABS(EXTRACT(EPOCH FROM (%(event_time)s - time))) LIMIT 1;
        """
        event_time = pd.Timestamp("2000-01-01 17:00:00.123456")
        params = {"file_pattern": "%_CH3@%", "event_time": event_time}
        read_sql(query, con=self.con, params=params)
        payload = self.con._session.post.call_args.kwargs["json"]
        self.assertEqual(payload["sql"], query.replace("%(event_time)s", "$1").replace("%(file_pattern)s", "$2"))
        self.assertEqual(payload["params"], [event_time.isoformat(), "%_CH3@%"])
        self.assertEqual(list(params), ["file_pattern", "event_time"])

    def test_named_values_are_bound_without_sql_interpolation(self):
        self.respond([], [])
        value = "O'Reilly'); DROP TABLE example; -- %(other)s $1 %%"
        self.con.read_sql("SELECT %(value)s::text", params={"value": value})
        payload = self.con._session.post.call_args.kwargs["json"]
        self.assertEqual(payload["sql"], "SELECT $1::text")
        self.assertEqual(payload["params"], [value])

    def test_named_placeholders_skip_sql_quoting_comments_and_identifiers(self):
        fragments = [
            "'it''s %(missing)s and $1'",
            r"'backslash\'",
            '"identifier""%(missing)s$1"',
            r"E'it\'s %(missing)s and $1'",
            r"e'backslash\\ %(missing)s and $1'",
            "$$%(missing)s and $1$$",
            "$tag$%(missing)s $$ ' $other$ and $1$tag$",
            "$täg_1$%(missing)s and $1$täg_1$",
            "1 /* %(missing)s /* nested */ and $1 */",
            "1 -- %(missing)s and $1\n",
            "1 -- %(missing)s and $1\r",
            "identifier$1",
        ]
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.respond([], [])
                self.con.read_sql(f"SELECT {fragment}, %(value)s", params={"value": 42})
                payload = self.con._session.post.call_args.kwargs["json"]
                self.assertEqual(payload["sql"], f"SELECT {fragment}, $1")
                self.assertEqual(payload["params"], ["42"])

    def test_named_params_allow_modulo_and_escaped_percent_signs(self):
        self.respond([], [])
        self.con.read_sql(
            "SELECT %(value)s::int % 3, 10 %% %(value)s::int, '100%%' /* %% */",
            params={"value": 4},
        )
        payload = self.con._session.post.call_args.kwargs["json"]
        self.assertEqual(payload["sql"], "SELECT $1::int % 3, 10 % $1::int, '100%' /* % */")
        self.assertEqual(payload["params"], ["4"])

    def test_named_params_accept_mappings_and_ignore_unused_keys(self):
        self.respond([], [])
        self.con.read_sql("SELECT %(value)s::int", params=MappingProxyType({"unused": object(), "value": 42}))
        payload = self.con._session.post.call_args.kwargs["json"]
        self.assertEqual(payload["sql"], "SELECT $1::int")
        self.assertEqual(payload["params"], ["42"])

    def test_empty_named_params_and_sql_without_placeholders(self):
        self.respond([], [])
        self.con.read_sql("SELECT 1", params={})
        self.assertEqual(self.con._session.post.call_args.kwargs["json"]["params"], [])

    def test_missing_named_param_is_rejected_before_request(self):
        for options in [{}, {"chunksize": 1}]:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "Missing named SQL parameter: 'missing'"):
                self.con.read_sql("SELECT %(value)s, %(missing)s", params={"value": 1}, **options)
        self.con._session.post.assert_not_called()

    def test_dictionary_params_reject_native_or_mixed_placeholders(self):
        for query in ["SELECT $1", "SELECT $1, %(value)s", "SELECT %(value)s, $2"]:
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "do not mix"):
                self.con.read_sql(query, params={"value": 1})
        self.con._session.post.assert_not_called()

    def test_positional_lists_and_tuples_leave_sql_unchanged(self):
        query = "SELECT $1::int, '%(literal)s 100%%' AS literal"
        for params in [[42], (42,)]:
            with self.subTest(params=params):
                self.respond([], [])
                self.con.read_sql(query, params=params)
                payload = self.con._session.post.call_args.kwargs["json"]
                self.assertEqual(payload["sql"], query)
                self.assertEqual(payload["params"], ["42"])

    def test_numeric_json_bytes_boolean_and_timezone(self):
        self.respond(
            [("n", "numeric"), ("j", "jsonb"), ("b", "bytea"), ("flag", "bool"), ("t", "timestamptz")],
            [["1.1234567890123456789", '{"channels":[1,2]}', "\\x00ff", "f", "2000-01-01 17:00:00.123456+01"]],
        )
        frame = self.con.read_sql("SELECT types", coerce_float=False)
        self.assertEqual(frame["n"].iloc[0], Decimal("1.1234567890123456789"))
        self.assertEqual(frame["j"].iloc[0], {"channels": [1, 2]})
        self.assertEqual(frame["b"].iloc[0], b"\x00\xff")
        self.assertFalse(frame["flag"].iloc[0])
        self.assertEqual(frame["t"].iloc[0], pd.Timestamp("2000-01-01T16:00:00.123456Z"))

    def test_array_quoting_escapes_nesting_null_and_bounds(self):
        self.assertEqual(_array('{"a,b","NULL",NULL,"a\\"b","a\\\\b",""}', "text"), ["a,b", "NULL", None, 'a"b', "a\\b", ""])
        self.assertEqual(_array("[0:1][2:3]={{1,2},{3,NULL}}", "int4"), [[1, 2], [3, None]])
        self.assertEqual(_array("{}", "float8"), [])
        with self.assertRaises(ValueError):
            _array('{"unfinished}', "text")

    def test_http_requires_explicit_loopback_opt_in(self):
        with self.assertRaises(ValueError):
            SQLConnection("http://example.test/query.php", "reader", "secret", database="sample_database", allow_local_http=True)
        with self.assertRaises(ValueError):
            SQLConnection("http://127.0.0.1/query.php", "reader", "secret", database="sample_database")
        with SQLConnection("http://127.0.0.1/query.php", "reader", "secret", database="sample_database", allow_local_http=True):
            pass

    def test_redirect_is_not_followed(self):
        response = self.respond([], [])
        response.status_code = 302
        response.json.side_effect = ValueError()
        with self.assertRaises(SQLAPIError) as caught:
            self.con.read_sql("SELECT 1")
        self.assertEqual(caught.exception.status_code, 302)

    def test_invalid_url_and_query_arguments(self):
        for url in ["https://reader:secret@example.test/query.php", "https:///query.php", "https://example.test/query.php?password=secret"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                SQLConnection(url, "reader", "secret", database="sample_database")
        for chunksize in [0, -1, True, 1.5]:
            with self.subTest(chunksize=chunksize), self.assertRaises(ValueError):
                self.con.read_sql("SELECT 1", chunksize=chunksize)
        for params in ["value", b"value", 42, {1, 2}]:
            with self.subTest(params=params), self.assertRaises(TypeError):
                self.con.read_sql("SELECT $1", params=params)

    def test_database_is_required_and_validated(self):
        with self.assertRaises(TypeError):
            SQLConnection("https://example.test/query.php", "reader", "secret")
        for database in [None, "", " \t", "bad\0name", 123, False, []]:
            with self.subTest(database=database), self.assertRaises(ValueError):
                SQLConnection("https://example.test/query.php", "reader", "secret", database=database)

    def test_database_name_is_forwarded_unchanged(self):
        database = "data 'quoted' \\ name = example"
        self.con.database = database
        self.respond([("value", "int4")], [["1"]])
        self.con.read_sql("SELECT 1")
        self.assertEqual(self.con._session.post.call_args.kwargs["json"]["database"], database)


if __name__ == "__main__":
    unittest.main()
