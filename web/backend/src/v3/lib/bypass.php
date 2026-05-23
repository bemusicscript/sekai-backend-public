<?php

$user_data = file(DATA_DIR . "profile.txt", FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
$hide_nickname_list = [];

foreach ($user_data as $line) {
    $parts = explode("||", $line, 2);
    $id = trim($parts[0]);

    if (ctype_digit($id)) {
        $hide_nickname_list[] = $id;
    }
}

return [
    "hide_nickname" => array_values(array_unique($hide_nickname_list))
];