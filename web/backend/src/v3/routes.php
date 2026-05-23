<?php

require_once("const.php");
require_once("database.php");

$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
if (in_array($origin, $allowed_origins, true)) {
    header("Access-Control-Allow-Origin: $origin");
    header("Vary: Origin");
}

$category = $params[0] ?? '';
$action   = $params[1] ?? '';

function handle_route($handler, $cache = 60)
{
    global $cache_time, $is_debug;
    $cache_time = $cache;

    try {
        return [
            'version' => API_VERSION,
            'result' => $handler(),
            'success' => true
        ];
    } catch (Throwable $e) {
        if (http_response_code() === 200) http_response_code(500);
        return [
            'version' => API_VERSION,
            'success' => false,
            'error' => $is_debug ? $e->getMessage() : "system error"
        ];
    }
}

switch ("$category/$action") {
    case "healthcheck/":
        require("lib/healthcheck.php");
        $result = handle_route(fn() => healthcheck(), 60);
        break;

    case "event/all":
        require_once("lib/event.php");
        $result = handle_route(fn() => fetch_event_all(), 3600);
        break;

    case "event/info":
        require_once("lib/event.php");
        $result = handle_route(fn() => fetch_event_info_cached(), 3600);
        break;

    case "event/bloom":
        require_once("lib/event.php");
        $result = handle_route(fn() => fetch_bloom_cached(), 3600);
        break;

    case "event/prediction":
        require_once("lib/event.php");
        $result = handle_route(fn() => fetch_prediction_cached(), 1800);
        break;

    case "event/scoreboard":
        require_once("lib/event.php");
        $result = handle_route(fn() => fetch_scoreboard(), 60);
        break;

    case "event/profile":
        require_once("lib/event.php");
        $result = handle_route(fn() => fetch_profile(), 60);
        break;

    case "event/jiiku":
        require_once("lib/jiiku.php");
        $additional_path = $params[2];
        $result = handle_route(fn() => handle_jiiku($additional_path), 300);
        break;

    case "mysekai/ikea":
        require_once("lib/mysekai.php");
        $response_obfuscate = false;
        $result = handle_route(fn() => handle_mysekai_ikea(), 3600);
        break;

    case "statistics/scoreboard":
        require_once("lib/statistics_runner.php");
        $result = handle_route(fn() => fetch_statistics(), 3600);
        break;

    case "statistics/banners":
        require_once("lib/statistics_banner.php");
        $result = handle_route(fn() => fetch_statistics(), 3600);
        break;

    case "statistics/network":
        require_once("lib/statistics_network.php");
        $result = handle_route(fn() => fetch_statistics(), 3600);
        break;

    case "extra/refresh":
        require_once("lib/extra.php");
        $response_obfuscate = false;
        $result = handle_route(fn() => handle_refresh_info(), 3600);
        break;

    default:
        http_response_code(404);
        $result['version'] = API_VERSION;
        $result['error'] = "invalid path";
        break;
}

return $result;
