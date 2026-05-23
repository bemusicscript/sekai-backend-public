<?php

function handle_refresh_info()
{
    $result = null;
    $value = @json_decode(file_get_contents("http://" . API_DOMAIN . "/json/eventBreakTimes.json", false), true);
    if (!$value) return [];

    // get highest id
    $value_ids = array_column($value, "id");
    $max_id = max($value_ids);
    $max_indices = array_keys($value_ids, $max_id);
    $target_id = $max_indices[0];
    $target_info = $value[$target_id];

    if (!$target_info) return [];

    $result = [
        "calc_info" => [
            "init" =>  $target_info["initialPoint"],
            "maximum" => $target_info["maxPoint"],
            "basePoint" => $target_info["pointsPerMusicSecond"],
            "offsetPoint" => $target_info["musicOffsetSeconds"],
        ],
        "decay_info" => [
            "minimum" => $target_info['decreaseMinutes'],
            "interval" => $target_info['requiredIntervalMinutes'],
            "deducted" => ($target_info['decreasePoint'] / $target_info['maxPoint']) * 100,
        ],
    ];
    return $result;
}
