<?php

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
    return cached('statistics:scoreboard', 3600, fn() => compute_statistics());
}

function compute_statistics(): array
{
    global $mysqli, $mawashi_list;

    $event_units = [];
    $events_json = file_get_contents("http://" . API_DOMAIN . "/json/events.json");
    $events_stories_json = file_get_contents("http://" . API_DOMAIN . "/json/eventStoryUnits.json");

    if ($events_json !== false) {
        foreach (json_decode($events_json, true) ?? [] as $event) {
            $event_id = $event['id'];
            $event_unit = $event['unit'];
            if(isset($event_units[$event_id])){
                continue;
            }
            $event_units[$event_id] = $event_unit !== "none" ? $event_unit : null;
        }
    }
    if ($events_stories_json !== false) {
        foreach (json_decode($events_stories_json, true) ?? [] as $event) {
            $event_id = (int) $event['eventStoryId'];
            $event_unit = $event['unit'];
            $event_type = $event['eventStoryUnitRelation'];
            if($event_units[$event_id]){
                continue;
            }

            if($event_type == "main") {
                $event_units[$event_id] = $event_unit;
                continue;
            }
        }
    }

    $query = "
        SELECT scoreboard_profile_id, scoreboard_nickname, scoreboard_rank, scoreboard_event_id
        FROM scoreboard_history
        WHERE scoreboard_type = 0
        AND scoreboard_rank <= 100
        AND scoreboard_event_id NOT IN (166, 186)
        UNION ALL
        SELECT scoreboard_profile_id, scoreboard_nickname, scoreboard_rank, scoreboard_event_id
        FROM scoreboard_history_top
        WHERE scoreboard_type = 0
        AND scoreboard_rank <= 100
        AND scoreboard_event_id NOT IN (166, 186)

        UNION ALL
        SELECT c.scoreboard_profile_id, c.scoreboard_nickname, c.scoreboard_rank, c.scoreboard_event_id
        FROM scoreboard_history_cluster c
        WHERE c.scoreboard_type = 0
        AND c.scoreboard_rank <= 100
        AND c.scoreboard_event_id NOT IN (166, 186)
        AND NOT EXISTS (
            SELECT 1
            FROM scoreboard_history h
            WHERE h.scoreboard_event_id = c.scoreboard_event_id
            AND h.scoreboard_profile_id = c.scoreboard_profile_id
            AND h.scoreboard_type = 0
        )
        AND NOT EXISTS (
            SELECT 1
            FROM scoreboard_history_top t
            WHERE t.scoreboard_event_id = c.scoreboard_event_id
            AND t.scoreboard_profile_id = c.scoreboard_profile_id
            AND t.scoreboard_type = 0
        )

        ORDER BY scoreboard_event_id ASC, scoreboard_rank ASC;
    ";

    $res = $mysqli->query($query);
    if (!$res) throw new RuntimeException("Query failed: " . $mysqli->error);
    $players    = [];
    $total_rows = 0;

    while ($row = $res->fetch_assoc()) {
        $profile_id = (int) $row['scoreboard_profile_id'];
        $rank       = $row['scoreboard_rank'] === null ? null : (int) $row['scoreboard_rank'];

        if ($rank === null) continue;
        $total_rows++;

        $event_id = (int) $row['scoreboard_event_id'];
        $unit     = $event_units[$event_id] ?? 'none';
        $nickname = ($v = trim((string) $row['scoreboard_nickname'])) !== '' ? $v : null;

        $p = &$players[$profile_id];
        $p['counts'][$rank]            = ($p['counts'][$rank] ?? 0) + 1;
        $p['units_by_rank'][$rank][]   = $unit;
        $p['latest_event_id_by_rank'][$rank] = max($p['latest_event_id_by_rank'][$rank] ?? 0, $event_id);
        if ($nickname !== null) {
            $p['nicknames_by_rank'][$rank][] = $nickname;
            $p['all_nicknames'][]            = $nickname;
        }
    }

    return [
        'total_rows' => $total_rows,
        'top3'   => compute_result($players, range(1, 3),   $mawashi_list, 100),
        '1st'    => compute_result($players, [1],           $mawashi_list, 100),
        '2nd'    => compute_result($players, [2],           $mawashi_list, 100),
        '3rd'    => compute_result($players, [3],           $mawashi_list, 100),
        'top10'  => compute_result($players, range(1, 10),  $mawashi_list, 100),
        'top50'  => compute_result($players, range(1, 50),  $mawashi_list, 100),
        'top100' => compute_result($players, range(1, 100), $mawashi_list, 200),
        'coverage' => fetch_coverage(),
    ];
}

function fetch_coverage(): array
{
    global $mysqli;

    $exclude = "AND scoreboard_type = 0 AND scoreboard_event_id NOT IN (166, 186)";

    $range_tiers  = ['top3' => 3, 'top10' => 10, 'top50' => 50, 'top100' => 100];
    $single_tiers = ['1st'  => 1, '2nd'   => 2,  '3rd'   => 3];

    $coverage = [];

    // Range tiers: directly follow your reference query.
    // Universe = events that have at least one row with rank <= n.
    // missing_count = SUM(n - present_count), expected = COUNT(events) * n.
    foreach ($range_tiers as $key => $n) {
        $sql = "
            WITH cluster_dedup AS (
                SELECT c.scoreboard_event_id, c.scoreboard_profile_id
                FROM scoreboard_history_cluster c
                WHERE c.scoreboard_rank <= {$n} {$exclude}
                AND NOT EXISTS (
                    SELECT 1
                    FROM scoreboard_history h
                    WHERE h.scoreboard_event_id = c.scoreboard_event_id
                        AND h.scoreboard_profile_id = c.scoreboard_profile_id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM scoreboard_history_top t
                    WHERE t.scoreboard_event_id = c.scoreboard_event_id
                        AND t.scoreboard_profile_id = c.scoreboard_profile_id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM scoreboard_history_cluster c2
                    WHERE c2.scoreboard_event_id = c.scoreboard_event_id
                        AND c2.scoreboard_profile_id = c.scoreboard_profile_id
                        AND c2.scoreboard_rank < c.scoreboard_rank
                )
            ),
            all_players AS (
                SELECT scoreboard_event_id, scoreboard_profile_id
                FROM scoreboard_history
                WHERE scoreboard_rank <= {$n} {$exclude}

                UNION

                SELECT scoreboard_event_id, scoreboard_profile_id
                FROM scoreboard_history_top
                WHERE scoreboard_rank <= {$n} {$exclude}

                UNION

                SELECT scoreboard_event_id, scoreboard_profile_id
                FROM cluster_dedup
            ),
            per_event AS (
                SELECT scoreboard_event_id, COUNT(*) AS present_count
                FROM all_players
                GROUP BY scoreboard_event_id
            )
            SELECT
                SUM({$n} - present_count) AS missing_count,
                COUNT(*) * {$n} AS expected_count,
                ROUND(SUM({$n} - present_count) * 100.0 / (COUNT(*) * {$n}), 2) AS missing_pct
            FROM per_event;
        ";
        $row = $mysqli->query($sql)?->fetch_assoc();
        $coverage[$key] = $row ? [
            'missing'     => (int)   $row['missing_count'],
            'expected'    => (int)   $row['expected_count'],
            'missing_pct' => (float) $row['missing_pct'],
        ] : null;
    }

    // Single-rank tiers: universe = top3 events (same as the top3 range query).
    // For each event in that universe, check whether the specific rank is present.
    // SUM(scoreboard_rank = N) is 1 if rank N exists for that event, else 0.
    foreach ($single_tiers as $key => $rank) {
        $sql = "
            WITH all_rows AS (
                SELECT scoreboard_event_id, scoreboard_rank FROM scoreboard_history
                WHERE scoreboard_rank <= 3 {$exclude}
            ),
            per_event AS (
                SELECT scoreboard_event_id,
                       SUM(scoreboard_rank = {$rank}) AS present_count
                FROM all_rows GROUP BY scoreboard_event_id
            )
            SELECT
                SUM(1 - present_count)                              AS missing_count,
                COUNT(*)                                            AS expected_count,
                ROUND(SUM(1 - present_count) * 100.0 / COUNT(*), 2) AS missing_pct
            FROM per_event
        ";
        $row = $mysqli->query($sql)?->fetch_assoc();
        $coverage[$key] = $row ? [
            'missing'     => (int)   $row['missing_count'],
            'expected'    => (int)   $row['expected_count'],
            'missing_pct' => (float) $row['missing_pct'],
        ] : null;
    }

    return $coverage;
}

function most_common_first(array $nicknames): array
{
    $counts = array_count_values($nicknames);
    uksort($counts, fn($a, $b) => $counts[$b] <=> $counts[$a] ?: strcmp($a, $b));
    return array_keys($counts);
}

function compute_result(array $players, array $ranks, array $mawashi_list, int $limit): array
{
    $rank_set = array_flip($ranks);
    $result   = [];

    foreach ($players as $profile_id => $info) {
        $count          = 0;
        $rank_nicknames = [];
        $units          = [];

        $latest_event_id = 0;
        foreach ($rank_set as $r => $_) {
            $count          += $info['counts'][$r] ?? 0;
            $latest_event_id = max($latest_event_id, $info['latest_event_id_by_rank'][$r] ?? 0);
            array_push($rank_nicknames, ...($info['nicknames_by_rank'][$r] ?? []));
            foreach ($info['units_by_rank'][$r] ?? [] as $unit) {
                if ($unit !== 'none') $units[] = $unit;
            }
        }

        if ($count === 0) continue;

        $scoped        = most_common_first($rank_nicknames ?: ($info['all_nicknames'] ?? []));
        $all           = most_common_first($info['all_nicknames'] ?? []);
        $best_nickname = $scoped[0] ?? '';

        $result[] = [
            'nickname'        => $best_nickname,
            'other_nicknames' => array_values(array_filter($all, fn($n) => $n !== $best_nickname)),
            'count'           => $count,
            'flags'           => $mawashi_list[$profile_id] ?? [],
            'units'           => $units,
            '_latest_event_id' => $latest_event_id,
        ];
    }

    // Equal count → earlier achiever ranks higher (lower latest_event_id wins)
    usort($result, fn($a, $b) => $b['count'] <=> $a['count'] ?: $a['_latest_event_id'] <=> $b['_latest_event_id']);

    $result = array_slice($result, 0, $limit);

    foreach ($result as $idx => &$row) {
        $row['rank'] = $idx + 1;
        unset($row['_latest_event_id']);
    }

    return $result;
}
