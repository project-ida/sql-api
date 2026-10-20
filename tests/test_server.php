<?php
declare(strict_types=1);

require __DIR__ . '/../server/src/api.php';

$checks = 0;
function check(bool $condition, string $message): void
{
    global $checks;
    $checks++;
    if (!$condition) {
        throw new RuntimeException($message);
    }
}
function fails(callable $call, int $status): void
{
    try {
        $call();
    } catch (\SqlApi\ApiError $error) {
        check($error->status === $status, 'Unexpected HTTP error status');
        return;
    }
    throw new RuntimeException('Expected ApiError');
}

$request = ['HTTPS' => 'on', 'REQUEST_METHOD' => 'POST', 'CONTENT_TYPE' => 'application/json; charset=utf-8'];
\SqlApi\validate_request($request, []);
fails(fn() => \SqlApi\validate_request(array_replace($request, ['HTTPS' => '', 'HTTP_X_FORWARDED_PROTO' => 'https']), []), 400);
fails(fn() => \SqlApi\validate_request(array_replace($request, ['REQUEST_METHOD' => 'GET']), []), 405);
fails(fn() => \SqlApi\validate_request(array_replace($request, ['CONTENT_TYPE' => 'text/plain']), []), 415);
\SqlApi\validate_request(array_replace($request, ['HTTPS' => '', 'REMOTE_ADDR' => '127.0.0.1']), ['allow_http_on_loopback' => true]);
fails(fn() => \SqlApi\validate_request(array_replace($request, ['HTTPS' => '', 'REMOTE_ADDR' => '192.0.2.1']), ['allow_http_on_loopback' => true]), 400);

check(\SqlApi\credentials(['HTTP_AUTHORIZATION' => 'Basic ' . base64_encode("reader:p'a:ss\\word")]) === ['reader', "p'a:ss\\word"], 'Basic credentials were altered');
check(\SqlApi\credentials(['PHP_AUTH_USER' => 'reader', 'PHP_AUTH_PW' => 'secret']) === ['reader', 'secret'], 'PHP credentials were altered');
fails(fn() => \SqlApi\credentials([]), 401);
fails(fn() => \SqlApi\credentials(['HTTP_AUTHORIZATION' => 'Basic invalid']), 401);
fails(fn() => \SqlApi\credentials(['PHP_AUTH_USER' => 'reader', 'PHP_AUTH_PW' => '']), 401);

check(\SqlApi\conninfo_value("p'a\\ss") === "'p\\'a\\\\ss'", 'Connection-string quoting is wrong');
fails(fn() => \SqlApi\conninfo_value("bad\0value"), 400);
[$sql, $params, $database] = \SqlApi\parse_request('{"database":"sample_database","sql":"WITH x AS (SELECT $1) SELECT * FROM x;","params":[false,true,null,9223372036854775808]}');
check(str_starts_with($sql, 'WITH'), 'SQL was rewritten');
check($params === ['false', 'true', null, '9223372036854775808'], 'Parameter precision/types changed');
check($database === 'sample_database', 'Requested database was not preserved');
foreach (['[]', '{}', '{', '{"database":"sample_database","sql":""}', '{"database":"sample_database","sql":"SELECT 1","params":{}}', '{"database":"sample_database","sql":"SELECT 1","params":[[]]}', '{"database":"sample_database","sql":"SELECT 1","params":["a\u0000b"]}'] as $body) {
    fails(fn() => \SqlApi\parse_request($body), 400);
}
fails(fn() => \SqlApi\parse_request('{"sql":"SELECT 1"}'), 400);
foreach ([null, '', '   ', "bad\0name", 123, false, [], new stdClass()] as $value) {
    $body = json_encode(['database' => $value, 'sql' => 'SELECT 1']);
    fails(fn() => \SqlApi\parse_request($body), 400);
}
$name = "data 'quoted' \\ name = example";
[, , $database] = \SqlApi\parse_request(json_encode(['database' => $name, 'sql' => 'SELECT 1']));
check($database === $name, 'Database names containing connection-string characters were altered');
echo "Passed $checks PHP checks.\n";
