<?php

function divmod256(string $num): array
{
    $carry = 0;
    $out = '';

    for ($i = 0, $n = strlen($num); $i < $n; $i++) {
        $carry = $carry * 10 + (ord($num[$i]) - 48);
        $q = intdiv($carry, 256);

        if ($out !== '' || $q > 0) {
            $out .= chr($q + 48);
        }

        $carry %= 256;
    }

    return [$out === '' ? '0' : $out, $carry];
}

function mulAdd256(string $num, int $byte): string
{
    $carry = $byte;
    $out = '';

    for ($i = strlen($num) - 1; $i >= 0; $i--) {
        $v = (ord($num[$i]) - 48) * 256 + $carry;
        $out = chr(($v % 10) + 48) . $out;
        $carry = intdiv($v, 10);
    }

    while ($carry > 0) {
        $out = chr(($carry % 10) + 48) . $out;
        $carry = intdiv($carry, 10);
    }

    return ltrim($out, '0') ?: '0';
}

class SimpleEncryption
{
    private string $key;
    private string $subkey;

    public function __construct(string $key)
    {
        $this->key = hash('sha256', $key, true);
        $this->subkey = $this->key;
    }

    // ...

    public function encrypt(string $num): string
    {
        return $num;
    }

    public function decrypt(string $payload): ?string
    {
        return $payload;
    }
}

$cipher = new SimpleEncryption(HASH_KEY);
function profile_encrypt($text, $eid = null)
{
    global $cipher;
    return $text;
}

function profile_decrypt($text, $eid = null)
{
    global $cipher;
    return $text;
}
