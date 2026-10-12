import os
import unittest

from support import Env, run_git, write
from cvr import packet

SECRET = "sk-ant-" + "a" * 30
TOKEN_LINE = 'password = "' + "abcd1234" * 3 + '"'


class ReviewPacketTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.repo = self.env.repo({"app.py": "def add(a, b):\n    return a + b\n", "old.py": "x = 1\n",
                                   "pic.bin": b"\x89PNG\x00\x01"})

    def tearDown(self):
        self.env.close()

    def test_changes_untracked_deleted_and_exclusions(self):
        write(os.path.join(self.repo, "app.py"), "def add(a, b):\n    return a - b\nKEY = '%s'\n" % SECRET)
        write(os.path.join(self.repo, "new.py"), TOKEN_LINE + "\n")
        write(os.path.join(self.repo, ".env"), "X=" + SECRET + "\n")
        write(os.path.join(self.repo, "config", "secrets", "k.txt"), "hidden\n")
        write(os.path.join(self.repo, "big.txt"), "y\n" * 150000)
        os.remove(os.path.join(self.repo, "old.py"))
        text, guidelines, stats = packet.review_packet(self.repo)
        self.assertEqual(guidelines, "")
        self.assertEqual(stats["files"], ["app.py", "new.py"])
        self.assertIn("    2|     return a - b", text)
        self.assertIn("--- FILE new.py (new, untracked) ---", text)
        self.assertIn("-x = 1", text)  # the deletion appears in the diff
        self.assertNotIn(SECRET, text)
        self.assertNotIn("abcd1234abcd1234", text)
        self.assertNotIn("hidden", text)
        self.assertGreaterEqual(stats["redactions"], 3)
        omitted = dict(stats["omitted"])
        self.assertEqual(omitted[".env"], "sensitive path")
        self.assertEqual(omitted["config/secrets/k.txt"], "sensitive path")
        self.assertEqual(omitted["big.txt"], "larger than 200 KB")

    def test_binary_change_is_named_not_sent(self):
        write(os.path.join(self.repo, "pic.bin"), b"\x89PNG\x00\x02")
        text, _, stats = packet.review_packet(self.repo)
        self.assertEqual(dict(stats["omitted"]), {"pic.bin": "binary"})

    def test_nothing_to_review_and_too_large(self):
        with self.assertRaises(packet.PacketError):
            packet.review_packet(self.repo)
        write(os.path.join(self.repo, "app.py"), "z = 2\n" * 5000)
        with self.assertRaises(packet.PacketError) as ctx:
            packet.review_packet(self.repo, max_chars=1000)
        self.assertIn("--paths", str(ctx.exception))

    def test_base_and_paths(self):
        run_git(self.repo, "checkout", "-q", "-b", "feature")
        write(os.path.join(self.repo, "app.py"), "def add(a, b):\n    return b + a\n")
        run_git(self.repo, "commit", "-q", "-am", "swap")
        write(os.path.join(self.repo, "other.py"), "w = 3\n")
        text, _, stats = packet.review_packet(self.repo, base="main")
        self.assertEqual(stats["files"], ["app.py", "other.py"])
        _, _, stats = packet.review_packet(self.repo, base="main", paths=["other.py"])
        self.assertEqual(stats["files"], ["other.py"])
        with self.assertRaises(packet.PacketError):
            packet.review_packet(self.repo, base="no-such-branch")

    def test_symlink_is_not_followed(self):
        outside = os.path.join(self.env.tmp, "outside-key.txt")
        write(outside, "TOPSECRETVALUE\n")
        os.symlink(outside, os.path.join(self.repo, "notes.txt"))
        write(os.path.join(self.repo, "app.py"), "x = 9\n")
        text, _, stats = packet.review_packet(self.repo)
        self.assertNotIn("TOPSECRETVALUE", text)
        self.assertEqual(dict(stats["omitted"])["notes.txt"], "symbolic link")

    def test_file_names_are_not_patterns(self):
        repo = self.env.repo({"*": "star\n", ".env": "X=old\n", "[a].py": "a = 1\n"})
        write(os.path.join(repo, "*"), "star changed\n")
        write(os.path.join(repo, ".env"), "X=NEWSECRETVALUE\n")
        text, _, stats = packet.review_packet(repo)
        self.assertNotIn("NEWSECRETVALUE", text)
        self.assertEqual(stats["files"], ["*"])
        _, _, stats = packet.review_packet(repo, paths=["*"])
        self.assertEqual(stats["files"], ["*"])

    def test_large_tracked_file_stays_out_of_the_diff(self):
        run_git(self.repo, "checkout", "-q", "-b", "feature")
        write(os.path.join(self.repo, "dump.txt"), "LARGEROW\n" * 30000)
        write(os.path.join(self.repo, "app.py"), "x = 9\n")
        run_git(self.repo, "add", "dump.txt")
        text, _, stats = packet.review_packet(self.repo)
        self.assertNotIn("LARGEROW", text)
        self.assertIn("left out of the diff too", dict(stats["omitted"])["dump.txt"])
        run_git(self.repo, "commit", "-q", "-am", "dump")
        os.remove(os.path.join(self.repo, "dump.txt"))  # a large deletion stays out as well
        text, _, stats = packet.review_packet(self.repo)
        self.assertNotIn("LARGEROW", text)

    def test_small_change_to_a_large_file_keeps_its_diff(self):
        write(os.path.join(self.repo, "big.txt"), "row\n" * 60000)
        run_git(self.repo, "add", "big.txt")
        run_git(self.repo, "commit", "-q", "-m", "big")
        write(os.path.join(self.repo, "big.txt"), "row\n" * 30000 + "CHANGEDROW\n" + "row\n" * 29999)
        text, _, stats = packet.review_packet(self.repo)
        self.assertIn("+CHANGEDROW", text)
        self.assertEqual(dict(stats["omitted"])["big.txt"], "larger than 200 KB; only its diff is included")

    def test_directory_entry_is_named_not_read(self):
        os.makedirs(os.path.join(self.repo, "vendored"))
        write(os.path.join(self.repo, "app.py"), "x = 9\n")
        self.assertEqual(packet.read_text(os.path.join(self.repo, "vendored"))[1],
                         "directory (for example a submodule)")

    def test_private_key_redaction_keeps_line_numbers(self):
        key = "-----BEGIN RSA PRIVATE " + "KEY-----\nAAA\nBBB\n-----END RSA PRIVATE " + "KEY-----"
        write(os.path.join(self.repo, "app.py"), "a = 1\nK = '''%s'''\nlast = 2\n" % key)
        text, _, _ = packet.review_packet(self.repo)
        self.assertIn("    6| last = 2", text)
        self.assertNotIn("AAA", text)

    def test_quoted_json_secret_is_redacted(self):
        write(os.path.join(self.repo, "config.json"), '{"password": "abcd1234abcd1234abcd", "clientSecret": '
                                                    '"zyxw9876zyxw9876zyxw"}\n')
        text, _, stats = packet.review_packet(self.repo)
        self.assertNotIn("abcd1234abcd1234abcd", text)
        self.assertNotIn("zyxw9876zyxw9876zyxw", text)

    def test_repository_without_commits(self):
        repo = self.env.repo({"a.py": "a = 1\n"}, commit=False)
        text, _, stats = packet.review_packet(repo)
        self.assertEqual(stats["files"], ["a.py"])

    def test_guidelines_are_opt_in_and_redacted(self):
        write(os.path.join(self.repo, "CONTRIBUTING.md"), "Use snake_case. " + SECRET + "\n")
        write(os.path.join(self.repo, "app.py"), "x = 9\n")
        _, guidelines, stats = packet.review_packet(self.repo, guideline_files=["CONTRIBUTING.md", "missing.md"])
        self.assertIn("Use snake_case.", guidelines)
        self.assertNotIn(SECRET, guidelines)
        self.assertGreater(stats["guideline_chars"], 0)


class ContextPacketTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_context_files(self):
        write(os.path.join(self.env.tmp, "notes.md"), "plan " + SECRET + "\n")
        write(os.path.join(self.env.tmp, ".env"), "X=1\n")
        text, stats = packet.context_packet("Which?", ["notes.md", ".env"], self.env.tmp)
        self.assertIn("=== QUESTION ===\nWhich?", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(stats["files"], ["notes.md"])
        with self.assertRaises(packet.PacketError):
            packet.context_packet("Which?", ["absent.md"], self.env.tmp)

    def test_question_is_redacted_and_links_to_sensitive_files_are_refused(self):
        write(os.path.join(self.env.tmp, ".env"), "DATABASE_URL=postgres://u:p@h/db\n")
        os.symlink(os.path.join(self.env.tmp, ".env"), os.path.join(self.env.tmp, "notes.md"))
        text, stats = packet.context_packet("Why does " + SECRET + " fail?", ["notes.md"], self.env.tmp)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("postgres://", text)
        self.assertEqual(dict(stats["omitted"]), {"notes.md": "sensitive path"})
        self.assertEqual(stats["redactions"], 1)

    def test_password_forms(self):
        hidden = ["p@ssw0rd", "correct horse battery staple", "abcdefghijklmnop!xyz", "it's-a-value", 'say "hi" now',
                  'pa\\"ss-word', "ab"]
        lines = ['password = "%s"' % hidden[0], '{"password": "%s"}' % hidden[1], "PASSWORD=%s" % hidden[2],
                 'password = "%s"' % hidden[3], "password: '%s'" % hidden[4], 'secret = "%s"' % hidden[5],
                 'token = "%s"' % hidden[6]]
        out, n = packet.redact("\n".join(lines))
        for value in hidden:
            self.assertNotIn(value, out)
        for part in ("!xyz", "ss-word", "hi"):
            self.assertNotIn(part, out)
        self.assertEqual(n, len(lines))
        more = {"password: hunter22": "hunter22", "DB_PASSWORD=hunter22": "hunter22",
                "url = postgres://admin:S3cretPass@db.example.com/app": "S3cretPass",
                'pwd = "hunter22xx"': "hunter22xx", 'passphrase: "long phrase here"': "long phrase",
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456": "abcdefghijklmnop",
                'private_key = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"': "MIIEvQIB"}
        for line, value in more.items():
            out, n = packet.redact(line)
            self.assertNotIn(value, out, line)
        for kept in ("password = get_password_from_environment(name)", "author: Jane Doe", "pwd = os.getcwd()",
                     "token = tokens[0]", "see https://example.com/docs for details"):
            self.assertEqual(packet.redact(kept), (kept, 0), kept)
        for line, value in (('secret_key = "s3cr3t-value"', "s3cr3t"), ("token_value: abcdefghijklmnopqrstu", "abcdefgh"),
                            ("pass" + 'word = "abcdefghijklmnop' + "'" + 'qrst"', "qrst")):
            out, n = packet.redact(line)
            self.assertNotIn(value, out, line)

    def test_long_line_does_not_stall_redaction(self):
        import time
        line = "password = " + "a" * 190000 + "()\ntoken = " + "b" * 190000 + "(\n"
        start = time.monotonic()
        out, n = packet.redact(line)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual((out, n), (line, 0))  # calls are left alone

    def test_diff_limit_counts_bytes(self):
        repo = self.env.repo({"big.txt": "x\n" * 60000})
        write(os.path.join(repo, "big.txt"), "x\n" * 60000 + chr(0xE9) * 120000 + "\n")  # 240 KB, 2 bytes each
        text, _, stats = packet.review_packet(repo)
        self.assertNotIn(chr(0xE9) * 10, text)
        self.assertIn("left out of the diff too", dict(stats["omitted"])["big.txt"])

    def test_credential_files_and_links_out_of_the_folder(self):
        for path in (".aws/credentials", "home/.git-credentials", ".pgpass", ".kube/config", ".docker/config.json"):
            self.assertTrue(packet.sensitive(path), path)
        folder = os.path.join(self.env.tmp, "project")
        write(os.path.join(self.env.tmp, "elsewhere.md"), "outside text\n")
        write(os.path.join(folder, "a.md"), "inside text\n")
        os.symlink(os.path.join(self.env.tmp, "elsewhere.md"), os.path.join(folder, "notes.md"))
        os.symlink(self.env.tmp, os.path.join(folder, "ext"))  # a linked directory on the way
        text, stats = packet.context_packet("Q", ["a.md", "notes.md", "ext/elsewhere.md", "../elsewhere.md"], folder)
        self.assertIn("inside text", text)
        self.assertNotIn("outside text", text)
        self.assertEqual(dict(stats["omitted"]), {"notes.md": "resolves outside the folder",
                                                  "ext/elsewhere.md": "resolves outside the folder",
                                                  "../elsewhere.md": "resolves outside the folder"})
        text, _ = packet.context_packet("Q", [os.path.join(self.env.tmp, "elsewhere.md")], folder)
        self.assertIn("outside text", text)  # an absolute path is the user's explicit choice

    def test_redaction_patterns(self):
        cases = ["ghp_" + "b" * 36, "AKIA" + "C" * 16, "xai-" + "d" * 24, "AIza" + "e" * 35,
                 "-----BEGIN RSA PRIVATE " + "KEY-----\nabc\n-----END RSA PRIVATE " + "KEY-----",
                 "api_key: " + "f1" * 10]
        for value in cases:
            out, n = packet.redact("before " + value + " after")
            self.assertEqual(n, 1, value)
            self.assertIn("<redacted:", out)
        out, n = packet.redact("token_count = compute_tokens(text)")
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
