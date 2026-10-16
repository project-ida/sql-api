# SQL API

Run PostgreSQL queries over HTTPS using PostgreSQL authentication and database
permissions. Apache serves a PHP endpoint, and a standalone Python helper
converts results into pandas DataFrames.

```text
Python client → HTTPS :443 → Apache/PHP → PostgreSQL
```

## Files

| Path | Purpose |
| --- | --- |
| `server/public/query.php` | The public POST endpoint |
| `server/src/api.php` | Authentication, query execution, and response handling |
| `server/config.example.php` | Fixed database connection settings; copy to `config.php` |
| `deploy/apache.conf.example` | Alias configuration for the existing HTTPS virtual host |
| `client/sql_api.py` | Standalone Python helper for pandas |
| `client/README.md` | Python client usage and examples |
| `tests/` | Client, PHP, and PostgreSQL integration checks |

## Server setup

Requires PHP 8.1+ with the **pgsql** extension, Apache with working HTTPS/PHP,
and an existing PostgreSQL database and password-authenticated read-only account.
No Composer dependencies or additional application service are required.

1. Place this checkout somewhere such as `/var/www/sql-api`.
2. Install/enable PHP's PostgreSQL extension. On Debian/Ubuntu, the package is
   usually `php-pgsql`; use the matching package for your installed PHP version.
3. Copy `server/config.example.php` to `server/config.php`. Set `db_name` to
   your database name; the example leaves it empty. Configure `db_host` and
   `db_port` for your installation. The host defaults to `127.0.0.1` for a local
   database connection. The config contains no database username/password: those
   come from each authenticated request.
4. Add the snippet in `deploy/apache.conf.example` **inside the existing HTTPS
   virtual host**, adjusting the checkout path. Keep that virtual host's existing
   PHP handler and TLS configuration. Expose only `server/public`, not the entire
   checkout. Validate Apache's config before reloading it.
5. Ensure PHP can read `server/config.php` and `server/src/api.php`. The config
   can alternatively live elsewhere: set `SQL_API_CONFIG` in PHP's environment
   to its absolute path. With PHP-FPM this must reach the FPM worker environment.

The example URL is `https://your-server.example/sql-api/query.php`.
`server/config.php` is ignored by Git. Leave `allow_http_on_loopback` false in
production. The endpoint requires Apache's actual HTTPS state, rather than
trusting client-supplied forwarded headers.

### Keep PostgreSQL in charge of authentication and permissions

The endpoint connects as the supplied PostgreSQL role. Use a database account
with the read permissions required by your application. Credentials are sent
using HTTP Basic authentication over verified HTTPS. PostgreSQL checks the
credentials and applies that role's permissions.

**The local PostgreSQL connection must check the password.** A matching
`pg_hba.conf` entry using `trust` would bypass it, and `peer` would authenticate
the PHP operating-system user instead. For example, a database administrator
can configure an applicable rule using these placeholders:

```text
host <database_name> <database_user> 127.0.0.1/32 scram-sha-256
```

Replace the placeholders with your database and role names. Rule order matters:
PostgreSQL uses the first matching rule. If `db_host` is a Unix socket directory
instead of `127.0.0.1`, the matching `local` rule must require password
authentication as well. The database port can remain closed to external traffic.

Only distribute the intended read-only credentials. This is a general SQL
endpoint: connecting with a more privileged account gives SQL that account's
permissions, just as a direct database connection would.

### Check the installed endpoint

This command prompts for the database password instead of placing it in shell
history:

```sh
curl --fail-with-body --user '<database_user>' \
  --header 'Content-Type: application/json' \
  --data '{"sql":"SELECT current_user, current_database()"}' \
  https://your-server.example/sql-api/query.php
```

Repeat with a deliberately incorrect password: the response must be HTTP 401.
That verifies that the local database rule is actually checking credentials.

If PHP reports that credentials are missing, ensure Apache passes the
`Authorization` header to PHP. The example uses `CGIPassAuth On` for CGI/FPM.
Do not enable request-body or Authorization-header logging for this endpoint.

## Python client

Install the Python dependencies and make the `client/` directory available on
Python's import path. Supply connection settings through environment variables:

```python
import os

from sql_api import SQLConnection, read_sql

with SQLConnection(
    os.environ["SQL_API_URL"],
    os.environ["SQL_API_USER"],
    os.environ["SQL_API_PASSWORD"],
) as connection:
    df = read_sql("SELECT $1::integer AS value", connection, params=[42])
```

Set `SQL_API_URL` to the complete HTTPS query endpoint, such as
`https://your-server.example/sql-api/query.php`. The other two variables contain
the PostgreSQL username and password. The server configuration selects the
database. See [client/README.md](client/README.md) for installation, parameterised
queries, and chunked downloads.

## Query and result behaviour

- Each POST accepts one SQL statement returning rows. CTEs, joins, PostgreSQL
  functions, window functions, and `EXPLAIN` are available subject to the role's
  existing permissions. SQL text is forwarded without rewriting.
- Optional parameters are a JSON array using PostgreSQL `$1`, `$2`, …
  placeholders. Multiple SQL commands in one request are rejected by PostgreSQL's
  parameterised-query protocol; trailing semicolons and CTEs are fine.
- Each request uses a new database connection. Temporary tables, transaction
  state, and session settings do not persist across requests. This is a
  query/result bridge, not a PostgreSQL wire-protocol or SQLAlchemy replacement.
- The server returns newline-delimited JSON: column names/types, rows in
  PostgreSQL text format, and a final row count. The Python helper reconstructs
  common types without sending large integers through floating-point conversion.
- The helper preserves `timestamp` precision and parses `timestamptz` to UTC.
  Supported PostgreSQL arrays become Python lists. Nullable integer columns use
  pandas `Int64`. `numeric` becomes float by default, matching pandas;
  `coerce_float=False` retains `Decimal` values. Unknown PostgreSQL types remain
  text. Empty results retain their columns and supported dtypes.
- HTTP downloads are streamed and have no application row cap. A completion
  marker lets the helper detect interrupted results. `chunksize` limits the
  number of rows per client DataFrame, but **the PHP/libpq driver currently
  buffers the database result**. For results too large for that server memory,
  use smaller SQL ranges; a server-side cursor would be a later extension.
- Match Apache/PHP-FPM execution timeouts to the queries you run. The client's
  default socket-read timeout is 300 seconds and is configurable. No automatic
  retries are made. Resource limits already configured for PostgreSQL still apply.

Query errors include PostgreSQL's SQLSTATE and primary error message for the
authenticated client, without query-body logging. Missing/incorrect
credentials return 401, permission failures 403, invalid SQL 400, and an
unreachable database 503. Database connection errors intentionally omit internal
connection details.

## Development and checks

Install the Python dependencies in your preferred environment:

```sh
python -m pip install -r client/requirements.txt
php -l server/src/api.php
php -l server/public/query.php
php tests/test_server.php
python -m unittest discover -s tests -v
```

The Python suite skips database integration tests unless
`SQL_API_TEST_ADMIN_DSN` is set. For a **disposable test database**, set it to an
administrator connection string and set `SQL_API_TEST_HOST`,
`SQL_API_TEST_PORT`, and `SQL_API_TEST_DATABASE` to the matching destination
(defaults: `127.0.0.1`, `5432`, `postgres`). Tests create/remove their own schema
and read-only role, start a local PHP server, and verify queries, authentication,
write rejection, type fidelity, and chunked downloads using synthetic fixtures.
They require `psql`, PHP with pgsql, and host password authentication. GitHub
Actions provides a disposable PostgreSQL service and runs these tests.

For local manual development, copy the example config and set
`allow_http_on_loopback` to true, then run:

```sh
php -S 127.0.0.1:8080 -t server/public
```

Use `http://127.0.0.1:8080/query.php` and explicitly set
`allow_local_http=True` in the Python client. Both opt-ins are for loopback
development only. PHP's development server is not the deployment server.

## References

- [PHP PostgreSQL parameterised queries](https://www.php.net/manual/en/function.pg-query-params.php)
- [PostgreSQL password authentication](https://www.postgresql.org/docs/current/auth-password.html)
- [PostgreSQL client authentication rules](https://www.postgresql.org/docs/current/auth-pg-hba-conf.html)
- [Apache CGIPassAuth](https://httpd.apache.org/docs/2.4/mod/core.html#cgipassauth)
