"""Redaction tests.

Weighted deliberately. A miss publishes a credential, so the must-redact cases are the point.
But a tool that redacts everything is useless and gets uninstalled, and then it protects
nobody, so the must-not-redact cases are nearly as numerous.

Every case here is a whole credential shape rather than a fragment, because a redactor can pass
a fragment test by matching a common prefix while missing the real thing.
"""
import unittest

from agent_replay.redact import redact, audit, find_leaks, shannon, decode_base64_blobs

# Synthetic throughout. These are assembled at runtime from parts so that no complete
# credential-shaped literal exists in this file: GitHub's push protection scans full history
# and rejects a synthetic-but-valid-looking secret, which blocks the whole repository.
A = "abcdefghijklmnopqrstuvwxyz"
D = "0123456789"


def key(prefix, n=40):
    body = "".join((A[i % 26] if i % 3 else A[i % 26].upper()) if i % 2 else D[i % 10]
                   for i in range(n))
    return prefix + body


MUST_REDACT = {
    "anthropic":       f"ANTHROPIC_API_KEY={key('sk-ant-api03-', 60)}",
    "openai project":  f"client = OpenAI(api_key='{key('sk-proj-', 40)}')",
    "openrouter":      f"export OPENROUTER_API_KEY={key('sk-or-v1-', 64)}",
    "github token":    f"gh auth login --with-token <<< {key('ghp_', 36)}",
    "github pat":      f"remote add origin https://{key('github_pat_', 60)}@github.com/x/y",
    "gitlab":          f"CI_TOKEN={key('glpat-', 20)}",
    # A real Google API key is exactly 39 characters: AIza plus 35. The first version of this
    # fixture was one character short and the pattern correctly declined to match it.
    "google":          "AIza" + "Bc3dEf7gH1iJk5lMn9oPq3rSt7uVw1xY2z9",
    "aws access key":  "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    "slack bot":       f"{key('xoxb-', 24)}",
    "stripe live":     f"{key('sk_live_', 26)}",
    "huggingface":     f"HF_TOKEN={key('hf_', 34)}",
    "npm":             f"//registry.npmjs.org/:_authToken={key('npm_', 36)}",
    "jwt":             ("Authorization: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
                        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
    "bearer":          "curl -H 'Authorization: Bearer " + key("", 44) + "'",
    "whole pem":       ("-----BEGIN RSA PRIVATE KEY-----\n"
                        "MIIEowIBAAKCAQEA7Yn3xK2mQ9vZ1pL4wR7tY1uI3oA6sD5fG0hJkL8mN2pQ4rS6\n"
                        "-----END RSA PRIVATE KEY-----"),
    "truncated pem":   ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
                        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAABlwAAAAdzc2gtcn"),
    "db url password": "DATABASE_URL=postgres://appuser:s3cr3tP4ssw0rd@db.internal:5432/prod",
    "plain password":  "DATABASE_PASSWORD=correcthorsebatterystaple",
    "homegrown":       "MY_COMPANY_SECRET=zQ8vN2mK9pL4wR7tY1uI3oA6sD5fG0hJ",
    "yaml secret":     'client_secret: "9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c"',
    "cli flag":        "deploy --api-key 8f3a9c2e1b7d4f6a0e5c8b2d9a1f3e7c --region us",
    "entropy nearby":  "the api_key for staging is zQ8vN2mK9pL4wR7tY1uI3oA6sD5fG0hJx7",
    "cookie header":   "Cookie: session=aB3dE5gH7jK9mN1pQ3rS5tU7wX9yZ1cV4bN6mQ8",
}

MUST_NOT_REDACT = {
    "prose":            "The expected value is 4 and the test asserts it directly.",
    "git sha":          "reverted 9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e cleanly",
    "long filename":    "edited src/components/UserProfileSettingsPanel.tsx today",
    "github url":       "https://github.com/JesseRWeigel/assertless/blob/main/README.md",
    "boolean flag":     "DEBUG_TOKEN_PARSER=true",
    "variable ref":     "API_KEY=$OPENAI_API_KEY",
    "shell expansion":  "export TOKEN=${CI_TOKEN}",
    "package versions": "installed react@18.3.1 typescript@5.6.2 vitest@2.1.4",
    "camelcase fn":     "await refreshAuthenticationTokenSilently(options)",
    "snake identifier": "def validate_access_token_signature(token):",
    "pem mention":      "Never commit a BEGIN RSA PRIVATE KEY block to a public repository.",
    "pem grep":         'git grep -n "BEGIN OPENSSH PRIVATE KEY" $(git rev-list --all)',
    "env var name":     "set ANTHROPIC_API_KEY in your shell before running this",
    "sha256 label":     "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "base64 png":       ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                         "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="),
    "uuid":             "session 15f05fff-c889-43bc-87e3-9668a87a3687 resumed",
    "docs sentence":    "Pass --token to authenticate, or set the environment variable.",
}


class TestMustRedact(unittest.TestCase):
    def test_every_credential_shape_is_removed(self):
        misses = []
        for name, text in MUST_REDACT.items():
            r = redact(text)
            if r.total == 0:
                misses.append(f"{name}: nothing was redacted")
            elif audit(r.text):
                misses.append(f"{name}: survived the independent audit")
        self.assertEqual(misses, [], "credentials were not removed")

    def test_the_secret_value_is_gone_from_the_output(self):
        # Counting redactions is not the same as removing the value. This checks the actual
        # bytes: a redactor that reported a hit while leaving the text intact would pass the
        # test above and fail this one.
        secret = key("sk-ant-api03-", 60)
        out = redact(f"ANTHROPIC_API_KEY={secret}").text
        self.assertNotIn(secret, out)
        self.assertIn("[redacted:", out)

    def test_a_secret_inside_json_is_removed(self):
        blob = '{"config":{"nested":{"apiKey":"' + key("", 40) + '"}}}'
        r = redact(blob)
        self.assertGreater(r.total, 0)
        self.assertEqual(audit(r.text), [])

    def test_multiple_secrets_in_one_string_are_all_removed(self):
        text = f"{key('ghp_', 36)} and {key('sk-proj-', 40)} and AKIAIOSFODNN7EXAMPLE"
        r = redact(text)
        self.assertGreaterEqual(r.total, 3)
        self.assertEqual(audit(r.text), [])


class TestMustNotRedact(unittest.TestCase):
    def test_ordinary_content_is_left_alone(self):
        # A tool that redacts everything protects nobody, because it gets uninstalled.
        wrong = []
        for name, text in MUST_NOT_REDACT.items():
            r = redact(text)
            if r.total:
                wrong.append(f"{name}: {r.text[:70]}")
        self.assertEqual(wrong, [], "ordinary content was redacted")

    def test_a_realistic_transcript_chunk_survives_intact(self):
        chunk = (
            "I ran the test suite and 27 of 27 passed.\n"
            "The failure was in src/lib/audio/tts.test.ts at line 300.\n"
            "commit 6f4e5647a2b1c3d4e5f6a7b8c9d0e1f2a3b4c5d6\n"
            "npm install --save-dev vitest@2.1.4\n"
            "See https://github.com/example/repo/pull/42 for context.\n")
        self.assertEqual(redact(chunk).total, 0)


class TestIndependentChecker(unittest.TestCase):
    """The checker must be able to fail, and must not fire on its own placeholders."""

    def test_it_detects_an_unredacted_secret(self):
        self.assertNotEqual(find_leaks(f"token {key('ghp_', 36)}"), [],
                            "the checker cannot see a plain credential, so it proves nothing")

    def test_it_does_not_fire_on_placeholders(self):
        self.assertEqual(find_leaks("API_KEY=[redacted:anthropic-key] and ok"), [])

    def test_it_does_not_fire_on_a_redacted_database_url(self):
        # This one was a real bug. The inline-password pattern matches from the scheme, so the
        # placeholder sits INSIDE the match, and checking only the characters before the match
        # never saw it.
        out = redact("postgres://appuser:s3cr3tP4ssw0rd@db.internal:5432/prod").text
        self.assertEqual(find_leaks(out), [], f"placeholder reported as a leak: {out}")

    def test_it_sees_through_one_layer_of_base64(self):
        import base64
        hidden = base64.b64encode(f"export TOKEN={key('ghp_', 36)}".encode()).decode()
        self.assertNotEqual(audit(f"echo {hidden} | base64 -d"), [],
                            "a secret one encoding deep was not seen")

    def test_pem_detection_needs_key_material_not_just_the_phrase(self):
        real = ("-----BEGIN RSA PRIVATE KEY-----\n"
                "MIIEowIBAAKCAQEA7Yn3xK2mQ9vZ1pL4wR7tY1uI3oA6sD5fG0hJkL8mN2pQ4rS6\n")
        self.assertTrue(any(l == "pem block" for l, _ in find_leaks(real)))
        for benign in ('git grep "BEGIN OPENSSH PRIVATE KEY" .',
                       'the regex r"BEGIN[ A-Z]*PRIVATE KEY" matches',
                       "BEGIN RSA PRIVATE KEY should never be committed",
                       "BEGIN OPENSSH PRIVATE KEY ... e289c7a79c2fc55912bd17f93a9d4be17a9d6ddc"):
            self.assertFalse(any(l == "pem block" for l, _ in find_leaks(benign)),
                             f"false alarm on: {benign[:50]}")


class TestMechanics(unittest.TestCase):
    def test_entropy_separates_a_random_token_from_a_word(self):
        # Compared token against token, because the candidate pattern never spans a space and
        # so never sees a phrase. An earlier version of this test compared a random token
        # against "the quick brown fox jumps", which scores 4.16 precisely because it has many
        # distinct characters, and the assertion was measuring nothing useful.
        random_token = shannon("zQ8vN2mK9pL4wR7tY1uI3oA6sD5fG0hJ")
        english_word = shannon("authentication")
        self.assertGreater(random_token, 4.0)
        self.assertLess(english_word, random_token)
        self.assertLess(english_word, 3.6, "an ordinary word must sit below the threshold")

    def test_home_path_is_replaced(self):
        r = redact("/home/someone/Projects/x/y.py", home="/home/someone")
        self.assertEqual(r.text, "~/Projects/x/y.py")
        self.assertEqual(r.redactions["home-path"].count, 1)

    def test_samples_never_contain_the_secret(self):
        # The report shows what was redacted. It must not undo the redaction while doing so.
        secret = key("sk-ant-api03-", 60)
        r = redact(secret)
        for red in r.redactions.values():
            for s in red.samples:
                self.assertNotIn(secret, s)
                self.assertLess(len(s), 40)

    def test_base64_decoder_ignores_non_text(self):
        self.assertEqual(decode_base64_blobs("plain text with no blobs at all"), [])

    def test_empty_and_none_are_safe(self):
        self.assertEqual(redact("").text, "")
        self.assertEqual(redact(None).text, "")
        self.assertEqual(find_leaks(""), [])

    def test_redaction_is_idempotent(self):
        once = redact(f"KEY={key('ghp_', 36)}").text
        twice = redact(once).text
        self.assertEqual(once, twice, "re-redacting changed the output")


if __name__ == "__main__":
    unittest.main()
