"""The cold tool downloader must use only its isolated proxy route."""

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from repository_checks import wheelhouse_tool_fetch as fetch
from repository_checks.wheelhouse_tool_asset import TransientToolDownload


class WheelhouseToolFetchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'output'
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        self.addCleanup(os.close, self.fd)

    def test_explicit_proxy_ignores_environment_bypass(self):
        request = urllib.request.Request('https://example.invalid/tool')
        handler = fetch._RequiredProxy({'https': 'http://127.0.0.1:12345'})
        with patch('urllib.request.proxy_bypass', return_value=True):
            handler.proxy_open(request, 'http://127.0.0.1:12345', 'https')
        self.assertEqual(request.host, '127.0.0.1:12345')
        self.assertEqual(request._tunnel_host, 'example.invalid')

    def test_fetch_streams_from_explicit_relay(self):
        response = io.BytesIO(b'qualified artifact')
        with (patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:12345', 'NO_PROXY': '*'}),
              patch('urllib.request.build_opener') as make_opener):
            make_opener.return_value.open.return_value = response
            fetch.download(self.fd, -1, 'system')
        self.assertEqual(self.path.read_bytes(), b'qualified artifact')
        self.assertEqual(make_opener.call_args.args[0].proxies, {'https': 'http://127.0.0.1:12345'})

    def test_missing_or_nonloopback_proxy_is_refused_before_network(self):
        for value in ('', 'http://other:123', 'http://127.0.0.1:65536', 'https://127.0.0.1:1'):
            with self.subTest(value=value), patch.dict(os.environ, {'HTTPS_PROXY': value}), \
                 patch('urllib.request.build_opener') as make_opener:
                with self.assertRaisesRegex(ValueError, 'relay address'):
                    fetch.download(self.fd, -1, 'system')
                make_opener.assert_not_called()

    def test_retryable_http_status_respects_retry_after(self):
        error = urllib.error.HTTPError('https://private.invalid', 429, 'private', {'Retry-After': '5'}, None)
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:12345'}), \
             patch('urllib.request.build_opener') as make_opener:
            make_opener.return_value.open.side_effect = error
            with self.assertRaises(TransientToolDownload) as caught:
                fetch.download(self.fd, -1, 'system')
        self.assertEqual(caught.exception.retry_after, 5)
        self.assertNotIn('private', str(caught.exception))

    def test_redirect_must_remain_https(self):
        request = urllib.request.Request('https://example.invalid/tool')
        with self.assertRaisesRegex(ValueError, 'must use HTTPS'):
            fetch._HTTPSRedirects().redirect_request(request, None, 302, '', {}, 'http://example.invalid/tool')


if __name__ == '__main__':
    unittest.main()
