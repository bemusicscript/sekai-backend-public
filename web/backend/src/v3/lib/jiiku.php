<?php

const ROUTES = [
    "analyze_team" => "handle_jiiku_api",
    "analysis" => "handle_jiiku_analysis",
    "update" => "handle_super_api",
];
const MAX_JIIKU_PAYLOAD = 300 * 1024;
const MAX_SUPER_PAYLOAD = 1024 * 1024;
const ALLOWED_REFS = [
    "refs/heads/main",
    "refs/heads/dev"
];

function error_response(string $msg)
{
    return [
        "type" => "error",
        "error" => $msg
    ];
}

function pick_target_domain($target)
{
    if ($target === "refs/heads/dev" || $target === "beta") {
        return JIIKU_DOMAIN_DEV;
    }
    return JIIKU_DOMAIN_PROD;
}








function forward_request($domain, $path, $data)
{
    $context = stream_context_create([
        'http' => [
            "timeout" => 3,
            "method" => "POST",
            'ignore_errors' => true, // important: lets you read 4xx/5xx response body
            "header" => [
                "Content-Type: application/json",
                "Content-Length: " . strlen($data)
            ],
            "content" => $data
        ]
    ]);

    $response = file_get_contents("http://{$domain}/{$path}", false, $context);
    $status_line = $http_response_header[0] ?? 'no status line';
    $headers = $http_response_header ?? [];

    if ($response === false) {
        error_log("forward_request failed");
        error_log("Status: " . $status_line);
        error_log("Headers: " . json_encode($headers));
        error_log("Request body: " . $data);

        return error_response("invalid path");
    }

    // Log backend errors with body included
    if (preg_match('#HTTP/\S+\s+([0-9]{3})#', $status_line, $matches)) {
        $status_code = (int)$matches[1];

        if ($status_code >= 400) {
            error_log("backend returned error");
            error_log("Status: " . $status_line);
            error_log("Headers: " . json_encode($headers));
            error_log("Request body: " . $data);
            error_log("Response body: " . $response);

            return error_response("backend returned {$status_code}");
        }
    }

    $decoded = json_decode($response);
    if (json_last_error() !== JSON_ERROR_NONE)
        return error_response("invalid backend response");

    return [
        "type" => "success",
        "data" => $decoded
    ];
}

function sanitize_commit($commit)
{
    return substr(preg_replace('/[^a-f0-9]/', '', $commit), 0, 40);
}

function sanitize_ref($ref)
{
    return preg_replace('/[^a-zA-Z0-9\/_.-]/', '', $ref);
}

function handle_jiiku_analysis($path)
{
    global $version, $redis, $mysqli, $cipher, $mawashi_list, $bypass_list;

    $domain = pick_target_domain($version);
    require_once("event.php");

    $eid = (int) ($_GET['eid'] ?? 0);
    $current_event = fetch_event_info_cached();
    $event_id = (int) $current_event['id'];

    // validate inputs
    if (!isset($_GET['player']) || !is_array($_GET['player']))
        return error_response("no hack");
    $players = [];
    foreach ($_GET['player'] as $player) {
        if (!is_string($player))
            return error_response("no hack");
        $profile_id = @profile_decrypt($player, $event_id);
        if (!$profile_id || !is_numeric($profile_id))
            return error_response("no hack");
        $players[] = $profile_id;
    }
    if (count($players) >= 5 || count($players) <= 0)
        return error_response("no hack");
    if (!isset($_GET['type']) || !is_string($_GET['type']))
        return error_response("no hack");

    $character_id = $_GET['type'];
    if (is_numeric($character_id)) {
        $character_id = strval((int) $character_id);
    } elseif ($character_id !== "all") {
        return error_response("no hack");
    }

    // get player information and prepare for requests
    $post_data = [
        "team" => [
            "cards" => [],
            "eventId" => $event_id,
            "teamPower" => null,
        ],
        "graph" => [
            "timestamps" => [],
            "points" => [],
        ]
    ];

    $results = [];
    foreach ($players as $player) {
        $cache_key = "jiiku_analysis:{$eid}:{$character_id}:{$player}";
        $cached = $redis->get($cache_key);
        if ($cached !== false) {
            $results[] = json_decode($cached, true);
            continue;
        }

        $player_info = fetch_profile($player);
        $player_info = $player_info["data"] ?? null;
        if (!$player_info || !is_array($player_info))
            continue;

        $post_data["team"]["cards"] = array_map(function ($card) {
            $c = array_combine(array_map('trim', array_keys($card)), array_values($card));
            return [
                "cardId"     => (int) ($c["cardId"] ?? 0),
                "masterRank" => (int) ($c["masterRank"] ?? 0),
            ];
        }, $player_info["profile_decks"]);
        $post_data["team"]["teamPower"] = $player_info["profile_score"]["totalPower"];

        $graph = [];
        if ($character_id == "all") {
            $graph = $player_info["profile_graph"]["all"];
        } else {
            $graph = $player_info["profile_graph"]["wl"][intval($character_id)];
        }
        $timestamps = $graph["x"];
        $points = $graph["y"];

        if ($eid === 0) {
            $len = count($timestamps);

            $ts = [];
            $ps = [];

            for ($i = 0; $i < $len; $i += 5) {
                $ts[] = $timestamps[$i];
                $ps[] = $points[$i];
            }

            $post_data["graph"]["timestamps"] = $ts;
            $post_data["graph"]["points"] = $ps;
        } else {
            $post_data["graph"]["timestamps"] = $timestamps;
            $post_data["graph"]["points"] = $points;
        }

        $payload = [
            "players" => [
                $post_data,
            ]
        ];

        $result = forward_request($domain, "api/analyze_player", json_encode($payload));
        if (($result["type"] ?? "") !== "success")
            continue;
        $player_data = $result["data"]->players[0] ?? null;
        if (!$player_data)
            continue;
        $redis->setex($cache_key, 300, json_encode($player_data));
        $results[] = $player_data;
    }

    $cache_key = "jiiku_analysis_stats:{$event_id}:{$character_id}";
    $cached = json_decode($redis->get($cache_key), true);

    return ["type" => "success", "data" => [
        "players" => $results,
        "populationStats" => $cached,
    ]];
}

function handle_jiiku_api($path)
{
    global $version;
    $domain = pick_target_domain($version);
    $data = file_get_contents("php://input");
    if ($data === false)
        return error_response("input failed");
    if (strlen($data) > MAX_JIIKU_PAYLOAD)
        return error_response("too big");
    $inflated = gzinflate($data);
    if ($inflated === false)
        return error_response("invalid payload");
    $payload = xor_string($inflated, "2");
    return forward_request($domain, "api/$path", $payload);
}

function handle_super_api($path)
{
    require_once("helper.php");

    $signature_check = $_SERVER['HTTP_X_HUB_SIGNATURE_256'] ?? '';
    if (!$signature_check)
        return error_response("invalid access");

    // parse POST headers
    $data = file_get_contents("php://input");
    if ($data === false)
        return error_response("input failed");
    if (strlen($data) > MAX_SUPER_PAYLOAD)
        return error_response("too big");

    if (!verify_webhook_signature($data)) {
        send_discord_webhook("🔴 Build Failed: invalid signature");
        return error_response("signature failed");
    }

    $parsed_body = json_decode($data, true);
    if (json_last_error() !== JSON_ERROR_NONE)
        return error_response("invalid json");

    $commit = sanitize_commit($parsed_body['commit'] ?? '');
    $ref = sanitize_ref($parsed_body['ref'] ?? '');

    if ($commit !== '') {
        $discord_message = "🚀  Build Requested\n";
        // commit and check ref
        if (!in_array($ref, ALLOWED_REFS, true)) {
            return error_response("wrong refs ($ref)");
        }
        // send message for build
        $discord_message .= "- Commit: `" . $commit . "`\n";
        $discord_message .= "- Ref: `" . $ref . "`";
    } else {
        $parsed_body['event'] = "push";
        $parsed_body['commit'] = "skip";
        $parsed_body['ref'] = JIIKU_DEFAULT_BRANCH;
        $ref = $parsed_body['ref'];
        $discord_message = "🚀  Build Requested (sekai-modules) for `" . $ref . "`";
        $data = json_encode($parsed_body);
    }
    send_discord_webhook($discord_message);

    // pick server domain based on ref
    $current_server_domain = pick_target_domain($ref);
    return forward_request($current_server_domain, $path, $data);
}

function handle_jiiku($path)
{
    global $response_obfuscate;

    $handler = ROUTES[$path] ?? null;
    if ($handler === null)
        return error_response("no hack");

    if ($handler === "handle_super_api") {
        if (!hash_equals((string) ($_GET['key'] ?? ''), JIIKU_KEY)) {
            return error_response("invalid key");
        }
        $response_obfuscate = false;
    }

    return $handler($path);
}

function verify_webhook_signature($payload)
{
    $signature256 = $_SERVER['HTTP_X_HUB_SIGNATURE_256'] ?? '';
    $signature1 = $_SERVER['HTTP_X_HUB_SIGNATURE'] ?? '';

    // If no signature header, reject
    if (!$signature256 && !$signature1) {
        http_response_code(401);
        return false;
    }

    if ($signature256) {
        $hash = 'sha256';
        $signature = str_replace('sha256=', '', $signature256);
    } else {
        $hash = 'sha1';
        $signature = str_replace('sha1=', '', $signature1);
    }

    $computed = hash_hmac($hash, $payload, JIIKU_SECRET);

    // Timing-safe comparison
    if (!hash_equals($computed, $signature)) {
        http_response_code(418);
        return false;
    }

    return true;
}
