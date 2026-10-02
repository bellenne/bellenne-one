"""Package a narrowly scoped MV3 extension, sharing the server DOM parser.

python apps/parity/extension/build.py [--origin https://bellenne.example]
Multiple --origin flags are permitted; default is the local gateway.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile, ZIP_DEFLATED


def build(origins=None):
    source = Path(__file__).resolve().parent
    root = source.parent
    origins = origins or ["http://localhost:17863", "http://127.0.0.1:17863"]
    matches = []
    for origin in origins:
        u = urlsplit(origin)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError("An origin must contain only scheme, hostname and optional port")
        if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("Remote Parity deployments require HTTPS")
        matches.append(f"{u.scheme}://{u.hostname}/*")
    matches = list(dict.fromkeys(matches))
    markets = ["https://www.wildberries.ru/*", "https://wildberries.ru/*", "https://www.ozon.ru/*", "https://ozon.ru/*"]
    manifest = {
        "manifest_version": 3, "name": "BellenneParity", "version": "1.2.2", "minimum_chrome_version": "120",
        "description": "Читает цены WB Кошелька и Ozon Карты в вашем браузере по заданиям BellenneParity.",
        "permissions": ["storage", "alarms"], "host_permissions": matches + markets,
        "background": {"service_worker": "worker.js"}, "action": {"default_title": "Прогресс BellenneParity", "default_popup": "popup.html"},
        "content_scripts": [
            {"matches": matches, "js": ["config.js", "core.js", "bridge.js"], "run_at": "document_idle"},
            {"matches": markets, "js": ["config.js", "core.js", "storefront-extract.js", "card.js"], "run_at": "document_idle"},
        ],
        "content_security_policy": {"extension_pages": "script-src 'self'; object-src 'none'"}
    }
    files = {p.name: p.read_bytes() for p in source.glob("*.js")}
    for name in ("popup.html", "popup.css"):
        files[name] = (source / name).read_bytes()
    for name in ("theme.css", "app.css"):
        local = root / "app/static" / name
        shared = local if local.exists() else source.parents[2] / "apps/pulse/app/static/css" / name
        files[name] = shared.read_bytes()
    files["parity.css"] = (root / "app/static/parity.css").read_bytes()
    files["manifest.json"] = json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    files["config.js"] = ("globalThis.PARITY_ORIGINS = " + json.dumps([s.rstrip('/') for s in origins]) + ";\nglobalThis.PARITY_PREFIX = '/parity';\n").encode()
    files["storefront-extract.js"] = (root / "app/static/storefront-extract.js").read_bytes()
    files["README.txt"] = (source / "README.txt").read_bytes()
    target = root / "app/static/bellenne-parity-extension.zip"
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name, contents in sorted(files.items()):
            archive.writestr("bellenne-parity-extension/" + name, contents)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", action="append")
    print(build(parser.parse_args().origin))
