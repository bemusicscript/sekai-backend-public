<?php

define("API_VERSION", "v3.5-20260410");
define("API_DOMAIN", "api.internal:5000");

define("JIIKU_DOMAIN_DEV", "jiiku-dev.internal:3000");
define("JIIKU_DOMAIN_PROD", "jiiku-prod.internal:3000");
define("JIIKU_DEFAULT_BRANCH", "refs/heads/main");

define("JIIKU_KEY", "fixme");
define("JIIKU_SECRET", "fixme"); //webhook secret

define("HASH_SALT", "fixme");
define("HASH_KEY",  "fixme");

define("MARIADB_HOST", "mariadb.internal");
define("MARIADB_USER", "sekai");
define("MARIADB_PASS", "sekai");
define("MARIADB_NAME", "sekai");

define("REDIS_HOST", "valkey.internal");
define("REDIS_PASS", "fixme");

define("DATA_DIR", "/var/www/data/");
define("MAWASHI_DIR", "/var/www/mawashi/");
define("MYSEKAI_DIR", "/var/www/asset/mysekai/");
define("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fixme");
define("PROXY_DOMAIN", "proxy.internal:3128");

define("ANALYTICS_URL", "http://gateway.internal:6767/analytics/");

$allowed_origins = [
    'https://sekai.run',
    'https://api.sekai.run',
    'https://rewrite.sekai.run'
];
$is_debug = false;
