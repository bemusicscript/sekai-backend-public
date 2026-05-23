<?php

declare(strict_types=1);
require_once("../src/config.php");

// Fastest implementation for XOR string
function xor_string($string, $key): string
{
    if ($key === '') {
        throw new Exception('Empty XOR key');
    }

    $len = strlen($string);
    $klen = strlen($key);

    $res = $string ^ str_repeat(
        $key,
        intdiv($len + $klen - 1, $klen)
    );
    return (string) $res;
}

// Setup & Environment
error_reporting(E_ALL); // & ~E_WARNING & ~E_NOTICE & ~E_STRICT); // Report everything, but don't display it
ini_set("display_errors", "0");
date_default_timezone_set("Asia/Tokyo");
$start_time = microtime(true);
$is_debug = false;
$cache_time = 60;
$response_data = ["success" => false];
$response_obfuscate = true;

// CORS & Headers
header("Content-Type: application/json; charset=utf-8");
header("Access-Control-Allow-Methods: GET, POST, PUT, DELETE, PATCH, OPTIONS");
header("Access-Control-Allow-Headers: Content-Type, Authorization, X-Requested-With");
header("Access-Control-Allow-Credentials: true");
header("Access-Control-Expose-Headers: Date, Last-Modified");

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}

// Routing Logic
try {
    $path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
    $parts = array_values(array_filter(explode('/', $path)));
    $version = $parts[0] ?? '';
    $allowed_versions = ['v3']; //'beta'

    if (!in_array($version, $allowed_versions, true)) {
        http_response_code(404);
        throw new Exception("Invalid API version");
    }

    $version_file = __DIR__ . "/../src/{$version}/routes.php";
    if (!file_exists($version_file)) {
        http_response_code(404);
        throw new Exception("Version endpoint map missing");
    }

    // Context for the required file
    $params = array_slice($parts, 1);
    $result = require_once($version_file);

    if (is_array($result)) {
        $response_data = array_merge(
            ["success" => false],
            $result
        );
    }
} catch (Throwable $e) {
    if (http_response_code() === 200)
        http_response_code(404);
    $response_data = [
        "success" => false,
        "error" => $is_debug ? $e->getMessage() : "invalid path",
    ];
}

// Final Output
if ($is_debug) {
    header("X-Runtime: " . (microtime(true) - $start_time));
}

// Prevent Cache Pollution
$current_time = time();
$current_bucket = (int) (floor($current_time / $cache_time) * $cache_time);
$grace_period = 120;

if (isset($_GET['t'])) {
    $request_bucket = (int) $_GET['t'];

    // Prevent Future Pollution
    // The bucket being requested MUST NOT be ahead of the server's current time.
    if ($request_bucket > $current_bucket) {
        // add grace periods though.
        if ($request_bucket > ($current_time + $cache_time + $grace_period)) {
            http_response_code(400);
            $response_data = [
                "success" => false,
                "error" => "cache pollution attempts",
            ];
        }
    // Prevent Stale/Old Buckets
    // Allow until n-1 bucket
    } else if ($request_bucket < ($current_bucket - $cache_time - $grace_period)) {
        http_response_code(400);
        $response_data = [
            "success" => false,
            "error" => "stale cache attempts",
        ];
    }
}

// encode with simple xors and gzdeflate: to prevent from hackers deobfuscating results
$status_code = http_response_code();
if ($status_code <= 400)
    header("Cache-Control: max-age=" . $cache_time);
if ($response_obfuscate) {
    $result = json_encode($response_data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    echo gzdeflate(xor_string($result, ENCODING_KEY), 9);
} else {
    $result = json_encode($response_data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    echo $result;
}

exit;
