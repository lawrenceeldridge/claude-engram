"""Privacy detection + redaction — the gate verbatim text passes before engram stores it.

A known-value corpus: every credential shape must be removed, and ordinary prose that merely
contains a trigger word ("token budget", "basic understanding") must survive untouched.
"""

from __future__ import annotations

import unittest

import _harness  # noqa: F401

from core.domain.privacy import REDACTED, privacy_flags, redact

R = REDACTED


class RedactTests(unittest.TestCase):
    def test_credentials_are_removed(self):
        cases = {
            "export API_KEY=abc123secretvalue now": f"export API_KEY={R} now",
            "password: hunter22": f"password: {R}",
            'PASSWORD="s3cr3t!pw"': f'PASSWORD="{R}"',
            'curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"': (
                f'curl -H "Authorization: Bearer {R}"'
            ),
            "Authorization: Basic dXNlcjpwYXNz": f"Authorization: Basic {R}",  # no digits: header context decides
            "authorization=Digest abc": f"authorization=Digest {R}",
            "Bearer abcdefgh12345 in a log": f"Bearer {R} in a log",
            "key sk-ant-api03-ABCDEFGHIJKLMNOP": f"key {R}",
            "AKIAIOSFODNN7EXAMPLE": R,
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789": R,
            "xoxb-1234567890-abcdefghij": R,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(redact(text, "/repo"), expected)

    def test_private_key_block_is_removed(self):
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(redact(f"key:\n{block}\ndone", "/repo"), f"key:\n{R}\ndone")

    def test_emails_and_non_project_paths_are_removed_project_paths_kept(self):
        self.assertEqual(redact("mail bob@example.com", "/repo"), f"mail {R}")
        self.assertEqual(
            redact("cat /Users/alice/secret/notes.txt and /Users/me/repo/app/x.py", "/Users/me/repo"),
            f"cat {R} and /Users/me/repo/app/x.py",
        )

    def test_an_unknown_project_path_redacts_every_home_path(self):
        self.assertEqual(redact("see /home/bob/work/x.py", ""), f"see {R}")

    def test_prose_with_trigger_words_survives(self):
        for text in (
            "the token budget is small",
            "token management is hard",
            "basic understanding of the system",
            "digest authentication overview",
            "basic setup of the repo",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text, "/repo"), text)


class PrivacyFlagsTests(unittest.TestCase):
    def test_flags_are_unchanged_by_the_move_from_bench(self):
        self.assertEqual(privacy_flags("ping ops@example.com", "/repo"), ["email"])
        self.assertEqual(privacy_flags("set the API_KEY", "/repo"), ["credential-shaped"])
        self.assertEqual(
            privacy_flags("open /Users/x/other/file.py", "/repo"), ["non-repo path: /Users/x/other/file.py"]
        )
        self.assertEqual(privacy_flags("plain text", "/repo"), [])


if __name__ == "__main__":
    unittest.main()
