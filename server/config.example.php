<?php
declare(strict_types=1);

// Copy to config.php. This file lives outside the public web directory.
// The client supplies the PostgreSQL username/password, never the host/database.
return [
    'db_host' => '127.0.0.1',
    'db_port' => 5432,
    'db_name' => '', // Set your PostgreSQL database name before deployment.
    'connect_timeout' => 10,
    // Only for a local development server; production requests require HTTPS.
    'allow_http_on_loopback' => false,
];
