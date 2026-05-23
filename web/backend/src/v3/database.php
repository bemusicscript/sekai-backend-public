<?php

$mysqli = null;
$redis = null;

function init_mysqli()
{
    try {
        $mysqli = new mysqli(
            "p:" . MARIADB_HOST,
            MARIADB_USER,
            MARIADB_PASS,
            MARIADB_NAME,
        );
    } catch (Exception $e) {
        return null;
    }

    if (!$mysqli || $mysqli->connect_error) {
        return null;
    }

    $mysqli->set_charset("utf8mb4");
    $mysqli->options(MYSQLI_OPT_INT_AND_FLOAT_NATIVE, 1);

    return $mysqli;
}

function init_redis()
{
    try {
        /** @disregard P1009 Redis xternal library **/
        $redis = new Redis([
            'host' => REDIS_HOST,
            'port' => 6379,
            'connectTimeout' => 3,
            'auth' => [REDIS_PASS],
            'persistent' => true,
            'backoff' => [
                'algorithm' => Redis::BACKOFF_ALGORITHM_DECORRELATED_JITTER,
                'base' => 500,
                'cap' => 750,
            ],
        ]);
        return $redis;
    } catch (Exception $e) {
        return null;
    }
}

function shutdown_database()
{
    // kill mysqli
    global $mysqli;
    if ($mysqli) {
        $mysqli->close();
    }
}

register_shutdown_function('shutdown_database');
