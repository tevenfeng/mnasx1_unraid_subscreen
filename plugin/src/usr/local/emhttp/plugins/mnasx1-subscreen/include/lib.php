<?php
/* Shared helpers for the mnasx1-subscreen settings page.
 *
 * Config lives on the flash drive so it survives reboots; the daemon reads the
 * same file with its own tolerant parser, and command-line options still win.
 */

const SUBSYS_PLUGIN = 'mnasx1-subscreen';
const SUBSYS_CONFIG = '/boot/config/plugins/mnasx1-subscreen/mnasx1-subscreen.cfg';
const SUBSYS_CONFIG_DIR = '/boot/config/plugins/mnasx1-subscreen';
const SUBSYS_APP_DIR = '/usr/local/lib/mnasx1-subscreen';
const SUBSYS_SCRIPT_DIR = '/usr/local/emhttp/plugins/mnasx1-subscreen/scripts';

function subsys_defaults(): array
{
    return [
        'pace' => '1.5',
        'every_fast' => '1',
        'every_medium' => '2',
        'every_slow' => '4',
        'every_static' => '12',
        'layout' => 'per-page',
        'syscount' => 'auto',
        'card_order' => 'cpu,igpu,mem,dgpu',
        'wlan_slots' => '3',
        'wlan_total_unit' => 'auto',
        'system_mode' => 'both',
        'disks' => '',
        'ifaces' => '',
        'gpu' => '1',
        'gpu_igpu' => '1',
        'gpu_dgpu' => '1',
        'nvidia_smi' => '1',
        'temp_hwmon' => '1',
        'temp_smartctl' => '1',
        'temp_disksini' => '1',
        'wake_disks' => '0',
        'allow_hibernate' => '0',
        'log_verbose' => '0',
    ];
}

function subsys_read_config(): array
{
    $cfg = subsys_defaults();
    if (!is_readable(SUBSYS_CONFIG)) {
        return $cfg;
    }
    foreach (file(SUBSYS_CONFIG, FILE_IGNORE_NEW_LINES) ?: [] as $line) {
        $line = trim($line);
        if ($line === '' || $line[0] === ';' || $line[0] === '#' || $line[0] === '[') {
            continue;
        }
        $pos = strpos($line, '=');
        if ($pos === false) {
            continue;
        }
        $key = strtolower(trim(substr($line, 0, $pos)));
        $value = trim(substr($line, $pos + 1), " \t\"'");
        if (array_key_exists($key, $cfg)) {
            $cfg[$key] = $value;
        }
    }
    return $cfg;
}

/* Atomic write: a power cut must never leave a half-written config behind.
 * The temp file is written to the same directory so rename() stays on one
 * filesystem. */
function subsys_write_config(array $values): bool
{
    if (!is_dir(SUBSYS_CONFIG_DIR) && !@mkdir(SUBSYS_CONFIG_DIR, 0777, true)) {
        return false;
    }
    $lines = ["; mnasx1-subscreen settings - written by the WebGUI", ''];
    foreach ($values as $key => $value) {
        $lines[] = $key . '=' . $value;
    }
    $tmp = SUBSYS_CONFIG_DIR . '/.mnasx1-subscreen.cfg.tmp';
    if (@file_put_contents($tmp, implode("\n", $lines) . "\n") === false) {
        return false;
    }
    @chmod($tmp, 0666);
    return @rename($tmp, SUBSYS_CONFIG);
}

/* Run a plugin script and capture its output. Escapes every argument. */
function subsys_run(string $script, array $args = []): array
{
    $path = SUBSYS_SCRIPT_DIR . '/' . $script;
    if (!is_file($path)) {
        return [1, "missing script: $path"];
    }
    $cmd = escapeshellcmd($path);
    foreach ($args as $arg) {
        $cmd .= ' ' . escapeshellarg((string)$arg);
    }
    $output = [];
    $code = 1;
    exec($cmd . ' 2>&1', $output, $code);
    return [$code, implode("\n", $output)];
}

function subsys_python(): string
{
    foreach ([getenv('SUBSYS_PYTHON') ?: '', '/usr/bin/python3',
              '/usr/local/bin/python3', '/opt/bin/python3'] as $candidate) {
        if ($candidate !== '' && is_executable($candidate)) {
            return $candidate;
        }
    }
    $found = trim((string)shell_exec('command -v python3 2>/dev/null'));
    return $found;
}

function subsys_daemon_running(): bool
{
    $pidfile = '/var/run/mnasx1-subscreen.pid';
    if (!is_readable($pidfile)) {
        return false;
    }
    $pid = (int)trim((string)file_get_contents($pidfile));
    return $pid > 0 && file_exists("/proc/$pid");
}

/* Mount points the daemon would report by default: real block devices under
 * /mnt, minus the union/remote/plugin paths. Mirrors _default_disk_paths(). */
function subsys_discover_disks(): array
{
    $skip = ['/mnt/user', '/mnt/user0', '/mnt/remotes', '/mnt/addons',
             '/mnt/rootshare', '/boot'];
    $found = [];
    foreach (file('/proc/mounts', FILE_IGNORE_NEW_LINES) ?: [] as $line) {
        $parts = preg_split('/\s+/', $line);
        if (count($parts) < 2 || strpos($parts[0], '/dev/') !== 0) {
            continue;
        }
        $mount = str_replace('\\040', ' ', $parts[1]);
        if (strpos($mount, '/mnt/') !== 0 && $mount !== '/boot') {
            continue;
        }
        foreach ($skip as $bad) {
            if ($mount === $bad || strpos($mount, $bad . '/') === 0) {
                continue 2;
            }
        }
        $found[] = $mount;
    }
    $priority = function (string $path): array {
        if (preg_match('#^/mnt/disk(\d+)$#', $path, $m)) {
            return [0, (int)$m[1]];
        }
        if ($path === '/mnt/cache') {
            return [1, 0];
        }
        if (strpos($path, '/mnt/disks/') === 0) {
            return [3, $path];
        }
        return [2, $path];
    };
    usort($found, function ($a, $b) use ($priority) {
        return $priority($a) <=> $priority($b);
    });
    return array_values(array_unique($found));
}

/* Interfaces that currently hold an IPv4 address, most useful first. */
function subsys_discover_ifaces(): array
{
    $raw = (string)shell_exec('ip -4 -o addr show 2>/dev/null');
    $names = [];
    foreach (explode("\n", trim($raw)) as $line) {
        if (preg_match('/^\d+:\s+([^\s@]+)/', $line, $m)) {
            $names[$m[1]] = true;
        }
    }
    $names = array_keys($names);
    $skip = '/^(lo|docker.*|veth.*|virbr.*|br-.*|tap.*|tun.*|tailscale.*|shim-.*|dummy.*|ifb.*)$/';
    $names = array_values(array_filter($names, function ($name) use ($skip) {
        return !preg_match($skip, $name);
    }));
    $rank = function (string $name): array {
        if ($name === 'br0') {
            return [0, $name];
        }
        if ($name === 'bond0') {
            return [1, $name];
        }
        if (preg_match('/^(eth|en|wl)/', $name)) {
            return [2, $name];
        }
        return [3, $name];
    };
    usort($names, function ($a, $b) use ($rank) {
        return $rank($a) <=> $rank($b);
    });
    return $names;
}

function subsys_h(string $text): string
{
    return htmlspecialchars($text, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function subsys_redirect(string $query): void
{
    header('Location: /Settings/mnasx1-subscreen?' . $query);
    exit;
}
