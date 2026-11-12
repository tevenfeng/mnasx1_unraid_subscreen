<?php
/* Save handler for the settings page.
 *
 * Every value is validated here rather than trusted from the browser: the
 * daemon clamps too, but a bad value must never reach the flash drive where it
 * would survive reboots.
 */

require_once __DIR__ . '/lib.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    subsys_redirect('error=' . urlencode('POST required'));
}

function post(string $key, string $default = ''): string
{
    $value = $_POST[$key] ?? $default;
    if (is_array($value)) {
        $value = implode(',', array_map('strval', $value));
    }
    return trim((string)$value);
}

function clamp_num(string $value, float $min, float $max, string $fallback): string
{
    if (!is_numeric($value)) {
        return $fallback;
    }
    $number = (float)$value;
    if ($number < $min) {
        $number = $min;
    }
    if ($number > $max) {
        $number = $max;
    }
    return rtrim(rtrim(sprintf('%.2f', $number), '0'), '.');
}

function clamp_int(string $value, int $min, int $max, string $fallback): string
{
    if (!is_numeric($value)) {
        return $fallback;
    }
    return (string)max($min, min($max, (int)$value));
}

function pick(string $value, array $allowed, string $fallback): string
{
    return in_array($value, $allowed, true) ? $value : $fallback;
}

function on_off(string $key, string $default = '0'): string
{
    return post($key, $default) === '1' ? '1' : '0';
}

$defaults = subsys_defaults();
$errors = [];

// --- refresh cadence -------------------------------------------------------
$values['pace'] = clamp_num(post('pace'), 0.5, 5.0, $defaults['pace']);
$values['every_fast'] = clamp_int(post('every_fast'), 1, 60, $defaults['every_fast']);
$values['every_medium'] = clamp_int(post('every_medium'), 1, 60, $defaults['every_medium']);
$values['every_slow'] = clamp_int(post('every_slow'), 1, 60, $defaults['every_slow']);
$values['every_static'] = clamp_int(post('every_static'), 1, 60, $defaults['every_static']);

// --- layout ---------------------------------------------------------------
$values['layout'] = pick(post('layout'), ['per-page', 'paired'], $defaults['layout']);
$syscount = post('syscount', 'auto');
$values['syscount'] = pick($syscount, ['auto', '3', '4'], 'auto');
$values['system_mode'] = pick(post('system_mode'),
    ['both', 'vendor', 'legacy'], $defaults['system_mode']);
$values['wlan_slots'] = clamp_int(post('wlan_slots'), 1, 3, $defaults['wlan_slots']);
$values['wlan_total_unit'] = pick(post('wlan_total_unit'),
    ['auto', 'kb', 'mb', 'gb', 'tb'], $defaults['wlan_total_unit']);

// Card order: four slots, each card exactly once, otherwise keep the default.
$order = [];
foreach (['slot1', 'slot2', 'slot3', 'slot4'] as $slot) {
    $card = post($slot);
    if (in_array($card, ['cpu', 'igpu', 'mem', 'dgpu'], true)
        && !in_array($card, $order, true)) {
        $order[] = $card;
    }
}
$values['card_order'] = count($order) === 4
    ? implode(',', $order) : $defaults['card_order'];

// --- data sources --------------------------------------------------------
$values['disks'] = implode(',', array_slice(array_filter(
    array_map('trim', explode(',', post('disks'))), 'strlen'), 0, 8));
$values['ifaces'] = implode(',', array_slice(array_filter(
    array_map('trim', explode(',', post('ifaces'))), 'strlen'), 0, 3));

$values['gpu'] = on_off('gpu', '1');
$values['gpu_igpu'] = on_off('gpu_igpu', '1');
$values['gpu_dgpu'] = on_off('gpu_dgpu', '1');
$values['nvidia_smi'] = on_off('nvidia_smi', '1');
$values['temp_hwmon'] = on_off('temp_hwmon', '1');
$values['temp_smartctl'] = on_off('temp_smartctl', '1');
$values['temp_disksini'] = on_off('temp_disksini', '1');
$values['wake_disks'] = on_off('wake_disks', '0');
$values['allow_hibernate'] = on_off('allow_hibernate', '0');
$values['log_verbose'] = on_off('log_verbose', '0');

// One source must remain, otherwise every disk silently shows "--".
if ($values['temp_hwmon'] === '0' && $values['temp_smartctl'] === '0'
    && $values['temp_disksini'] === '0') {
    $values['temp_hwmon'] = '1';
    $errors[] = '至少保留一个温度来源，已自动启用 hwmon / kept hwmon enabled';
}

if (!subsys_write_config($values)) {
    subsys_redirect('error=' . urlencode('无法写入配置文件 / cannot write config file'));
}

[$code, $output] = subsys_run('restart.sh');
$note = $code === 0 ? 'saved' : 'saved-nostart';
subsys_redirect('note=' . $note . '&output=' . urlencode(substr($output, -400)));
