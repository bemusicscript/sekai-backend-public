<?php

function truncate_string($str)
{
    $out = mb_strlen($str) > 10 ? mb_substr($str, 0, 9) . "…" : $str;
    return $out;
}

function send_discord_webhook($message): bool
{
    $webhookUrl = DISCORD_WEBHOOK_URL;

    $payload = [
        'content' => $message,
    ];
    $jsonPayload = json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

    $ch = curl_init($webhookUrl);
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, $jsonPayload);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_HTTPHEADER, [
        'Content-Type: application/json',
        'Content-Length: ' . strlen($jsonPayload)
    ]);
    curl_setopt($ch, CURLOPT_PROXY, PROXY_DOMAIN);
    curl_setopt($ch, CURLOPT_PROXYTYPE, CURLPROXY_HTTP);
    $response = curl_exec($ch);

    if (curl_errno($ch)) {
        error_log('cURL error: ' . curl_error($ch));
        unset($ch);
        return false;
    }
    unset($ch);
    return true;
}

function streamJsonArrayFromUrl(string $url): Generator
{
    $context = stream_context_create([
        'http' => [
            'method' => 'GET',
            'timeout' => 60,
            'ignore_errors' => true,
        ],
    ]);

    $fh = fopen($url, 'rb', false, $context);
    if ($fh === false) {
        throw new RuntimeException("Failed to open URL: $url");
    }

    try {
        $buffer = '';
        $depth = 0;
        $inString = false;
        $escape = false;
        $startedObject = false;
        $foundArrayStart = false;

        while (!feof($fh)) {
            $chunk = fread($fh, 8192);
            if ($chunk === false) {
                throw new RuntimeException("Failed to read stream");
            }

            $len = strlen($chunk);
            for ($i = 0; $i < $len; $i++) {
                $ch = $chunk[$i];

                if (!$foundArrayStart) {
                    if (ctype_space($ch)) {
                        continue;
                    }
                    if ($ch === '[') {
                        $foundArrayStart = true;
                        continue;
                    }
                    throw new RuntimeException("Expected JSON array");
                }

                if (!$startedObject) {
                    if (ctype_space($ch) || $ch === ',') {
                        continue;
                    }

                    if ($ch === ']') {
                        return;
                    }

                    if ($ch === '{') {
                        $startedObject = true;
                        $depth = 1;
                        $buffer = '{';
                        continue;
                    }

                    throw new RuntimeException("Unexpected character in JSON array: $ch");
                }

                $buffer .= $ch;

                if ($inString) {
                    if ($escape) {
                        $escape = false;
                    } elseif ($ch === '\\') {
                        $escape = true;
                    } elseif ($ch === '"') {
                        $inString = false;
                    }
                    continue;
                }

                if ($ch === '"') {
                    $inString = true;
                    continue;
                }

                if ($ch === '{') {
                    $depth++;
                } elseif ($ch === '}') {
                    $depth--;

                    if ($depth === 0) {
                        yield json_decode($buffer, true, 512, JSON_THROW_ON_ERROR);

                        $buffer = '';
                        $startedObject = false;
                    }
                }
            }
        }

        if ($startedObject) {
            throw new RuntimeException('Incomplete JSON object at end of stream');
        }
    } finally {
        fclose($fh);
    }
}