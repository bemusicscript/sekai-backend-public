<?php

require_once("helper.php");

$redis = init_redis();
if (!$redis) {
    http_response_code(429);
    $result['success'] = false;
    $result['error'] = "redis dead";
    return $result;
}
$redis->select(3);

function cached(string $key, int $ttl, callable $fetch): mixed
{
    global $redis;
    $value = $redis->get($key);
    if ($value !== false) return json_decode($value, true);
    $data = $fetch();
    if ($data !== null) {
        $redis->setex($key, $ttl, json_encode($data));
    }
    return $data;
}

function fetch_statistics(): array
{
    $key = "statistics:network:v2";
    return cached($key, 3600, fn() => compute_statistics());
}

function compute_statistics(): array
{
    $params = [];
    $url = ANALYTICS_URL;

    $ctx = stream_context_create([
        'http' => [
            'timeout' => 30,
            'ignore_errors' => true,
            'header' => "Accept: application/json\r\n",
        ],
    ]);

    $body = @file_get_contents($url, false, $ctx);
    if ($body === false) {
        throw new RuntimeException("Failed to fetch analytics from gateway");
    }

    $data = json_decode($body, true);
    if (!is_array($data) || !isset($data['summary'])) {
        throw new RuntimeException("Invalid analytics response");
    }

    return [
        'since'      => $data['since']      ?? null,
        'until'      => $data['until']      ?? null,
        'summary'    => $data['summary'],
        'requests'   => $data['requests']   ?? ['countries' => []],
        'web_vitals' => $data['web_vitals'] ?? ['available' => false],
    ];
}
