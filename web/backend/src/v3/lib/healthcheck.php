<?php

$redis = init_redis();
if (!$redis) {
    http_response_code(429);
    $result['success'] = false;
    $result['error'] = "redis dead";
    return $result;
}
$redis->select(3);

function healthcheck()
{
    global $redis;
    $result = [
        "active" => (int) $redis->get("ACTIVE_COUNT"),
    ];
    return $result;
}
