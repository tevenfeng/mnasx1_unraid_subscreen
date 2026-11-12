<?php
/* Download a diagnostics bundle: settings, versions, GPU/disk probes, log tail. */

require_once __DIR__ . '/lib.php';

header('Content-Type: text/plain; charset=utf-8');
header('Content-Disposition: attachment; filename="mnasx1-subscreen-diag.txt"');

echo "mnasx1-subscreen diagnostics\n";
echo "generated: " . date('c') . "\n";
echo "unraid: " . trim((string)@file_get_contents('/etc/unraid-version')) . "\n\n";

echo "== status ==\n";
[$code, $output] = subsys_run('status.sh');
echo $output . "\n\n";

echo "== config (" . SUBSYS_CONFIG . ") ==\n";
echo is_readable(SUBSYS_CONFIG)
    ? (string)file_get_contents(SUBSYS_CONFIG) : "(defaults, no file)\n";
echo "\n";

$python = subsys_python();
if ($python === '') {
    echo "== python ==\nMISSING: install NerdTools or a Python 3 plugin\n";
} else {
    echo "== version ==\n";
    $cmd = escapeshellcmd($python) . ' '
        . escapeshellarg(SUBSYS_APP_DIR . '/subscreen_daemon.py') . ' version 2>&1';
    echo (string)shell_exec($cmd) . "\n";
    echo "== gpus ==\n";
    $cmd = escapeshellcmd($python) . ' '
        . escapeshellarg(SUBSYS_APP_DIR . '/subscreen_daemon.py') . ' gpus 2>&1';
    echo (string)shell_exec($cmd) . "\n";
    echo "== temps ==\n";
    $cmd = escapeshellcmd($python) . ' '
        . escapeshellarg(SUBSYS_APP_DIR . '/subscreen_daemon.py') . ' temps 2>&1';
    echo (string)shell_exec($cmd) . "\n";
}

echo "== log (last 200 lines) ==\n";
$log = '/var/log/mnasx1-subscreen.log';
if (is_readable($log)) {
    $lines = file($log, FILE_IGNORE_NEW_LINES) ?: [];
    echo implode("\n", array_slice($lines, -200)) . "\n";
} else {
    echo "(no log yet)\n";
}
