#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static checks on the plugin packaging.

These do not need Unraid: they catch the structural mistakes that are
expensive to discover on the real box (a page that renders inline instead of
as its own screen, forms that post to a helper script, a .plg whose payload
name cannot be replaced on upgrade).
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_SRC = os.path.join(ROOT, "plugin", "src", "usr", "local", "emhttp",
                          "plugins", "mnasx1-subscreen")
PAGE = os.path.join(PLUGIN_SRC, "mnasx1-subscreen.page")


def page_header():
    with open(PAGE, encoding="utf-8") as handle:
        text = handle.read()
    header = text.split("\n---\n", 1)[0]
    values = {}
    for line in header.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    return values, text


class PageStructureTest(unittest.TestCase):
    def test_page_is_a_standalone_screen_not_inline_content(self):
        header, _ = page_header()
        # A page under Settings that carries content but no Type is rendered
        # inline at /Settings; Type="xmenu" makes it its own URL.
        self.assertEqual(header.get("Type"), "xmenu")
        self.assertIn(header.get("Menu"), ("OtherSettings", "Utilities",
                                           "UserPreferences", "NetworkServices"))

    def test_page_has_a_title_and_icon(self):
        header, _ = page_header()
        self.assertTrue(header.get("Title"))
        self.assertTrue(header.get("Icon"))

    def test_header_is_utf8_without_bom_and_uses_lf(self):
        raw = open(PAGE, "rb").read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"),
                         "a BOM breaks parse_ini_string on the first key")
        self.assertNotIn(b"\r\n", raw, "CRLF breaks the INI header")

    def test_forms_post_back_to_the_page(self):
        _, text = page_header()
        self.assertNotIn('action="/plugins/', text,
                         "a helper endpoint leaves a blank screen on error")
        # five action buttons rendered from one loop + the save form
        self.assertEqual(text.count('name="do"'), 2)
        self.assertIn('name="do" value="<?=$cmd?>"', text)
        self.assertIn('name="do" value="save"', text)

    def test_page_does_not_reference_removed_endpoints(self):
        _, text = page_header()
        for gone in ("include/save.php", "include/action.php"):
            self.assertNotIn(gone, text)


class HandlerTest(unittest.TestCase):
    def test_handle_php_defines_every_action_the_page_posts(self):
        with open(os.path.join(PLUGIN_SRC, "include", "handle.php"),
                  encoding="utf-8") as handle:
            source = handle.read()
        for action in ("start", "stop", "restart", "once", "reset"):
            self.assertIn("'%s' =>" % action, source)
        self.assertIn("$action !== 'save'", source)

    def test_every_script_the_handler_runs_exists_and_is_executable(self):
        directory = os.path.join(PLUGIN_SRC, "scripts")
        with open(os.path.join(PLUGIN_SRC, "include", "handle.php"),
                  encoding="utf-8") as handle:
            source = handle.read()
        for script in re.findall(r"=> '([a-z-]+\.sh)'", source):
            path = os.path.join(directory, script)
            self.assertTrue(os.path.isfile(path), script)
            if os.name != "nt":
                self.assertTrue(os.access(path, os.X_OK), script + " not 0755")

    def test_event_hooks_are_executable(self):
        for name in ("started", "stopping"):
            path = os.path.join(PLUGIN_SRC, "event", name)
            self.assertTrue(os.path.isfile(path))
            if os.name != "nt":
                self.assertTrue(os.access(path, os.X_OK), name + " not 0755")


class ConfigKeyCoverageTest(unittest.TestCase):
    """A key added to subsys_defaults() must reach all three places.

    lib.php holds the defaults, handle.php validates them on save, default.cfg
    seeds fresh installs and the page renders the controls. Adding a key to
    only some of those is exactly the kind of drift that silently strands a
    setting (the daemon reads a key the page can never change, or restarts
    without one the config file lacks), so the three lists are tied together.
    """

    def _default_keys(self):
        with open(os.path.join(PLUGIN_SRC, "include", "lib.php"),
                  encoding="utf-8") as handle:
            body = handle.read()
        block = re.search(r"function subsys_defaults\(\): array\s*\{(.*?)\n    \]",
                          body, re.S)
        self.assertIsNotNone(block, "subsys_defaults() shape changed")
        keys = re.findall(r"'([a-z_]+)'\s*=>", block.group(1))
        self.assertGreater(len(keys), 10, "regex matched nothing useful")
        return keys

    def test_every_default_key_is_written_by_the_save_path(self):
        with open(os.path.join(PLUGIN_SRC, "include", "handle.php"),
                  encoding="utf-8") as handle:
            source = handle.read()
        # Look only at the save branch. Keys written through the toggle
        # foreach ($values[$key]) are named inside its key list, so a quoted
        # occurrence counts — a key nobody ever mentions in the save branch
        # cannot end up in the config file.
        save = source.split("$action !== 'save'", 1)[-1]
        for key in self._default_keys():
            present = ("$values['%s']" % key in source
                       or "'%s'" % key in save)
            self.assertTrue(present, "%s is never saved by handle.php" % key)

    def test_every_default_key_is_seeded_in_default_cfg(self):
        with open(os.path.join(PLUGIN_SRC, "default.cfg"),
                  encoding="utf-8") as handle:
            cfg = handle.read()
        for key in self._default_keys():
            self.assertRegex(cfg, r"(?m)^%s=" % key,
                             "%s missing from default.cfg" % key)

    def test_every_default_key_is_visible_on_the_page(self):
        _, text = page_header()
        for key in self._default_keys():
            # A key is on the page when it has its own control, is posted as
            # an array (disks[]/ifaces[]), is read through $cfg (card_order),
            # or is a row of the toggle loop (rendered as name="<?=...?>").
            present = ('name="%s"' % key in text
                       or 'name="%s[]"' % key in text
                       or "$cfg['%s']" % key in text
                       or "'%s'," % key in text)
            self.assertTrue(present,
                            "%s has no control or reader on the page" % key)


class PluginDescriptorTest(unittest.TestCase):
    def _descriptor(self):
        path = os.path.join(ROOT, "plugin", "mnasx1-subscreen.plg")
        if not os.path.exists(path):
            self.skipTest(".plg not built yet; run plugin/build.sh")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        # The shipped descriptor uses DOCTYPE entities; resolve them so the
        # assertions can talk about real paths and versions.
        doctype = re.search(r"<!DOCTYPE PLUGIN \[(.*?)\]>", text, re.S)
        entities = dict(re.findall(r'<!ENTITY\s+(\w+)\s+"([^"]*)"', doctype.group(1)))
        for _ in range(4):
            for key, value in entities.items():
                entities[key] = re.sub(r"&(\w+);",
                                       lambda m: entities.get(m.group(1), m.group(0)),
                                       value)
        body = text[:doctype.start()] + text[doctype.end():]
        return re.sub(r"&(\w+);", lambda m: entities.get(m.group(1), m.group(0)), body)

    def test_payload_filename_carries_the_version(self):
        text = self._descriptor()
        version = open(os.path.join(ROOT, "plugin", "version"),
                       encoding="utf-8").read().strip()
        self.assertIn("mnasx1-subscreen-%s-x86_64-1.txz" % version, text)

    def test_payload_block_has_a_checksum_and_install_run(self):
        text = self._descriptor()
        block = re.search(r"<FILE Name=\"[^\"]*\.txz\"[^>]*>.*?</FILE>", text, re.S)
        self.assertIsNotNone(block)
        self.assertIn('Run="upgradepkg --install-new --reinstall"', block.group(0))
        self.assertRegex(block.group(0), r"<SHA256>[a-f0-9]{64}</SHA256>")

    def test_plugin_url_points_at_this_repository(self):
        text = self._descriptor()
        self.assertIn("tevenfeng/mnasx1_unraid_subscreen", text)
        self.assertIn("/main/plugin/mnasx1-subscreen.plg", text)

    def test_uninstall_block_removes_the_ram_copy(self):
        text = self._descriptor()
        remove = re.search(r'<FILE Run="/bin/bash" Method="remove">.*?</FILE>',
                           text, re.S)
        self.assertIsNotNone(remove)
        self.assertIn("rm -rf /usr/local/emhttp/plugins/mnasx1-subscreen",
                      remove.group(0))
        self.assertIn("rm -rf /usr/local/lib/mnasx1-subscreen", remove.group(0))

    def test_stale_flash_and_page_cache_are_cleaned_up(self):
        text = self._descriptor()
        self.assertIn("/boot/config/plugins/subscreen/rc.subscreen start", text,
                      "the old manual install must be migrated away")
        self.assertIn("/etc/rc.d/rc.subscreen", text)


if __name__ == "__main__":
    sys.exit(unittest.main())


class UpgradeHandlingTest(unittest.TestCase):
    """An upgrade replaces files under a running process; without a restart
    the old build keeps running and the update looks like it did nothing."""

    def _descriptor(self):
        path = os.path.join(ROOT, "plugin", "mnasx1-subscreen.plg")
        if not os.path.exists(path):
            self.skipTest(".plg not built yet; run plugin/build.sh")
        return open(path, encoding="utf-8").read()

    def test_install_block_restarts_a_running_daemon(self):
        text = self._descriptor()
        install = text.split('Method="remove"')[0]
        self.assertIn("/var/run/mnasx1-subscreen.pid", install)
        self.assertIn("restart.sh", install)
        self.assertIn("kill -0", install,
                      "only restart when a process is actually alive")
