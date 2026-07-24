from __future__ import annotations

import os
import ssl
import unittest
import unittest.mock as mock

os.environ.setdefault("ALERTAS_DT_SKIP_DOTENV", "1")

from dt_alerts import tls


class UrllibTLSOpenerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tls._urllib_opener_installed = False
        tls._urllib_backend_info = None

    def tearDown(self) -> None:
        tls._urllib_opener_installed = False
        tls._urllib_backend_info = None

    def test_installs_secure_https_handler_once(self) -> None:
        context = ssl.create_default_context()
        info = tls.TLSBackendInfo(
            backend=tls.BACKEND_STANDARD,
            os_name="Windows",
            ca_bundle_configured=False,
            ca_bundle_label="",
            error=None,
        )
        handler = object()
        opener = object()

        with (
            mock.patch.object(tls, "build_ssl_context", return_value=(context, info)) as build_context,
            mock.patch.object(tls.urllib.request, "HTTPSHandler", return_value=handler) as https_handler,
            mock.patch.object(tls.urllib.request, "build_opener", return_value=opener) as build_opener,
            mock.patch.object(tls.urllib.request, "install_opener") as install_opener,
        ):
            first = tls.install_urllib_https_opener()
            second = tls.install_urllib_https_opener()

        self.assertIs(first, info)
        self.assertIs(second, info)
        build_context.assert_called_once_with()
        https_handler.assert_called_once_with(context=context)
        build_opener.assert_called_once_with(handler)
        install_opener.assert_called_once_with(opener)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_invalid_ca_bundle_fails_closed(self) -> None:
        context = ssl.create_default_context()
        info = tls.TLSBackendInfo(
            backend=tls.BACKEND_STANDARD,
            os_name="Windows",
            ca_bundle_configured=True,
            ca_bundle_label="corporativa.pem",
            error="TLS_CA_BUNDLE inválido",
        )

        with (
            mock.patch.object(tls, "build_ssl_context", return_value=(context, info)),
            mock.patch.object(tls.urllib.request, "install_opener") as install_opener,
        ):
            with self.assertRaisesRegex(RuntimeError, "TLS_CA_BUNDLE inválido"):
                tls.install_urllib_https_opener()

        install_opener.assert_not_called()
        self.assertFalse(tls._urllib_opener_installed)

    def test_build_context_never_disables_verification(self) -> None:
        with mock.patch.dict(os.environ, {"TLS_CA_BUNDLE": ""}, clear=False):
            context, info = tls.build_ssl_context()

        self.assertIsNone(info.error)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


if __name__ == "__main__":
    unittest.main()
