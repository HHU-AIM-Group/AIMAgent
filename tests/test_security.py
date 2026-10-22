"""External resource security regressions; no network or user database writes."""
import io
from pathlib import Path
import socket
import sys
import unittest
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sources


class ExternalResourceBoundary(unittest.TestCase):
    def test_mixed_public_private_dns_results_are_rejected(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0)),
        ]
        with patch.object(sources.socket, 'getaddrinfo', return_value=addresses):
            with self.assertRaises(ValueError):
                sources.validate_public_url('https://public-looking.example/paper')

    def test_fake_ip_exception_is_exact_publisher_host_only(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.27',443))]
        with patch.object(sources.socket, 'getaddrinfo', return_value=addresses):
            self.assertEqual(sources.validate_public_url('https://arxiv.org/html/2505.18022'), 'https://arxiv.org/html/2505.18022')
            for host in ['custom.example', 'arxiv.org.evil.example', 'evil-arxiv.org', 'sub.arxiv.org']:
                with self.subTest(host=host), self.assertRaises(ValueError):
                    sources.validate_public_url('https://'+host+'/paper')

    def test_publisher_name_never_allows_loopback_or_rfc1918(self):
        for address in ['127.0.0.1','10.0.0.1','192.168.1.2','172.16.0.1','169.254.169.254','::1']:
            rows=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address,443))]
            with self.subTest(address=address), patch.object(sources.socket,'getaddrinfo',return_value=rows), self.assertRaises(ValueError):
                sources.validate_public_url('https://arxiv.org/paper')

    def test_redirect_target_revalidated_for_private_network_and_credentials(self):
        redirect = sources.SafeRedirect()
        request = urllib.request.Request('https://example.com/paper')
        for target in ['https://127.0.0.1/private', 'https://[::1]/private',
                       'https://user:secret@example.com/private', 'http://example.com/private']:
            with self.subTest(target=target), self.assertRaises(ValueError):
                redirect.redirect_request(request, io.BytesIO(), 302, 'Found', {}, target)

    def test_cached_body_does_not_bypass_url_validation(self):
        store = Mock()
        store.cache_get.return_value = b'cached external text'
        fetch = sources.Fetcher(store)
        fetch.opener = Mock()
        with self.assertRaises(ValueError):
            fetch.get('https://127.0.0.1/paper')
        store.cache_get.assert_not_called()
        fetch.opener.open.assert_not_called()

    def test_oversize_response_is_not_cached(self):
        store = Mock()
        store.cache_get.return_value = None
        fetch = sources.Fetcher(store)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'x' * 17
        fetch.opener = Mock()
        fetch.opener.open.return_value = response
        with patch.object(sources, 'validate_public_url', side_effect=lambda url: url), patch.object(sources, 'MAX_RESPONSE', 16):
            with self.assertRaisesRegex(ValueError, 'limit'):
                fetch.get('https://example.com/paper')
        store.cache_set.assert_not_called()

    def test_invalid_json_is_evicted_instead_of_retained_as_valid_evidence(self):
        store = Mock()
        fetch = sources.Fetcher(store)
        fetch.get = Mock(return_value=b'<html>Verifying your browser</html>')
        with self.assertRaisesRegex(RuntimeError, '验证'):
            fetch.json('https://example.com/paper')
        store.cache_delete.assert_called_once_with('https://example.com/paper')


if __name__ == '__main__':
    unittest.main()
