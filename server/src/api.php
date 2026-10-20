<?php
declare(strict_types=1);

namespace SqlApi;

final class ApiError extends \RuntimeException
{
    public function __construct(
        public readonly int $status,
        public readonly string $errorCode,
        string $message,
        public readonly ?string $sqlstate = null,
    ) {
        parent::__construct($message);
    }
}

function encode_json(mixed $value): string
{
    return json_encode($value, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE);
}

/** Quote a libpq connection-string value, including a supplied password. */
function conninfo_value(string $value): string
{
    if (str_contains($value, "\0")) {
        throw new ApiError(400, 'invalid_credentials', 'Credentials cannot contain NUL characters.');
    }
    return "'" . str_replace(["\\", "'"], ["\\\\", "\\'"], $value) . "'";
}

function credentials(array $server): array
{
    if (isset($server['PHP_AUTH_USER'], $server['PHP_AUTH_PW'])) {
        $pair = [$server['PHP_AUTH_USER'], $server['PHP_AUTH_PW']];
    } else {
        $header = $server['HTTP_AUTHORIZATION'] ?? $server['REDIRECT_HTTP_AUTHORIZATION'] ?? '';
        if (!preg_match('/^Basic\s+(\S+)$/i', $header, $matches)) {
            throw new ApiError(401, 'authentication_required', 'Supply the PostgreSQL username and password using HTTP Basic authentication.');
        }
        $decoded = base64_decode($matches[1], true);
        if ($decoded === false || !str_contains($decoded, ':')) {
            throw new ApiError(401, 'authentication_required', 'Invalid HTTP Basic authentication.');
        }
        $pair = explode(':', $decoded, 2);
    }
    if ($pair[0] === '' || $pair[1] === '') {
        throw new ApiError(401, 'authentication_required', 'A PostgreSQL username and password are required.');
    }
    return $pair;
}

function validate_request(array $server, array $config): void
{
    $https = in_array(strtolower((string) ($server['HTTPS'] ?? '')), ['on', '1'], true);
    $localHttp = ($config['allow_http_on_loopback'] ?? false) === true
        && in_array($server['REMOTE_ADDR'] ?? '', ['127.0.0.1', '::1'], true);
    if (!$https && !$localHttp) {
        // Forwarded headers are intentionally not accepted as evidence of TLS.
        throw new ApiError(400, 'https_required', 'This endpoint requires HTTPS.');
    }
    if (($server['REQUEST_METHOD'] ?? '') !== 'POST') {
        throw new ApiError(405, 'method_not_allowed', 'Use POST to submit a SQL query.');
    }
    $contentType = strtolower(trim(explode(';', $server['CONTENT_TYPE'] ?? '')[0]));
    if ($contentType !== 'application/json') {
        throw new ApiError(415, 'unsupported_media_type', 'Send an application/json request body.');
    }
}

function parse_request(string $body): array
{
    try {
        $request = json_decode($body, false, 512, JSON_THROW_ON_ERROR | JSON_BIGINT_AS_STRING);
    } catch (\JsonException) {
        throw new ApiError(400, 'invalid_request', 'The request body is not valid JSON.');
    }
    if (!$request instanceof \stdClass || !isset($request->sql)
        || !is_string($request->sql) || trim($request->sql) === ''
        || str_contains($request->sql, "\0")) {
        throw new ApiError(400, 'invalid_request', 'Provide a non-empty SQL string in the sql field.');
    }
    if (!isset($request->database) || !is_string($request->database)
        || trim($request->database) === '' || str_contains($request->database, "\0")) {
        throw new ApiError(400, 'invalid_request', 'Provide a non-empty database name in the database field.');
    }
    $params = $request->params ?? [];
    if (!is_array($params)) {
        throw new ApiError(400, 'invalid_request', 'params must be a JSON array of scalar values or null.');
    }
    foreach ($params as &$param) {
        if (!is_scalar($param) && $param !== null) {
            throw new ApiError(400, 'invalid_request', 'Query parameters must be scalar values or null.');
        }
        if (is_string($param) && str_contains($param, "\0")) {
            throw new ApiError(400, 'invalid_request', 'Query parameters cannot contain NUL characters.');
        }
        if (is_bool($param)) {
            $param = $param ? 'true' : 'false';
        } elseif (is_float($param)) {
            // JSON uses a decimal point independent of the PHP process locale.
            $param = encode_json($param);
        }
    }
    unset($param);
    return [$request->sql, $params, $request->database];
}

/** Catch connection warnings so libpq messages never leak credentials to HTTP. */
function connect(array $config, string $username, string $password, string $database): \PgSql\Connection
{
    $fields = [
        'host' => $config['db_host'],
        'port' => $config['db_port'] ?? 5432,
        'dbname' => $database,
        'user' => $username,
        'password' => $password,
        'connect_timeout' => $config['connect_timeout'] ?? 10,
        'application_name' => 'sql-api',
        'options' => '-c client_encoding=UTF8 -c DateStyle=ISO -c bytea_output=hex',
    ];
    $parts = [];
    foreach ($fields as $key => $value) {
        $parts[] = $key . '=' . conninfo_value((string) $value);
    }
    $warning = '';
    set_error_handler(static function (int $severity, string $message) use (&$warning): bool {
        $warning = $message;
        return true;
    });
    try {
        $connection = pg_connect(implode(' ', $parts), PGSQL_CONNECT_FORCE_NEW);
    } finally {
        restore_error_handler();
    }
    if ($connection === false) {
        if (preg_match('/authentication failed|no password supplied|role .* does not exist/i', $warning)) {
            throw new ApiError(401, 'authentication_failed', 'PostgreSQL rejected the username or password.');
        }
        if (str_contains($warning, 'permission denied for database')) {
            throw new ApiError(403, 'database_forbidden', 'PostgreSQL denied access to the requested database.');
        }
        throw new ApiError(503, 'database_unavailable', 'Cannot connect to the requested PostgreSQL database.');
    }
    return $connection;
}

/** Submit one statement, leaving SQL permissions to the supplied database role. */
function execute(\PgSql\Connection $connection, string $sql, array $params): \PgSql\Result
{
    if (!@pg_send_query_params($connection, $sql, $params)) {
        throw new ApiError(503, 'database_unavailable', 'Could not submit the query to PostgreSQL.');
    }
    $result = pg_get_result($connection);
    if ($result === false) {
        throw new ApiError(503, 'database_unavailable', 'PostgreSQL did not return a result.');
    }
    $status = pg_result_status($result);
    if ($status === PGSQL_FATAL_ERROR || $status === PGSQL_BAD_RESPONSE) {
        $state = pg_result_error_field($result, PGSQL_DIAG_SQLSTATE) ?: null;
        $message = pg_result_error_field($result, PGSQL_DIAG_MESSAGE_PRIMARY) ?: 'PostgreSQL rejected the query.';
        $httpStatus = in_array($state, ['42501', '25006'], true) ? 403 : 400;
        throw new ApiError($httpStatus, 'query_failed', $message, $state);
    }
    if ($status !== PGSQL_TUPLES_OK) {
        throw new ApiError(400, 'no_result_set', 'Submit a SQL statement that returns rows.');
    }
    return $result;
}

function stream_result(\PgSql\Result $result): void
{
    $columns = [];
    for ($i = 0; $i < pg_num_fields($result); $i++) {
        $columns[] = [
            'name' => pg_field_name($result, $i),
            'type' => pg_field_type($result, $i),
            'oid' => pg_field_type_oid($result, $i),
        ];
    }
    header('Content-Type: application/x-ndjson; charset=utf-8');
    echo encode_json(['type' => 'schema', 'version' => 1, 'columns' => $columns]) . "\n";
    while (($row = pg_fetch_row($result)) !== false) {
        // libpq text values avoid numeric rounding and retain PostgreSQL types.
        echo encode_json(['type' => 'row', 'values' => $row]) . "\n";
    }
    echo encode_json(['type' => 'complete', 'row_count' => pg_num_rows($result)]) . "\n";
}

function run(): void
{
    ini_set('display_errors', '0');
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    $connection = null;
    $streaming = false;
    try {
        $path = getenv('SQL_API_CONFIG') ?: dirname(__DIR__) . '/config.php';
        if (!is_file($path)) {
            throw new ApiError(503, 'not_configured', 'The SQL API has not been configured.');
        }
        $config = require $path;
        if (!is_array($config) || empty($config['db_host'])) {
            throw new ApiError(503, 'not_configured', 'The SQL API configuration is incomplete.');
        }
        validate_request($_SERVER, $config);
        [$username, $password] = credentials($_SERVER);
        [$sql, $params, $database] = parse_request(file_get_contents('php://input'));
        if (!extension_loaded('pgsql')) {
            throw new ApiError(503, 'not_configured', 'The PHP pgsql extension is required.');
        }
        $connection = connect($config, $username, $password, $database);
        $result = execute($connection, $sql, $params);
        $streaming = true;
        stream_result($result);
    } catch (\Throwable $error) {
        if (!$error instanceof ApiError) {
            // Log the class only; exception messages may contain SQL/credentials.
            error_log('SQL API internal error: ' . get_class($error));
            $error = new ApiError(500, 'internal_error', 'The SQL API encountered an internal error.');
        }
        $body = ['error' => ['code' => $error->errorCode, 'message' => $error->getMessage()]];
        if ($error->sqlstate !== null) {
            $body['error']['sqlstate'] = $error->sqlstate;
        }
        if (!$streaming) {
            http_response_code($error->status);
            header('Content-Type: application/json; charset=utf-8');
            if ($error->status === 401) {
                header('WWW-Authenticate: Basic realm="PostgreSQL", charset="UTF-8"');
            } elseif ($error->status === 405) {
                header('Allow: POST');
            }
        } else {
            $body['type'] = 'error';
        }
        echo encode_json($body) . "\n";
    } finally {
        if ($connection !== null) {
            pg_close($connection);
        }
    }
}
