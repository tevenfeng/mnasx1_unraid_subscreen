<?php
/* Small action endpoint: start/stop/restart/once/reset from the settings page. */

require_once __DIR__ . '/lib.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    subsys_redirect('error=' . urlencode('POST required'));
}

$allowed = [
    'start' => 'start.sh',
    'stop' => 'stop.sh',
    'restart' => 'restart.sh',
    'once' => 'once.sh',
    'reset' => 'reset-usb.sh',
];
$action = (string)($_POST['action'] ?? '');
if (!isset($allowed[$action])) {
    subsys_redirect('error=' . urlencode('unknown action'));
}

[$code, $output] = subsys_run($allowed[$action]);
subsys_redirect(($code === 0 ? 'note=ok' : 'note=failed')
    . '&output=' . urlencode(substr($output, -400)));
