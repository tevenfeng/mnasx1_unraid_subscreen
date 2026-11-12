<?php
/* POST handling for the settings page.
 *
 * Deliberately not a separate endpoint: the form posts back to the page and
 * the result is rendered inline. A redirect through /plugins/.../save.php
 * looked like a dead end in the browser whenever anything was printed before
 * the Location header, and it also left a second URL that had to be secured
 * and kept in sync.
 */

require_once __DIR__ . '/lib.php';

function subsys_post(string $key, string $default = ''): string
{
    $value = $_POST[$key] ?? $default;
    if (is_array($value)) {
        $value = implode(',', array_map('strval', $value));
    }
    return trim((string)$value);
}

function subsys_clamp_num(string $value, float $min, float $max, string $fallback): string
{
    if (!is_numeric($value)) {
        return $fallback;
    }
    $number = max($min, min($max, (float)$value));
    return rtrim(rtrim(sprintf('%.2f', $number), '0'), '.');
}

function subsys_clamp_int(string $value, int $min, int $max, string $fallback): string
{
    if (!is_numeric($value)) {
        return $fallback;
    }
    return (string)max($min, min($max, (int)$value));
}

function subsys_pick(string $value, array $allowed, string $fallback): string
{
    return in_array($value, $allowed, true) ? $value : $fallback;
}

function subsys_on_off(string $key, string $default = '0'): string
{
    return subsys_post($key, $default) === '1' ? '1' : '0';
}

/* Returns [level, message]; level is 'ok' or 'error'. */
function subsys_handle_post(): array
{
    $action = subsys_post('do');

    $scripts = [
        'start' => 'start.sh',
        'stop' => 'stop.sh',
        'restart' => 'restart.sh',
        'once' => 'once.sh',
        'reset' => 'reset-usb.sh',
    ];
    if (isset($scripts[$action])) {
        [$code, $output] = subsys_run($scripts[$action]);
        $text = trim($output);
        if (strlen($text) > 600) {
            $text = substr($text, -600);
        }
        return [$code === 0 ? 'ok' : 'error', $text !== '' ? $text : 'done'];
    }

    if ($action !== 'save') {
        return ['error', '未知操作 / unknown action'];
    }

    $defaults = subsys_defaults();
    $values['pace'] = subsys_clamp_num(subsys_post('pace'), 0.5, 5.0, $defaults['pace']);
    $values['every_fast'] = subsys_clamp_int(subsys_post('every_fast'), 1, 60, $defaults['every_fast']);
    $values['every_medium'] = subsys_clamp_int(subsys_post('every_medium'), 1, 60, $defaults['every_medium']);
    $values['every_slow'] = subsys_clamp_int(subsys_post('every_slow'), 1, 60, $defaults['every_slow']);
    $values['every_static'] = subsys_clamp_int(subsys_post('every_static'), 1, 60, $defaults['every_static']);

    $values['layout'] = subsys_pick(subsys_post('layout'), ['per-page', 'paired'], $defaults['layout']);
    $values['syscount'] = subsys_pick(subsys_post('syscount', 'auto'), ['auto', '3', '4'], 'auto');
    $values['system_mode'] = subsys_pick(subsys_post('system_mode'),
        ['both', 'vendor', 'legacy'], $defaults['system_mode']);
    $values['wlan_slots'] = subsys_clamp_int(subsys_post('wlan_slots'), 1, 3, $defaults['wlan_slots']);
    $values['wlan_total_unit'] = subsys_pick(subsys_post('wlan_total_unit'),
        ['auto', 'kb', 'mb', 'gb', 'tb'], $defaults['wlan_total_unit']);

    $order = [];
    foreach (['slot1', 'slot2', 'slot3', 'slot4'] as $slot) {
        $card = subsys_post($slot);
        if (in_array($card, ['cpu', 'igpu', 'mem', 'dgpu'], true)
            && !in_array($card, $order, true)) {
            $order[] = $card;
        }
    }
    $values['card_order'] = count($order) === 4
        ? implode(',', $order) : $defaults['card_order'];

    $values['disks'] = implode(',', array_slice(array_filter(
        array_map('trim', explode(',', subsys_post('disks'))), 'strlen'), 0, 8));
    $values['ifaces'] = implode(',', array_slice(array_filter(
        array_map('trim', explode(',', subsys_post('ifaces'))), 'strlen'), 0, 3));

    foreach (['gpu', 'gpu_igpu', 'gpu_dgpu', 'nvidia_smi',
              'temp_hwmon', 'temp_smartctl', 'temp_disksini',
              'wake_disks', 'allow_hibernate', 'log_verbose'] as $key) {
        $values[$key] = subsys_on_off($key, $defaults[$key]);
    }

    $notes = [];
    if ($values['temp_hwmon'] === '0' && $values['temp_smartctl'] === '0'
        && $values['temp_disksini'] === '0') {
        $values['temp_hwmon'] = '1';
        $notes[] = '至少保留一个温度来源，已自动启用 hwmon / kept hwmon enabled';
    }

    if (!subsys_write_config($values)) {
        return ['error', '无法写入配置文件 / cannot write ' . SUBSYS_CONFIG];
    }

    [$code, $output] = subsys_run('restart.sh');
    $message = '设置已保存 / Settings saved.';
    if ($notes) {
        $message .= ' ' . implode(' ', $notes);
    }
    if ($code !== 0) {
        return ['error', $message . ' 但守护进程未能启动 / daemon did not start: ' . trim($output)];
    }
    return ['ok', $message];
}
