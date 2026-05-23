<?php

function fetch_mawashi(): array
{
    $result = [];
    $path = MAWASHI_DIR . 'list.txt';

    $mawashi_list = file_get_contents($path);
    if ($mawashi_list === false) {
        return $result;
    }

    $lines = explode("\n", $mawashi_list);

    foreach ($lines as $line) {
        $line = trim($line);
        if ($line === '') {
            continue;
        }

        $parts = explode('|', $line, 2);
        if (count($parts) < 2) {
            continue;
        }

        $mawashi_uid = trim($parts[0]);
        if ($mawashi_uid === '') {
            continue;
        }

        $types_raw = trim($parts[1]);
        $mawashi_type = ($types_raw === '') ? [$types_raw] : explode(',', $types_raw);

        if (!isset($result[$mawashi_uid])) {
            $result[$mawashi_uid] = $mawashi_type;
        }
    }

    return $result;
}

return fetch_mawashi();
