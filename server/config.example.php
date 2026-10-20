<?php
declare(strict_types=1);

// Copy to config.php. This file lives outside the public web directory.
// The client supplies the PostgreSQL username, password, and database name.
// The host and port are fixed here on the server.
return [
    'db_host' => '127.0.0.1',
    'db_port' => 5432,
    'connect_timeout' => 10,
    // Only for a local development server; production requests require HTTPS.
    'allow_http_on_loopback' => false,
];
