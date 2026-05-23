<?php

function handle_mysekai_ikea()
{
    return @json_decode(file_get_contents(MYSEKAI_DIR . "/ikea.json"), true);
}