import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import machines_core as core  # noqa: E402

PID = "0123456789abcdef0123456789abcdef"


class SshConfigTextTests(unittest.TestCase):
    def parse(self, text, ssh_dir="/nonexistent"):
        hosts, warnings = core.parse_ssh_config_text(text, ssh_dir)
        return hosts, warnings

    def aliases(self, text):
        return [h.alias for h in self.parse(text)[0]]

    def test_simple_hosts_with_hostname_and_user(self):
        hosts, _ = self.parse(
            "Host workbox\n"
            "    HostName 192.0.2.5\n"
            "    User deploy\n"
            "\n"
            "Host other\n"
            "  hostname other.example\n"
        )
        self.assertEqual([h.alias for h in hosts], ["workbox", "other"])
        self.assertEqual(hosts[0].hostname, "192.0.2.5")
        self.assertEqual(hosts[0].user, "deploy")
        self.assertEqual(hosts[0].describe(), "deploy@192.0.2.5")
        self.assertEqual(hosts[1].hostname, "other.example")
        self.assertIsNone(hosts[1].user)

    def test_placeholder_patterns_are_skipped(self):
        text = "Host *\n  User x\nHost web-?\nHost !bad good\nHost *.example real\n"
        self.assertEqual(self.aliases(text), ["good", "real"])

    def test_multiple_names_per_line_share_settings(self):
        hosts, _ = self.parse("Host a b c\n  HostName shared\n")
        self.assertEqual([h.alias for h in hosts], ["a", "b", "c"])
        self.assertTrue(all(h.hostname == "shared" for h in hosts))

    def test_equals_syntax_and_case_insensitive_keywords(self):
        hosts, _ = self.parse("HOST=one\nhostNAME = h1\nhost two\nUSER=admin\n")
        self.assertEqual([h.alias for h in hosts], ["one", "two"])
        self.assertEqual(hosts[0].hostname, "h1")
        self.assertEqual(hosts[1].user, "admin")

    def test_comments_and_quotes(self):
        text = (
            "# full line comment\n"
            "   # indented comment\n"
            "Host alpha # trailing comment\n"
            'Host "quoted name"\n'
        )
        self.assertEqual(self.aliases(text), ["alpha", "quoted name"])

    def test_match_block_is_ignored(self):
        hosts, _ = self.parse(
            "Host a\n  HostName real-a\n"
            "Match host a exec true\n  HostName from-match\n  User matchuser\n"
        )
        self.assertEqual(hosts[0].hostname, "real-a")
        self.assertIsNone(hosts[0].user)

    def test_first_value_wins_and_duplicates_collapse(self):
        hosts, _ = self.parse("Host a\n HostName one\n HostName two\nHost a\n HostName three\n")
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0].hostname, "one")

    def test_matches_filter(self):
        h = core.SshHost(alias="workbox", hostname="192.0.2.5", user="deploy")
        self.assertTrue(h.matches(""))
        self.assertTrue(h.matches("WORK"))
        self.assertTrue(h.matches("deploy"))
        self.assertTrue(h.matches("192.0"))
        self.assertFalse(h.matches("zzz"))


class SshConfigIncludeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_relative_include_with_glob(self):
        cfg = self.write("config", "Host main\nInclude conf.d/*.conf\nHost last\n")
        self.write("conf.d/b.conf", "Host bee\n")
        self.write("conf.d/a.conf", "Host ay\n  User u\n")
        self.write("conf.d/ignored.txt", "Host nope\n")
        hosts, warnings = core.load_ssh_hosts(cfg, self.dir)
        self.assertEqual([h.alias for h in hosts], ["main", "ay", "bee", "last"])
        self.assertEqual(hosts[1].user, "u")
        self.assertEqual(warnings, [])

    def test_absolute_include_and_missing_include(self):
        other = self.write("elsewhere/extra", "Host extra\n")
        cfg = self.write("config", f"Include {other} does-not-exist\nHost main\n")
        hosts, warnings = core.load_ssh_hosts(cfg, self.dir)
        self.assertEqual([h.alias for h in hosts], ["extra", "main"])
        self.assertEqual(warnings, [])

    def test_include_recursion_guard(self):
        cfg = self.write("config", "Host a\nInclude loop\n")
        self.write("loop", "Host b\nInclude config\n")
        hosts, warnings = core.load_ssh_hosts(cfg, self.dir)
        self.assertEqual([h.alias for h in hosts], ["a", "b"])
        self.assertTrue(any("Schleife" in w for w in warnings))

    def test_missing_config_gives_no_hosts(self):
        hosts, warnings = core.load_ssh_hosts(os.path.join(self.dir, "nothing"), self.dir)
        self.assertEqual(hosts, [])
        self.assertEqual(warnings, [])

    def test_default_ssh_config_env_override(self):
        path, base = core.default_ssh_config({"HERDR_MACHINES_MENU_SSH_CONFIG": os.path.join(self.dir, "cfg")})
        self.assertEqual(path, os.path.join(self.dir, "cfg"))
        self.assertEqual(base, self.dir)
        path, base = core.default_ssh_config({})
        self.assertEqual(path, os.path.expanduser("~/.ssh/config"))
        self.assertEqual(base, os.path.expanduser("~/.ssh"))


class MachineListTests(unittest.TestCase):
    def test_parse_list(self):
        text = (
            '[{"id":"%s","label":"workbox","target":"deploy@workbox","session":null,'
            '"enabled":true,"selected":false},'
            '{"id":"ffffffffffffffffffffffffffffffff","label":"build","target":"ssh://b:2222",'
            '"session":"main","enabled":false,"selected":true}]' % PID
        )
        machines = core.parse_machine_list(text)
        self.assertEqual(len(machines), 2)
        self.assertEqual(machines[0].id, PID)
        self.assertEqual(machines[0].label, "workbox")
        self.assertIsNone(machines[0].session)
        self.assertTrue(machines[0].enabled)
        self.assertEqual(machines[1].session, "main")
        self.assertFalse(machines[1].enabled)
        self.assertTrue(machines[1].selected)

    def test_empty_array(self):
        self.assertEqual(core.parse_machine_list("[]\n"), [])

    def test_empty_output_is_an_error(self):
        for text in ("", "  \n"):
            with self.assertRaises(core.ParseError) as ctx:
                core.parse_machine_list(text)
            self.assertIn("keine Ausgabe", str(ctx.exception))

    def test_broken_json(self):
        with self.assertRaises(core.ParseError) as ctx:
            core.parse_machine_list('[{"id": ')
        self.assertIn("ungültiges JSON", str(ctx.exception))

    def test_wrong_shapes(self):
        for text in ('{"id":"x"}', "[1]", '[{"label":"no id"}]'):
            with self.assertRaises(core.ParseError):
                core.parse_machine_list(text)

    def test_missing_label_falls_back_to_id(self):
        machines = core.parse_machine_list('[{"id":"%s"}]' % PID)
        self.assertEqual(machines[0].label, PID)
        self.assertEqual(machines[0].target, "")


class MachineStatusTests(unittest.TestCase):
    TEXT = (
        '[{"id":"a","label":"A","status":"reachable","error":null},'
        '{"id":"b","label":"B","status":"error","error":"ssh: connect refused"},'
        '{"id":"c","label":"C","status":"disabled","error":null}]'
    )

    def test_parse_status(self):
        result = core.parse_machine_status(self.TEXT)
        self.assertEqual(set(result), {"a", "b", "c"})
        self.assertEqual(result["a"].status, "reachable")
        self.assertIsNone(result["a"].error)
        self.assertEqual(result["b"].error, "ssh: connect refused")

    def test_empty_and_broken(self):
        self.assertEqual(core.parse_machine_status("[]"), {})
        with self.assertRaises(core.ParseError):
            core.parse_machine_status("")
        with self.assertRaises(core.ParseError):
            core.parse_machine_status("not json")

    def test_rc1_with_valid_json_is_normal(self):
        result = core.interpret_status_result(1, self.TEXT, "")
        self.assertEqual(result["b"].status, "error")

    def test_failure_without_json_carries_stderr(self):
        with self.assertRaises(core.ParseError) as ctx:
            core.interpret_status_result(2, "", "boom: catalog unreadable")
        self.assertIn("Exit-Code 2", str(ctx.exception))
        self.assertIn("boom: catalog unreadable", str(ctx.exception))

    def test_rc0_with_garbage(self):
        with self.assertRaises(core.ParseError) as ctx:
            core.interpret_status_result(0, "garbage", "")
        self.assertIn("ungültiges JSON", str(ctx.exception))


class StatusLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(core.status_label("reachable"), "erreichbar")
        self.assertEqual(core.status_label("disabled"), "deaktiviert")
        self.assertEqual(core.status_label("error"), "Fehler")
        self.assertEqual(core.status_label("attention"), "Attention")
        self.assertEqual(core.status_label("auth"), "Anmeldung nötig")
        self.assertEqual(core.status_label("auth_required"), "Anmeldung nötig")
        self.assertEqual(core.status_label("Reachable"), "erreichbar")
        self.assertEqual(core.status_label("connecting"), "connecting")
        self.assertEqual(core.status_label(""), "")
        self.assertEqual(core.status_label(None), "")

    def test_kinds(self):
        self.assertEqual(core.status_kind("reachable"), "ok")
        self.assertEqual(core.status_kind("disabled"), "off")
        self.assertEqual(core.status_kind("error"), "error")
        self.assertEqual(core.status_kind("attention"), "attention")
        self.assertEqual(core.status_kind("needs_auth"), "auth")
        self.assertEqual(core.status_kind("weird"), "unknown")
        self.assertEqual(core.status_kind(None), "none")


class ArgvTests(unittest.TestCase):
    H = "/opt/herdr"

    def test_list_and_status(self):
        self.assertEqual(core.argv_list(self.H), [self.H, "machine", "list", "--json"])
        self.assertEqual(core.argv_status(self.H), [self.H, "machine", "status", "--json"])

    def test_add_variants(self):
        self.assertEqual(core.argv_add(self.H, "workbox"), [self.H, "machine", "add", "workbox"])
        self.assertEqual(
            core.argv_add(self.H, " ssh://u@h:2222 ", "Build machine", "main"),
            [self.H, "machine", "add", "ssh://u@h:2222", "--label", "Build machine",
             "--remote-session", "main"],
        )
        self.assertEqual(core.argv_add(self.H, "u@h", "", "  "), [self.H, "machine", "add", "u@h"])
        self.assertEqual(
            core.argv_add(self.H, "u@h", remote_session="s"),
            [self.H, "machine", "add", "u@h", "--remote-session", "s"],
        )

    def test_add_rejects_bad_values(self):
        for bad in ("", "   ", "-oProxyCommand=x"):
            with self.assertRaises(ValueError):
                core.argv_add(self.H, bad)
        with self.assertRaises(ValueError):
            core.argv_add(self.H, "h", label="-x")
        with self.assertRaises(ValueError):
            core.argv_add(self.H, "h", remote_session="--y")

    def test_id_actions(self):
        self.assertEqual(core.argv_enable(self.H, PID), [self.H, "machine", "enable", PID])
        self.assertEqual(core.argv_disable(self.H, PID), [self.H, "machine", "disable", PID])
        self.assertEqual(core.argv_remove(self.H, PID), [self.H, "machine", "remove", PID])
        self.assertEqual(core.argv_reconnect(self.H, PID), [self.H, "machine", "reconnect", PID])
        self.assertEqual(
            core.argv_rename(self.H, PID, " Neuer Name "),
            [self.H, "machine", "rename", PID, "--label", "Neuer Name"],
        )

    def test_id_actions_refuse_labels(self):
        for fn in (core.argv_enable, core.argv_disable, core.argv_remove, core.argv_reconnect):
            with self.assertRaises(ValueError):
                fn(self.H, "workbox")
            with self.assertRaises(ValueError):
                fn(self.H, PID[:-1])
        with self.assertRaises(ValueError):
            core.argv_rename(self.H, "workbox", "x")
        with self.assertRaises(ValueError):
            core.argv_rename(self.H, PID, "  ")


class HerdrBinTests(unittest.TestCase):
    def test_env_and_fallback(self):
        self.assertEqual(core.herdr_bin({"HERDR_BIN_PATH": "/x/herdr"}), "/x/herdr")
        self.assertEqual(core.herdr_bin({"HERDR_BIN_PATH": ""}), "herdr")
        self.assertEqual(core.herdr_bin({}), "herdr")


class RunCliTests(unittest.TestCase):
    def test_captures_everything(self):
        rc, out, err = core.run_cli([sys.executable, "-c",
                                     "import sys; print('aus'); print('fehler', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((rc, out, err), (3, "aus\n", "fehler\n"))

    def test_missing_binary(self):
        rc, out, err = core.run_cli(["/nonexistent/herdr-binary", "machine", "list"])
        self.assertEqual(rc, core.RC_NOT_FOUND)
        self.assertEqual(out, "")
        self.assertIn("/nonexistent/herdr-binary", err)

    def test_timeout(self):
        rc, _, err = core.run_cli([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
        self.assertEqual(rc, core.RC_TIMEOUT)
        self.assertIn("Zeitüberschreitung", err)


    def test_timeout_also_ends_grandchildren(self):
        # herdr starts ssh children that keep the pipes open; the timeout must
        # still return promptly instead of waiting for them.
        start = time.monotonic()
        rc, _, err = core.run_cli(["sh", "-c", "sleep 8 & sleep 8"], timeout=0.3)
        self.assertEqual(rc, core.RC_TIMEOUT)
        self.assertLess(time.monotonic() - start, 4)
        self.assertIn("Zeitüberschreitung", err)


class DisplayWidthTests(unittest.TestCase):
    def test_control_characters_are_blanked(self):
        self.assertEqual(core.truncate("a\x1bb\rc", 10), "a b c")
        self.assertEqual(core.wrap("eins\r\nzwei\x07", 20), ["eins", "zwei"])

    def test_width_and_truncate(self):
        self.assertEqual(core.display_width("Grüße"), 5)
        self.assertEqual(core.display_width("日本"), 4)
        self.assertEqual(core.truncate("abcdef", 4), "abc…")
        self.assertEqual(core.truncate("abc", 4), "abc")
        self.assertEqual(core.truncate("日本語", 4), "日…")
        self.assertEqual(core.truncate("x", 0), "")
        self.assertEqual(core.pad("ab", 4), "ab  ")
        self.assertEqual(core.display_width(core.pad("日本語テキスト", 5)), 5)

    def test_wrap(self):
        self.assertEqual(core.wrap("eins zwei drei", 9), ["eins zwei", "drei"])
        self.assertEqual(core.wrap("a\n\nb", 5), ["a", "", "b"])
        self.assertEqual(core.wrap("abcdefgh", 3), ["abc", "def", "gh"])


if __name__ == "__main__":
    unittest.main()
