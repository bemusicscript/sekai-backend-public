<?php

require_once("helper.php");
$mawashi_list = require("mawashi.php");

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
    return compute_statistics();
    // return cached('statistics:banners', 3600, fn() => compute_statistics());
}

function get_unit_name($character_id): string
{
    if($character_id >= 1 && $character_id <= 4){
        return "light_sound";
    }
    if($character_id >= 5 && $character_id <= 8){
        return "idol";
    }
    if($character_id >= 9 && $character_id <= 12){
        return "street";
    }
    if($character_id >= 13 && $character_id <= 16){
        return "theme_park";
    }
    if($character_id >= 17 && $character_id <= 20){
        return "school_refusal";
    }
    return "piapro";
}

function compute_statistics(): array
{
    global $mysqli, $mawashi_list;
    // http_response_code(404);

    $events_json = file_get_contents("http://" . API_DOMAIN . "/json/events.json");
    $events_cards_json = file_get_contents("http://" . API_DOMAIN . "/json/eventCards.json");
    $events_bonus_json = file_get_contents("http://" . API_DOMAIN . "/json/eventDeckBonuses.json");

    $cards = [];
    $event_units = [];
    $event_characters = [];
    $event_attrs = [];

    foreach (streamJsonArrayFromUrl("http://" . API_DOMAIN . "/json/cards.json") as $card) {
        $cards[$card['id']] = [
            "attr" => $card['attr'],
            "characterId" => $card['characterId'],
            "assetbundleName" => $card['assetbundleName'],
            "unit" => isset($card['unit']) ? $card['unit'] : null,
        ];
    }

    // Grab Units of each event cards
    $event_cards_unit = [];
    $event_cards_character = [];
    foreach (json_decode($events_cards_json, true) ?? [] as $event_cards) {
        if(!isset($event_cards_unit[$event_cards['eventId']])){
            $event_cards_unit[$event_cards['eventId']] = [];
        }
        if($event_cards['isDisplayCardStory']){
            $card_info = $cards[(int)$event_cards['cardId']];
            $card_attr = $card_info['attr'];
            $card_character_id = $card_info['characterId'];
            $card_unit = get_unit_name($card_character_id);
            if($card_unit == "piapro") continue;
            if(!isset($event_cards_character[$event_cards['eventId']])){
                $event_cards_character[$event_cards['eventId']] = $card_character_id;
            }
            $event_cards_unit[$event_cards['eventId']][] = $card_unit;
        }
    }

    // Grab Attributes
    $event_bonus_list = [];
    foreach (json_decode($events_bonus_json, true) ?? [] as $event_bonus){
        if(!isset($event_bonus_list[$event_bonus['eventId']])){
            $event_bonus_list[$event_bonus['eventId']] = [];
        }
        if(isset($event_bonus['cardAttr'])){
            $event_bonus_list[$event_bonus['eventId']][] = $event_bonus['cardAttr'];
        }
    }

    foreach ($event_cards_unit as $eid => $units) {
        // (Jiiku's idea) to guess mixed
        $unit_count = count(array_unique($units));
        if($unit_count > 1){
            $event_units[$eid] = null;
        }else{
            $unit_unique= array_unique($units);
            if (count($unit_unique) > 0) {
                $event_units[$eid] = $unit_unique[0];
            } else {
                $event_units[$eid] = null;
            }
        }
        // Pick the most common one as attributes
        $most_common_attribute = most_common_first($event_bonus_list[$eid]);
        if(count($most_common_attribute) > 0){
            $event_attrs[$eid] = $most_common_attribute[0];
        } else {
            $event_attrs[$eid] = null;
        }
    }

    // grab attributes
    $event_final = [];
    $chara_count = [];
    foreach (json_decode($events_json, true) ?? [] as $event) {
        $event_id = $event['id'] ?? null;
        if ($event_id === null) continue;

        $event_unit = $event_units[$event_id] ?? null;
        $event_type = $event['eventType'] ?? null;
        $event_attr = $event_attrs[$event_id] ?? null;
        $event_character = $event_cards_character[$event_id] ?? null;

        // skip bloom, mixed, etc.
        $chara_event_count = null;

        if ($event_character !== null) {
            $chara_count[$event_character] ??= 0;

            if ($event_unit && $event_attr) {
                $chara_count[$event_character]++;
            }

            $chara_event_count = $chara_count[$event_character];
        }

        $event_final[$event_id] = [
            "unit" => $event_unit,
            "type" => $event_type,
            "attr" => $event_attr,
            "chara" => $event_character,
            "chara_count" => $chara_event_count,
        ];
    }

    return $event_final;
}

function most_common_first(array $nicknames): array
{
    $counts = array_count_values($nicknames);
    uksort($counts, fn($a, $b) => $counts[$b] <=> $counts[$a] ?: strcmp($a, $b));
    return array_keys($counts);
}
