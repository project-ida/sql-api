# Python client

`sql_api.py` is a standalone module for running PostgreSQL queries over HTTPS
and returning pandas DataFrames. It depends on pandas and requests:

```sh
python -m pip install -r client/requirements.txt
```

## Connection and queries

Make the `client/` directory available on Python's import path. For example, add
it to `PYTHONPATH` or place `sql_api.py` alongside your application.

Supply the endpoint URL, PostgreSQL credentials, and database name through
environment variables:

```python
import os

from sql_api import SQLConnection, SQLAPIError, read_sql

connection = SQLConnection(
    os.environ["SQL_API_URL"],
    os.environ["SQL_API_USER"],
    os.environ["SQL_API_PASSWORD"],
    database=os.environ["SQL_API_DATABASE"],
    timeout=(10, 600),  # connect and socket-read timeout in seconds
)

df = read_sql("SELECT $1::integer AS value", connection, params=[42])
```

`SQL_API_URL` is the full query endpoint URL, for example
`https://your-server.example/sql-api/query.php`. The API's server configuration
selects the PostgreSQL host and port. The client supplies the username, password,
and required database name. There is no server-side database default. Each query
runs with that database account's permissions in the selected database.

`connection.read_sql(query, ...)` is also available. Call `connection.close()`
when finished, or use `with SQLConnection(...) as connection:`. TLS certificates
are verified. For a custom trusted CA, use `ca_bundle="/path/to/ca.pem"`.

## Parameters and DataFrame options

Optional bound parameters use PostgreSQL's native positional placeholders:

```python
df = read_sql(
    "SELECT $1::bigint AS id, $2::text AS label",
    connection,
    params=[123, "example"],
    index_col="id",
)
```

Datetime/date/time values and pandas timestamps are encoded as ISO strings.
Numbers, strings, booleans, and null values are supported. For an array parameter,
pass its PostgreSQL text form such as `"{1,2}"` with a `$1::int[]` cast.
Parameters bind values; identifiers such as table names belong in the SQL text.

The helper supports `index_col`, positional `params`, `chunksize`, and
`coerce_float`. It implements a subset of `pandas.read_sql` options. Each query
has an independent database session, so temporary tables and transaction state
do not persist between calls.

## Larger downloads

Set `chunksize` to receive an iterator of DataFrames:

```python
query = "SELECT generate_series(1, 1000000) AS id"
for chunk in read_sql(query, connection, chunksize=100_000):
    print(len(chunk))
```

Consume the iterator to completion: an interrupted response raises
`SQLAPIError`, including after earlier chunks have been yielded. A normal
`read_sql(...)` returns a DataFrame only after verifying the complete response.
Chunks reduce client-side memory use; the current PHP/libpq result is still
buffered on the server. No automatic pagination or row truncation is applied.

## Types and errors

Common numeric, boolean, datetime, JSON, bytea, and numeric/text array types are
decoded. Unknown PostgreSQL types retain their text representation. Timestamp
infinities or values outside pandas' supported range remain text. `timestamptz`
columns are normalized to UTC, preserving the instant. Nullable integers use
pandas `Int64`. `numeric` becomes float by default; use `coerce_float=False` to
retain `Decimal` values.

```python
try:
    df = read_sql(query, connection)
except SQLAPIError as error:
    print(error.status_code, error.sqlstate, str(error))
```

The client does not automatically retry requests or follow redirects. Its
`timeout` setting controls connection and socket-read timeouts rather than a
total query duration.
