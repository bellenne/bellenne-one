import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from extension.build import build


class ExtensionPackageTests(unittest.TestCase):
    def test_default_package_supports_production_and_local_gateways(self):
        with TemporaryDirectory() as directory:
            target = build(target=Path(directory)/"extension.zip")
            with ZipFile(target) as archive:
                prefix = "bellenne-parity-extension/"
                manifest = json.loads(archive.read(prefix+"manifest.json"))
                config = archive.read(prefix+"config.js").decode()
                self.assertEqual(manifest["version"], "1.2.3")
                self.assertEqual(manifest["permissions"], ["storage", "alarms"])
                self.assertEqual(manifest["content_scripts"][0]["matches"], [
                    "https://one.customcraft-mes.ru/*", "http://localhost/*", "http://127.0.0.1/*"])
                self.assertNotIn("<all_urls>", manifest["host_permissions"])
                self.assertIn('PARITY_ORIGINS = ["https://one.customcraft-mes.ru",', config)
                self.assertIn("https://one.customcraft-mes.ru/parity/integrations", archive.read(prefix+"README.txt").decode())
                self.assertIsNone(archive.testzip())

    def test_explicit_origin_replaces_defaults_and_keeps_exact_port(self):
        with TemporaryDirectory() as directory:
            target = build(["https://another.example:8443/"], target=Path(directory)/"custom.zip")
            with ZipFile(target) as archive:
                manifest = json.loads(archive.read("bellenne-parity-extension/manifest.json"))
                config = archive.read("bellenne-parity-extension/config.js").decode()
                self.assertEqual(manifest["content_scripts"][0]["matches"], ["https://another.example/*"])
                self.assertIn('["https://another.example:8443"]', config)
                self.assertNotIn("one.customcraft-mes.ru", config)

    def test_unsafe_origins_are_rejected_without_writing_package(self):
        with TemporaryDirectory() as directory:
            for origin in ("http://one.customcraft-mes.ru", "https://one.customcraft-mes.ru/parity", "https://user:secret@one.customcraft-mes.ru", "https://one.customcraft-mes.ru?host=evil", "https://one.customcraft-mes.ru#x"):
                target = Path(directory)/"invalid.zip"
                with self.subTest(origin=origin), self.assertRaises(ValueError):
                    build([origin], target=target)
                self.assertFalse(target.exists())
