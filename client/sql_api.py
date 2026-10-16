"""Run PostgreSQL queries over HTTPS and return pandas DataFrames.

Uses PostgreSQL username/password authentication. SQL is supplied by the caller;
optional bound parameters use PostgreSQL's $1, $2, ... placeholders.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import numbers
from decimal import Decimal
from typing import Iterator, Sequence
from urllib.parse import urlsplit

import pandas as pd
import requests

__all__ = ["SQLConnection", "SQLAPIError", "read_sql"]


class SQLAPIError(RuntimeError):
    """An HTTP, PostgreSQL, or incomplete-result error from the SQL API."""

    def __init__(self, message, *, status_code=None, sqlstate=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.sqlstate = sqlstate
        self.code = code


def _parameter(value):
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        # Send as text so PHP/JSON intermediaries cannot round a large integer.
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, numbers.Real):
        value = float(value)
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    raise TypeError(f"Unsupported query parameter type: {type(value).__name__}. Pass its PostgreSQL text representation instead.")


def _array(text, element_type):
    """Parse PostgreSQL array text, including nesting, escapes, and SQL NULL."""
    # Non-default array lower bounds do not affect Python list positions.
    if text.startswith("["):
        _, separator, text = text.partition("=")
        if not separator:
            raise ValueError("Invalid PostgreSQL array bounds")
    position = 0

    def parse():
        nonlocal position
        if position >= len(text) or text[position] != "{":
            raise ValueError("Invalid PostgreSQL array")
        position += 1
        result = []
        if position < len(text) and text[position] == "}":
            position += 1
            return result
        while position < len(text):
            if text[position] == "{":
                item = parse()
            else:
                quoted = text[position] == '"'
                value = []
                if quoted:
                    position += 1
                while position < len(text):
                    char = text[position]
                    if char == "\\":
                        position += 1
                        if position >= len(text):
                            raise ValueError("Invalid PostgreSQL array escape")
                        value.append(text[position])
                        position += 1
                    elif quoted and char == '"':
                        position += 1
                        break
                    elif not quoted and char in ",}":
                        break
                    else:
                        value.append(char)
                        position += 1
                else:
                    raise ValueError("Unterminated PostgreSQL array value")
                value = "".join(value)
                item = None if not quoted and value == "NULL" else _decode(value, element_type)
            result.append(item)
            if position >= len(text):
                break
            char = text[position]
            position += 1
            if char == "}":
                return result
            if char != ",":
                break
        raise ValueError("Unterminated PostgreSQL array")

    result = parse()
    if position != len(text):
        raise ValueError("Unexpected text after PostgreSQL array")
    return result


_INTEGER_TYPES = {"int2", "int4", "int8", "oid"}
_FLOAT_TYPES = {"float4", "float8"}
_ARRAY_TYPES = _INTEGER_TYPES | _FLOAT_TYPES | {
    "numeric", "bool", "text", "varchar", "bpchar", "name", "json", "jsonb",
    "date", "timestamp", "timestamptz", "uuid", "bytea",
}


def _decode(value, pg_type):
    if value is None:
        return None
    if pg_type in _INTEGER_TYPES:
        return int(value)
    if pg_type in _FLOAT_TYPES:
        return float(value)
    if pg_type == "numeric":
        return Decimal(value)
    if pg_type == "bool":
        return value == "t"
    if pg_type in {"json", "jsonb"}:
        return json.loads(value)
    if pg_type == "bytea" and value.startswith("\\x"):
        return bytes.fromhex(value[2:])
    if pg_type == "date" and value not in {"infinity", "-infinity"}:
        return dt.date.fromisoformat(value)
    if pg_type.startswith("_") and pg_type[1:] in _ARRAY_TYPES:
        return _array(value, pg_type[1:])
    # Unknown/custom types, intervals, and timestamp array elements retain text.
    return value


def _dataframe(columns, rows, index_col, coerce_float):
    series = []
    for i, column in enumerate(columns):
        pg_type = column["type"]
        values = [_decode(row[i], pg_type) for row in rows]
        nullable = any(value is None for value in values)
        if pg_type in _INTEGER_TYPES:
            item = pd.Series(values, dtype="Int64" if nullable else "int64")
        elif pg_type in _FLOAT_TYPES:
            item = pd.Series(values, dtype="float64")
        elif pg_type == "bool":
            item = pd.Series(values, dtype="boolean" if nullable else "bool")
        elif pg_type == "numeric" and coerce_float:
            item = pd.Series([float(v) if v is not None else None for v in values], dtype="float64")
        else:
            item = pd.Series(values, dtype=object)
            if pg_type in {"timestamp", "timestamptz"}:
                try:
                    item = pd.to_datetime(item, format="ISO8601", utc=pg_type == "timestamptz")
                except (ValueError, OverflowError):
                    # Preserve infinity/BC/out-of-range timestamps as text.
                    pass
        series.append(item)
    frame = pd.concat(series, axis=1) if series else pd.DataFrame(index=range(len(rows)))
    frame.columns = [column["name"] for column in columns]
    if index_col is not None:
        frame.set_index(index_col, inplace=True)
    return frame


class SQLConnection:
    """Connection settings for independent SQL-over-HTTPS requests.

    ``url`` is the full query.php URL. Passwords are excluded from repr. TLS
    certificates are verified; ``ca_bundle`` optionally names a custom CA file.
    HTTP is allowed only on loopback and only with ``allow_local_http=True``.
    ``timeout`` is a requests timeout in seconds (connect, socket read), not a
    total query duration. There are no automatic retries or redirects.
    """

    def __init__(self, url, username, password, *, timeout=(10, 300), ca_bundle=None, allow_local_http=False):
        parsed = urlsplit(url)
        local_http = allow_local_http and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not local_http:
            raise ValueError("Use an HTTPS URL. Local HTTP requires allow_local_http=True.")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("Use a query endpoint URL without credentials, query parameters, or a fragment.")
        if not isinstance(username, str) or not username or ":" in username:
            raise ValueError("HTTP Basic authentication requires a non-empty username without a colon.")
        if not isinstance(password, str) or not password:
            raise ValueError("A non-empty PostgreSQL password is required.")
        self.url = url
        self.username = username
        self._password = password
        self.timeout = timeout
        self._verify = str(ca_bundle) if ca_bundle is not None else True
        self._session = requests.Session()

    def __repr__(self):
        return f"SQLConnection(url={self.url!r}, username={self.username!r})"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._session.close()

    def read_sql(self, sql, params=None, index_col=None, *, chunksize=None, coerce_float=True):
        return read_sql(sql, self, index_col=index_col, params=params, chunksize=chunksize, coerce_float=coerce_float)

    def _frames(self, sql, params, index_col, chunksize, coerce_float):
        payload = {"sql": sql, "params": [_parameter(value) for value in params]}
        try:
            with self._session.post(
                self.url, json=payload, auth=(self.username, self._password),
                headers={"Accept": "application/x-ndjson"},
                timeout=self.timeout, verify=self._verify,
                stream=True, allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    try:
                        error = response.json()["error"]
                        message = error["message"]
                        code, sqlstate = error.get("code"), error.get("sqlstate")
                    except (ValueError, KeyError, TypeError):
                        message = f"SQL API returned HTTP {response.status_code}. Check the endpoint URL and Apache configuration."
                        code = sqlstate = None
                    raise SQLAPIError(message, status_code=response.status_code, sqlstate=sqlstate, code=code)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/x-ndjson":
                    raise SQLAPIError("Expected a SQL API result stream; check the endpoint URL.")
                columns = None
                rows = []
                count = 0
                complete = False
                for line in response.iter_lines(chunk_size=65536):
                    if not line:
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise SQLAPIError("Invalid SQL API result record.")
                    kind = record.get("type")
                    if complete:
                        raise SQLAPIError("Unexpected data after the result completion marker.")
                    if kind == "error":
                        error = record["error"]
                        raise SQLAPIError(error["message"], sqlstate=error.get("sqlstate"), code=error.get("code"))
                    if kind == "schema" and columns is None:
                        if record.get("version") != 1 or not isinstance(record.get("columns"), list):
                            raise SQLAPIError("Unsupported SQL API result format.")
                        columns = record["columns"]
                        if any(not isinstance(c, dict) or not isinstance(c.get("name"), str) or not isinstance(c.get("type"), str) for c in columns):
                            raise SQLAPIError("Invalid SQL API column metadata.")
                    elif kind == "row" and columns is not None:
                        values = record.get("values")
                        if not isinstance(values, list) or len(values) != len(columns) or any(v is not None and not isinstance(v, str) for v in values):
                            raise SQLAPIError("Invalid SQL API result row.")
                        rows.append(values)
                        count += 1
                        if chunksize is not None and len(rows) == chunksize:
                            yield _dataframe(columns, rows, index_col, coerce_float)
                            rows = []
                    elif kind == "complete" and columns is not None:
                        if record.get("row_count") != count:
                            raise SQLAPIError("The SQL API result row count does not match the completion marker.")
                        complete = True
                    else:
                        raise SQLAPIError("Unexpected SQL API result record.")
                if not complete:
                    raise SQLAPIError("The SQL API response ended before completion; the result is incomplete.")
                if rows or count == 0 or chunksize is None:
                    yield _dataframe(columns, rows, index_col, coerce_float)
        except requests.RequestException:
            # Do not include request objects: they contain the Authorization header.
            raise SQLAPIError("The HTTPS request failed or timed out; check server connectivity, TLS certificates, and the timeout setting.") from None
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise SQLAPIError(f"Could not decode the SQL API result ({type(error).__name__}).") from None


def read_sql(sql, con, index_col=None, params=None, *, chunksize=None, coerce_float=True):
    """Return a DataFrame, or an iterator of DataFrames when chunksize is set.

    A small alternative to ``pd.read_sql(sql, engine, index_col=None)``. Each
    call is an independent database session; temporary tables and transactions
    do not persist between calls. SQL strings are forwarded without rewriting.
    ``params`` is a sequence for PostgreSQL $1, $2, ... placeholders. Arrays can
    be passed in PostgreSQL text form, e.g. "{1,2}" with a $1::int[] cast.
    Timestamp columns are parsed automatically; timestamptz is normalized to
    UTC. Nullable integers use pandas Int64 to preserve exact values. numeric
    uses float by default, as in pandas; set coerce_float=False for Decimal.
    """
    if not isinstance(con, SQLConnection):
        raise TypeError("con must be a SQLConnection")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must be a non-empty SQL string")
    if params is None:
        params = []
    if isinstance(params, (str, bytes, dict)) or not isinstance(params, Sequence):
        raise TypeError("params must be a sequence for $1, $2, ... placeholders")
    if chunksize is not None and (isinstance(chunksize, bool) or not isinstance(chunksize, int) or chunksize <= 0):
        raise ValueError("chunksize must be a positive integer")
    frames = con._frames(sql, params, index_col, chunksize, coerce_float)
    if chunksize is not None:
        return frames
    try:
        return next(frames)
    finally:
        frames.close()
