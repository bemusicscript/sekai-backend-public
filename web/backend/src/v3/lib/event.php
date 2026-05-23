<?php

require_once("encrypt.php");
require_once("helper.php");
$mawashi_list = require("mawashi.php");
$bypass_list = require("bypass.php");

$redis = init_redis();
if (!$redis) {
    http_response_code(429);
    $result['success'] = false;
    $result['error'] = "redis dead";
    return $result;
}
$redis->select(3);

$mysqli = init_mysqli();
if (!$mysqli) {
    http_response_code(429);
    $result['success'] = false;
    $result['error'] = "mysql dead";
    return $result;
}

function safe_json_fetch(string $url, ?array $context_opts = null): ?array
{
    $context = $context_opts ? stream_context_create($context_opts) : null;
    $data = file_get_contents($url, false, $context);
    if ($data === false) {
        return null;
    }
    $decoded = json_decode($data, true);
    if (json_last_error() !== JSON_ERROR_NONE) {
        return null;
    }
    return $decoded;
}

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

function fetch_event_info($eid = null)
{
    if ($eid) {
        return safe_json_fetch("http://" . API_DOMAIN . "/current_event/" . (int) $eid);
    } else {
        return safe_json_fetch("http://" . API_DOMAIN . "/current_event");
    }
}

function fetch_event_json(): mixed
{
    return safe_json_fetch("http://" . API_DOMAIN . "/json/events.json");
}

function build_event_units(): array
{
    $event_units = [];

    $events_json = safe_json_fetch("http://" . API_DOMAIN . "/json/events.json");
    if ($events_json !== null) {
        foreach ($events_json as $event) {
            $event_id = $event['id'];
            $event_unit = $event['unit'];
            if (isset($event_units[$event_id])) continue;
            $event_units[$event_id] = $event_unit !== "none" ? $event_unit : null;
        }
    }

    $stories_json = safe_json_fetch("http://" . API_DOMAIN . "/json/eventStoryUnits.json");
    if ($stories_json !== null) {
        foreach ($stories_json as $event) {
            $event_id = (int) $event['eventStoryId'];
            $event_unit = $event['unit'];
            $event_type = $event['eventStoryUnitRelation'];
            if ($event_units[$event_id]) continue;
            if ($event_type == "main") {
                $event_units[$event_id] = $event_unit;
            }
        }
    }

    return $event_units;
}

function fetch_event_units_cached(): array
{
    return cached("EVENT_UNITS", 3600, fn() => build_event_units()) ?? [];
}

function fetch_bloom()
{
    // fetch current WL and return array
    $res = safe_json_fetch("http://" . API_DOMAIN . "/current_bloom");
    if (!$res) return null;
    $result = [];
    foreach ($res as $record) {
        $event_id = (int) $record['eventId'];
        $character_id = (int) ($record['gameCharacterId'] ?? 0);
        if (!isset($result[$event_id])) {
            $result[$event_id] = [];
        }
        $result[$event_id][$character_id] = [
            "chapter_start" => $record['chapterStartAt'] / 1000,
            "chapter_end" => $record['aggregateAt'] / 1000,
        ];
    }

    return $result;
}

function fetch_prediction()
{
    return safe_json_fetch("http://" . API_DOMAIN . "/predict/");
}

function fetch_event_info_cached($eid=0)
{
    if ($eid) {
        $eid = (int) $eid;
    } else {
        $eid = (int) ($_GET['eid'] ?? 0);
    }
    return cached("EVENT_INFO:" . (int) $eid, 3600, fn() => fetch_event_info($eid));
}

function fetch_event_json_cached()
{
    return cached("EVENT_ALL", 3600, fn() => fetch_event_json());
}

function fetch_bloom_cached()
{
    return cached("EVENT_BLOOM", 3600, fn() => fetch_bloom());
}


function fetch_event_all()
{
    // Only show what is needed to show

    $result = [];
    $data = array_reverse(fetch_event_json_cached());
    foreach ($data as $event_info) {
        $result[] = [
            "id" => $event_info['id'],
            "name" => $event_info['name'],
            "eventOnlyComponentDisplayStartAt" => $event_info['eventOnlyComponentDisplayStartAt']
        ];
    }

    return $result;
}

function fetch_prediction_cached()
{
    return cached("EVENT_PREDICTION", 1200, fn() => fetch_prediction());
}

// --- load_profile helpers ---

function profile_exists(int $profile_id): bool
{
    global $mysqli;
    $stmt = $mysqli->prepare("SELECT scoreboard_profile_id FROM scoreboard_current WHERE scoreboard_profile_id=? UNION SELECT scoreboard_profile_id FROM scoreboard WHERE scoreboard_profile_id=? LIMIT 1");
    $stmt->bind_param('ii', $profile_id, $profile_id);
    $stmt->execute();
    return $stmt->get_result()->fetch_assoc() !== null;
}

function sync_profile_honors(int $profile_id, array $honors): void
{
    global $mysqli;
    $stmt = $mysqli->prepare("INSERT IGNORE INTO profile_honors (scoreboard_profile_id, scoreboard_profile_honor_id) VALUES (?, ?)");
    $mysqli->begin_transaction();
    try {
        foreach ($honors as $honorId) {
            $honorId = (int) $honorId;
            $stmt->bind_param("ii", $profile_id, $honorId);
            if (!$stmt->execute()) {
                throw new Exception($stmt->error);
            }
        }
        $mysqli->commit();
    } catch (Exception $e) {
        $mysqli->rollback();
        throw $e;
    }
}

function build_deck_cards(array $decks, array $cards): array
{
    $deck_ids = [];
    for ($i = 1; $i <= 5; $i++) {
        $deck_ids[] = (int) $decks["member{$i}"];
    }
    $card_assets = safe_json_fetch("http://" . API_DOMAIN . "/cards/" . implode(",", $deck_ids)) ?? [];
    $cards_by_id = array_column($cards, null, 'cardId');
    $deck_cards = [];
    foreach ($deck_ids as $deck_id) {
        if (isset($cards_by_id[$deck_id])) {
            $card = $cards_by_id[$deck_id];
            $card['cardAsset'] = $card_assets[$deck_id] ?? null;
            $deck_cards[] = $card;
        }
    }
    return $deck_cards;
}

function empty_graph_shape(): array
{
    return ["all" => ["x" => [], "y" => [], "r" => []], "wl" => []];
}

// ---

function fetch_scoreboard()
{
    global $mysqli, $mawashi_list, $redis;

    $eid = (int) ($_GET['eid'] ?? 0);
    $current_event = fetch_event_info_cached();
    $event_id = (int) $current_event['id'];

    $bloom_data = fetch_bloom_cached();
    $current_bloom = $bloom_data[$event_id] ?? [];

    if ($eid) {
        $is_past = true;
        $target_table_name = "scoreboard";
    } else {
        $is_past = false;
        $target_table_name = "scoreboard_current";
    }

    $scoreboard_status = [
        "round" => 0,
        "updated" => -1,
    ];

    // Get chapter lists
    $chapter_list = [0];
    foreach ($current_bloom as $chapter_name => $chapter_duration) {
        $chapter_list[] = $chapter_name;
    }

    // Calculate rounds (optimized)
    // this should be 100x faster than GROUP BY..
    $round_query = "";
    foreach ($chapter_list as $chapter_id) {
        $round_query_template = "COALESCE((SELECT scoreboard_round FROM $target_table_name WHERE scoreboard_event_id=%d AND scoreboard_type=%d ORDER BY scoreboard_round DESC LIMIT 1), 0) AS round_%d, ";
        $round_query .= sprintf($round_query_template, $event_id, $chapter_id, $chapter_id);
    }
    $round_query = substr($round_query, 0, -2);
    $stmt = $mysqli->prepare("SELECT $round_query LIMIT 1");
    $stmt->execute();
    $res = $stmt->get_result()->fetch_assoc();



    if (!$res || !$res['round_0'] || $res['round_0'] == "0") {
        $result["message"] = "not updated yet";
        $result["extra"] = $scoreboard_status;
        return;
    }

    $scoreboard_status = [
        "round" => $res,
    ];

    // Collect active chapters and their round info
    $active_chapters = [];
    $seen = [];
    foreach ($chapter_list as $chapter_id) {
        if (in_array($chapter_id, $seen))
            continue;
        $seen[] = $chapter_id;

        $round = (int) $scoreboard_status["round"]["round_" . $chapter_id];
        if ($round == 0)
            continue;

        $last_hour_round = $is_past ? $round - 12 : $round - 60;
        $active_chapters[] = [
            'id' => $chapter_id,
            'round' => $round,
            'last_hour_round' => $last_hour_round,
        ];
    }

    // Build two UNION ALL queries: one for current+last_hour join, one for banner data
    // This reduces 2*N queries down to 2 total, regardless of chapter count.
    $unions_current = [];
    $params_current = [];
    $types_current = '';

    $unions_banner = [];
    $params_banner = [];
    $types_banner = '';

    foreach ($active_chapters as $ch) {
        $unions_current[] = "
            SELECT
                score_current.scoreboard_type,
                score_current.scoreboard_profile_id,
                score_current.scoreboard_nickname,
                score_current.scoreboard_score,
                score_last_hour.scoreboard_score AS scoreboard_score_last_hour,
                score_current.scoreboard_info_cheerful,
                score_current.scoreboard_info_card
            FROM $target_table_name AS score_current
            LEFT OUTER JOIN
                (SELECT scoreboard_profile_id, scoreboard_type, scoreboard_score
                 FROM $target_table_name
                 WHERE scoreboard_event_id=? AND scoreboard_round=? AND scoreboard_type=?) AS score_last_hour
            ON score_current.scoreboard_profile_id = score_last_hour.scoreboard_profile_id
               AND score_current.scoreboard_type = score_last_hour.scoreboard_type
            WHERE score_current.scoreboard_event_id=?
              AND score_current.scoreboard_round=?
              AND score_current.scoreboard_type=?
        ";
        array_push($params_current, $event_id, $ch['last_hour_round'], $ch['id'], $event_id, $ch['round'], $ch['id']);
        $types_current .= 'iiiiii';

        $unions_banner[] = "
            SELECT scoreboard_type, scoreboard_score, scoreboard_rank, scoreboard_round
            FROM $target_table_name
            WHERE scoreboard_event_id=? AND scoreboard_round=? AND scoreboard_type=?
        ";
        array_push($params_banner, $event_id, $ch['last_hour_round'], $ch['id']);
        $types_banner .= 'iii';
    }

    // Fetch all current data (1 query)
    // ORDER BY type groups chapters together, then score DESC within each chapter
    $result_all = [];
    $result_wl = [];

    if ($unions_current) {
        $query = implode(" UNION ALL ", $unions_current) . " ORDER BY scoreboard_type ASC, scoreboard_score DESC";
        $stmt = $mysqli->prepare($query);
        $stmt->bind_param($types_current, ...$params_current);
        $stmt->execute();
        $data = $stmt->get_result()->fetch_all(MYSQLI_ASSOC);

        foreach ($data as $value) {
            $character_id = $value['scoreboard_type'];
            $value['scoreboard_profile_id_hash'] = sha1(HASH_SALT . $value['scoreboard_profile_id'] . HASH_SALT);

            if (array_key_exists($value['scoreboard_profile_id'], $mawashi_list)) {
                $value['scoreboard_dangerous'] = $mawashi_list[$value['scoreboard_profile_id']];
            }
            $value['scoreboard_profile_id'] = profile_encrypt($value['scoreboard_profile_id'], $event_id);
            $value['scoreboard_nickname'] = htmlspecialchars(truncate_string($value['scoreboard_nickname']));
            $value['scoreboard_score'] = (int) $value['scoreboard_score'];
            if ((int) $value['scoreboard_score_last_hour'] > 0)
                $value['scoreboard_score_last_hour'] = (int) $value['scoreboard_score_last_hour'];
            unset($value['scoreboard_type']);

            if ($character_id == "0") {
                $result_all[] = $value;
            } else {
                if (!isset($result_wl[$character_id])) {
                    $result_wl[$character_id] = [];
                }
                $result_wl[$character_id][] = $value;
            }
        }
    }

    // Fetch all banner/last-hour data (1 query)
    if ($unions_banner) {
        $query = implode(" UNION ALL ", $unions_banner) . " ORDER BY scoreboard_type ASC, scoreboard_score DESC";
        $stmt = $mysqli->prepare($query);
        $stmt->bind_param($types_banner, ...$params_banner);
        $stmt->execute();
        $data_last_hour = $stmt->get_result()->fetch_all(MYSQLI_ASSOC);

        $idx = 0;
        $idx_wl = [];
        foreach ($data_last_hour as $value) {
            $character_id = $value['scoreboard_type'];
            if ($character_id == "0") {
                if ((int) $value['scoreboard_score'] > 0)
                    $value['scoreboard_score'] = (int) $value['scoreboard_score'];
                $result_all[$idx]['scoreboard_score_last_hour_banner'] = $value['scoreboard_score'];
                $idx += 1;
            } else {
                if (!isset($idx_wl[$character_id])) {
                    $idx_wl[$character_id] = 0;
                }

                if ((int) $value['scoreboard_score'] > 0)
                    $value['scoreboard_score'] = (int) $value['scoreboard_score'];
                $result_wl[$character_id][$idx_wl[$character_id]]['scoreboard_score_last_hour_banner'] = $value['scoreboard_score'];

                $idx_wl[$character_id] += 1;
            }
        }
    }

    $result["type"] = "success";
    $result["data"] = [
        "all" => $result_all,
        "wl" => $result_wl
    ];
    $result["extra"] = $scoreboard_status;

    return $result;
}


function load_profile($profile_id, $event_info=null)
{
    global $mysqli, $mawashi_list;
    $event_id = (int) $event_info['id'];
    $event_date_end = (int) $event_info['aggregateAt'] / 1000;

    // check if the profile ever existed from scoreboard (validation check)
    if (!profile_exists($profile_id)) {
        return [];
    }

    // fetch profile from cache
    $stmt = $mysqli->prepare("SELECT * FROM profile WHERE profile_id=? AND profile_event_id=? LIMIT 1");
    $stmt->bind_param('ii', $profile_id, $event_id);
    $stmt->execute();
    $res = $stmt->get_result()->fetch_assoc();

    $is_created = $res !== null;
    $is_updated = $is_created && (time() <= ((int) $res['profile_updated']) + 600);
    $is_event_over = time() > $event_date_end;

    if (!$is_created || (!$is_updated && !$is_event_over)) {
        $profile_data = safe_json_fetch("http://" . API_DOMAIN . "/profile/{$profile_id}", ['http' => ['timeout' => 5]]);
        if (!$profile_data) {
            return [];
        }
        unset($profile_data['userDeck']['userId']);

        $new_profile_id = (int) $profile_data['userProfile']['userId'];
        $new_profile_nickname = $profile_data['user']['name'];
        $new_profile_total_score = $profile_data['totalPower'];
        $new_profile_updated = time();

        // adding honors to profile honors cache
        $honors = array_values(array_unique(array_merge(
            array_column($profile_data['userProfileHonors'], 'honorId'),
            array_column($profile_data['userHonors'], 'honorId')
        )));
        sync_profile_honors($new_profile_id, $honors);

        $new_profile_decks = build_deck_cards($profile_data['userDeck'], $profile_data['userCards']);

        // create or update depending on situations (prevent race conditions)
        $query = "INSERT INTO profile (profile_id, profile_nickname, profile_decks, profile_total_score, profile_updated, profile_event_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
            profile_nickname = VALUES(profile_nickname),
            profile_decks = VALUES(profile_decks),
            profile_total_score = VALUES(profile_total_score),
            profile_updated = VALUES(profile_updated),
            profile_event_id = VALUES(profile_event_id)";

        $stmt = $mysqli->prepare($query);
        $decks_json = json_encode($new_profile_decks);
        $score_json = json_encode($new_profile_total_score);
        $stmt->bind_param('isssii', $new_profile_id, $new_profile_nickname, $decks_json, $score_json, $new_profile_updated, $event_id);
        $stmt->execute();

        $ret['data'] = [
            "profile_nickname" => htmlspecialchars(truncate_string($new_profile_nickname)),
            "profile_decks" => $new_profile_decks,
            "profile_score" => $new_profile_total_score,
            "profile_updated" => (int) $new_profile_updated,
            "profile_graph" => empty_graph_shape(),
            "border_graph" => empty_graph_shape(),
            "compare_graph" => empty_graph_shape(),
        ];
        $ret['extra'] = "updated";
    } else {
        $ret['data'] = [
            "profile_nickname" => htmlspecialchars(truncate_string($res['profile_nickname'])),
            "profile_decks" => json_decode($res['profile_decks'], true),
            "profile_score" => json_decode($res['profile_total_score'], true),
            "profile_updated" => (int) $res['profile_updated'],
            "profile_graph" => empty_graph_shape(),
            "border_graph" => empty_graph_shape(),
            "compare_graph" => empty_graph_shape(),
        ];
        $ret['extra'] = "not updated";
    }

    if (array_key_exists($profile_id, $mawashi_list)) {
        $ret['data']['profile_dangerous'] = $mawashi_list[$profile_id];
    }

    return $ret;
}

function fetch_profile($profile_id = null, $compare_profile_id = null, $rank_id = null)
{
    global $mysqli;

    $eid = (int) ($_GET['eid'] ?? 0);
    $current_event = fetch_event_info_cached();
    $event_id = (int) $current_event['id'];
    $is_past = (bool) $eid;

    if (!$profile_id)
        $profile_id = profile_decrypt((string) ($_GET['profile_id'] ?? ''), $event_id);
    if (!$compare_profile_id)
        $compare_profile_id = profile_decrypt((string) ($_GET['compare_profile_id'] ?? ''), $event_id);
    $rank_id = (int) ($rank_id ?? ($_GET['rank'] ?? 0));

    // sanitize input
    if (!$profile_id || !is_numeric($profile_id)) {
        $result["message"] = "profile not found";
        // $result["debug"] = $profile_id;
        return;
    }
    if (!$compare_profile_id || !is_numeric($compare_profile_id)) {
        $compare_profile_id = "";
    }
    $profile_id = (int) $profile_id;
    $compare_profile_id = (int) $compare_profile_id;

    // fetch profiles
    $tmp = load_profile($profile_id, $current_event);

    $result['data'] = $tmp['data'];
    $result['extra'] = $tmp['extra'];
    $result['type'] = "success";

    if ($compare_profile_id) {
        $tmp = load_profile($compare_profile_id, $current_event);
        unset($tmp['data']['border_graph']);
        unset($tmp['data']['profile_graph']);
        unset($tmp['data']['compare_graph']);
        $result['data']['compare_info'] = $tmp['data'];
        $result['extra_compare'] = $tmp['extra'];
        populate_graphs($mysqli, $result, $event_id, $profile_id, $compare_profile_id, $is_past, $rank_id);
        get_profile_history($mysqli, $result, $profile_id, $compare_profile_id, $is_past);
    } else {
        populate_graphs($mysqli, $result, $event_id, $profile_id, null, $is_past, $rank_id);
        get_profile_history($mysqli, $result, $profile_id, null, $is_past);
    }

    return $result;
}

function get_profile_history($mysqli, &$result, $profile_id, $compare_profile_id, $is_past)
{
    global $bypass_list;
    $profile_dataset = [
        "profile_history" => [
            "id" => $profile_id
        ],
        "profile_history_compare" => [
            "id" => $compare_profile_id
        ],
    ];
    $event_all = fetch_event_json_cached();
    $event_info = array_column($event_all, null, 'id');
    $event_units = fetch_event_units_cached();

    foreach ($profile_dataset as $profile_type => $profile_data) {
        $query = <<<EOF
            SELECT
                'real' AS method, scoreboard_event_id, scoreboard_rank, scoreboard_profile_id, scoreboard_nickname, scoreboard_score, scoreboard_type
            FROM scoreboard_history
            WHERE scoreboard_profile_id=?
            UNION ALL
            SELECT
                'top' AS method, scoreboard_event_id, scoreboard_rank, scoreboard_profile_id, scoreboard_nickname, 0 AS scoreboard_score, scoreboard_type
            FROM scoreboard_history_top
            WHERE scoreboard_profile_id=? AND scoreboard_rank <= 100
            ORDER BY scoreboard_event_id DESC
        EOF;
        $stmt = $mysqli->prepare($query);
        $stmt->bind_param('ii', $profile_data['id'], $profile_data['id']);
        $stmt->execute();
        $res = $stmt->get_result()->fetch_all(MYSQLI_ASSOC);
        $result['data'][$profile_type] = [];

        foreach ($res as $value) {
            // dont show chapters
            if ($value['scoreboard_type'] != "0")
                $value['method'] = "wl";

            // make null nicknames empty
            if ($value['scoreboard_nickname'] == null)
                $value['scoreboard_nickname'] = "";

            // hide nicknames accordingly
            // in any case, hide ranks too

            if (isset($value['scoreboard_profile_id']) &&
                in_array((string) $value['scoreboard_profile_id'], $bypass_list['hide_nickname'])) {
                if($value['method'] == "real"){
                    continue;
                }
                $nickname = "";
            } else {
                $nickname = htmlspecialchars(truncate_string($value['scoreboard_nickname']));
            }

            $result['data'][$profile_type][] = [
                "event_id" => $value['scoreboard_event_id'],
                "event_name" => $event_info[$value['scoreboard_event_id']]['name'] ?? null,
                "event_start" => $event_info[$value['scoreboard_event_id']]['startAt'] ?? null,
                "event_unit" => $event_units[$value['scoreboard_event_id']] ?? $event_info[$value['scoreboard_event_id']]['unit'] ?? null,
                "nickname" => $nickname,
                "type" => $value['scoreboard_type'],
                "rank" => $value['scoreboard_rank'],
                "score" => $value['scoreboard_score'],
                "method" => $value['method'],
            ];
        }
    }
}

/**
 * Build graph xy-series from a result set, partitioned by scoreboard_type.
 */
function build_graph_series(array $rows, bool $get_rank = true): array
{
    $graph = ['all' => ['x' => [], 'y' => [], 'r' => []], 'wl' => []];
    foreach ($rows as $value) {
        $ts = (int) $value['scoreboard_updated'] * 1000;
        $score = (int) $value['scoreboard_score'];
        $rank = $get_rank ? (int) $value['scoreboard_rank'] : null;

        if ($value['scoreboard_type'] != 0) {
            $type = $value['scoreboard_type'];
            if (!isset($graph['wl'][$type])) {
                $graph['wl'][$type] = ['x' => [], 'y' => [], 'r' => []];
            }
            $graph['wl'][$type]['x'][] = $ts;
            $graph['wl'][$type]['y'][] = $score;
            if ($rank) {
                $graph['wl'][$type]['r'][] = $rank;
            }
        } else {
            $graph['all']['x'][] = $ts;
            $graph['all']['y'][] = $score;
            if ($rank) {
                $graph['all']['r'][] = $rank;
            }
        }
    }
    return $graph;
}

function populate_graphs($mysqli, &$result, $event_id, $profile_id, $compare_profile_id, $is_past, $rank_id)
{
    $target_table_name = $is_past ? "scoreboard" : "scoreboard_current";

    // Profile graph
    $stmt = $mysqli->prepare("SELECT scoreboard_updated, scoreboard_type, scoreboard_score, scoreboard_rank FROM $target_table_name WHERE scoreboard_event_id=? AND scoreboard_profile_id=? ORDER BY scoreboard_updated ASC");
    $stmt->bind_param('ii', $event_id, $profile_id);
    $stmt->execute();
    $result['data']['profile_graph'] = build_graph_series($stmt->get_result()->fetch_all(MYSQLI_ASSOC));

    // Border graph
    // if the rank is out of bounds we just silently end the progress right at the point
    if ($rank_id < 1 || $rank_id > 1000000)
        return;

    $stmt = $mysqli->prepare("SELECT scoreboard_updated, scoreboard_type, scoreboard_score, scoreboard_rank FROM $target_table_name WHERE scoreboard_event_id=? AND scoreboard_rank=? ORDER BY scoreboard_updated ASC");
    $stmt->bind_param('ii', $event_id, $rank_id);
    $stmt->execute();
    $result['data']['border_graph'] = build_graph_series($stmt->get_result()->fetch_all(MYSQLI_ASSOC), false);

    // Compare graph
    if (!$compare_profile_id)
        return;

    $stmt = $mysqli->prepare("SELECT scoreboard_updated, scoreboard_type, scoreboard_score, scoreboard_rank FROM $target_table_name WHERE scoreboard_event_id=? AND scoreboard_profile_id=? ORDER BY scoreboard_updated ASC");
    $stmt->bind_param('ii', $event_id, $compare_profile_id);
    $stmt->execute();
    $result['data']['compare_graph'] = build_graph_series($stmt->get_result()->fetch_all(MYSQLI_ASSOC));
}
