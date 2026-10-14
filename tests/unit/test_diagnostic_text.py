import unittest

from nmrpeak_provider.diagnostic_text import diagnostic_text, exception_diagnostic


class DiagnosticTextTests(unittest.TestCase):
    def test_preserves_reason_while_redacting_before_truncation(self):
        message = "Unsupported multiplicity xy; api_key=credential-value Bearer other-token " + "x" * 200
        rendered = diagnostic_text(message, limit=100)
        self.assertIn("Unsupported multiplicity xy", rendered)
        self.assertNotIn("credential-value", rendered)
        self.assertNotIn("other-token", rendered)
        self.assertIn("truncated", rendered)

    def test_redacted_credentials_preserve_following_sentence_punctuation(self):
        for punctuation in (".", "!", "?"):
            message = "API_KEY=[redacted]" + punctuation + " Generation did not start."
            self.assertEqual(diagnostic_text(message), message)
            self.assertEqual(diagnostic_text(diagnostic_text(message)), message)
        self.assertEqual(diagnostic_text("API_KEY=[redacted]still-secret"), "API_KEY=[redacted]")

    def test_handles_known_secrets_private_keys_and_url_credentials(self):
        message = "TLS error https://user:password@example.test/path; custom-secret\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        rendered = diagnostic_text(message, secrets=("custom-secret",))
        for secret in ("password", "custom-secret", "abc"):
            self.assertNotIn(secret, rendered)
        self.assertIn("example.test/path", rendered)

    def test_preserves_primary_cause_and_cleanup_note(self):
        cause = OSError("No space left on device; password=hunter2")
        error = RuntimeError("Cannot retain result; Authorization: Bearer primary-secret")
        error.__cause__ = cause
        error.add_note("Worker stop unconfirmed")
        rendered = exception_diagnostic(error)
        for fact in ("Cannot retain result", "No space left on device", "Worker stop unconfirmed"):
            self.assertIn(fact, rendered)
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("primary-secret", rendered)
        self.assertEqual(diagnostic_text(rendered), rendered)

    def test_authorization_cookie_and_escaped_values_do_not_leak(self):
        cases = (
            "Authorization: Bearer top-secret-token",
            "Authorization: Basic dXNlcjpwYXNz",
            'Authorization: Bearer "topsecret"',
            'Authorization: Digest username="user", response="topsecret"',
            '"authorization": "Bearer topsecret"',
            "Cookie: session=secret-session; csrf=secret-csrf",
            'api-key: "a\\\"secret"',
            "https://example.test/?token=topsecret&status=failed",
            "postgresql://user:topsecret@db/x",
        )
        for message in cases:
            with self.subTest(message=message):
                rendered = diagnostic_text(message)
                for secret in ("top-secret-token", "dXNlcjpwYXNz", "secret-session", "secret-csrf", "secret", "topsecret"):
                    self.assertNotIn(secret, rendered)
