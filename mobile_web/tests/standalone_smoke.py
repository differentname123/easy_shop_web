# -*- coding: utf-8 -*-
"""Runtime-only, network-denied smoke test; no pytest/httpx/test fixtures required.

Run ``python tests/standalone_smoke.py``. Only production files are copied to a
new directory outside the checkout, then a fresh isolated Python checks the real
application through ASGI (no listening HTTP server).
"""
import argparse
import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import unittest


PRODUCTION_PACKAGES = ("adapters", "api", "infrastructure", "models", "repositories", "services", "scripts")
SETTINGS_ENV = (
    "PDD_CLIENT_ID", "PDD_CLIENT_SECRET", "PDD_PID", "MONGO_HOST", "MONGO_PORT",
    "MONGO_USER", "MONGO_PASSWORD", "MONGO_DB_NAME", "MONGO_AUTH_SOURCE",
    "WEB_AUTH_SECRET", "WEB_HOST", "WEB_PORT", "PORT", "WEB_CONFIG_FILE",
    "WEB_WARMUP_ENABLED", "WEB_LOG_DIR", "WEB_LOG_LEVEL", "PDD_DNS_MODE", "PDD_TRUST_ENV",
)
SMOKE_SECRET = "standalone-smoke-private-secret-not-for-production-2026"


def copy_production_tree(source, destination):
    """Explicit allowlist: never copy private config, tests, caches or repo parents."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True)
    for path in source.glob("*.py"):
        shutil.copy2(path, destination / path.name)
    for name in PRODUCTION_PACKAGES:
        package = source / name
        if not package.is_dir():
            continue
        for path in package.rglob("*.py"):
            if "__pycache__" not in path.parts:
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    shutil.copytree(source / "static", destination / "static")
    for name in ("requirements.txt", "constraints.txt", "config.example.json"):
        if (source / name).is_file():
            shutil.copy2(source / name, destination / name)
    return destination


def isolated_environment(config_file):
    env = {key: value for key, value in os.environ.items() if key not in SETTINGS_ENV}
    env.update({
        "PYTHONPATH": "", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8", "WEB_CONFIG_FILE": str(config_file),
        "WEB_WARMUP_ENABLED": "false", "WEB_LOG_DIR": "-", "PDD_DNS_MODE": "off",
        "PDD_TRUST_ENV": "false",
    })
    return env


def write_fake_config(path):
    Path(path).write_text(json.dumps({
        "myself_pdd_client_id": "standalone-client", "myself_pdd_client_secret": "standalone-secret",
        "myself_pdd_pid": "standalone-pid", "local_mongo_host": "127.0.0.1",
        "local_mongo_port": 27017, "local_mongo_user": "", "local_mongo_password": "",
        "local_mongo_db_name": "standalone_never_connect", "web_auth_secret": SMOKE_SECRET,
    }), encoding="utf-8")


async def asgi_get(app, path):
    """Exercise middleware, routing and FileResponse using only the ASGI protocol."""
    messages = []
    first = True
    waiting = asyncio.Event()

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await waiting.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode("ascii"), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"smoke.invalid")], "server": ("smoke.invalid", 80),
        "client": ("127.0.0.1", 12345),
    }, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], body


def run_worker(app_dir, forbidden_root):
    app_dir, forbidden_root = Path(app_dir).resolve(), Path(forbidden_root).resolve()
    sys.path.insert(0, str(app_dir))  # Only the standalone deployment, never a repository parent.
    # A deployment-local virtualenv may itself live inside the source checkout.
    # Exempt only this interpreter's exact dependency installation roots, never
    # the virtualenv/checkout as a whole or an editable package's source tree.
    package_dirs = {Path(sysconfig.get_path(name)).resolve() for name in ("purelib", "platlib")}
    application_modules = {"main", "config", "auth", "cache", "cache_utils", "run", *PRODUCTION_PACKAGES}
    # Windows may use a loopback socket pair when creating its event loop. Create
    # that standard-library plumbing before installing the outbound deny guard.
    loop = asyncio.new_event_loop()
    blocked = []
    originals = {name: getattr(socket, name) for name in ("getaddrinfo", "create_connection")}
    originals.update({"connect": socket.socket.connect, "connect_ex": socket.socket.connect_ex,
                      "sendto": socket.socket.sendto})

    def deny(*args, **kwargs):
        blocked.append("outbound socket attempt")
        raise AssertionError("standalone smoke forbids all external sockets")

    socket.getaddrinfo = deny
    socket.create_connection = deny
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.socket.sendto = deny

    class StandaloneApplicationTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.main = importlib.import_module("main")
            cls.config = importlib.import_module("config")
            cls.mongo = importlib.import_module("infrastructure.mongo")
            cls.settings = importlib.import_module("infrastructure.settings")

        def tearDown(self):
            self.assertEqual(blocked, [], "caught socket failures must not hide attempted network access")
            self.assertIs(socket.getaddrinfo, deny)
            self.assertIs(socket.create_connection, deny)
            self.assertIs(socket.socket.connect, deny)
            self.assertIs(socket.socket.connect_ex, deny)
            self.assertIs(socket.socket.sendto, deny)

        def test_01_real_modules_are_local_and_import_is_lazy(self):
            self.assertEqual(sys.flags.utf8_mode, 1, "the isolated worker must explicitly enable UTF-8")
            self.assertEqual(sys.stdout.encoding.lower().replace("_", "-"), "utf-8")
            for module in (self.main, self.config, self.mongo, self.settings):
                self.assertTrue(Path(module.__file__).resolve().is_relative_to(app_dir), module.__name__)
            self.assertEqual(self.config.PDD_CLIENT_ID, "standalone-client")
            self.assertEqual(self.config.MONGO_DB_NAME, "standalone_never_connect")
            self.assertEqual(self.config.AUTH_SECRET, SMOKE_SECRET)
            self.assertFalse(self.config.settings.warmup_enabled)
            self.assertIsNone(self.config.settings.log_dir)
            self.assertFalse(any(isinstance(handler, logging.FileHandler) for handler in self.config.logger.handlers))
            self.assertFalse(list(app_dir.rglob("*.log")), "imports must not initialize file logging")
            for name, module in tuple(sys.modules.items()):
                if name == "__main__":
                    continue
                filename = getattr(module, "__file__", None)
                if filename:
                    origin = Path(filename).resolve()
                    if name.partition(".")[0] in application_modules:
                        self.assertTrue(origin.is_relative_to(app_dir), name)
                    elif origin.is_relative_to(forbidden_root):
                        self.assertTrue(any(origin.is_relative_to(directory) for directory in package_dirs), name)
            for name in ("tests.conftest", "tests.offline_support", "tests.preview_app"):
                self.assertNotIn(name, sys.modules)
            self.assertFalse(any(name == "common" or name.startswith("common.") or
                                 name == "app.mobile_web" or name.startswith("app.mobile_web.")
                                 for name in sys.modules))
            for path in sys.path:
                if path:
                    location = Path(path).resolve()
                    if location.is_relative_to(forbidden_root):
                        # Windows also puts the interpreter's own prefix on
                        # sys.path. It is not a project source directory; module
                        # origins above still allow only purelib/platlib there.
                        self.assertTrue(location == Path(sys.prefix).resolve() or
                                        any(location.is_relative_to(directory) for directory in package_dirs), path)
            self.assertIs(socket.getaddrinfo, deny, "application imports must not replace process-wide DNS")
            self.assertIs(socket.create_connection, deny)
            self.assertIs(socket.socket.connect, deny)
            self.assertIs(socket.socket.connect_ex, deny)

        def test_02_lifespan_health_and_real_static_content(self):
            async def exercise():
                async with self.main.app.router.lifespan_context(self.main.app):
                    status, body = await asgi_get(self.main.app, "/health")
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body), {"status": "ok", "service": "mobile_web_service"})
                    for route, filename in (("/", "index.html"), ("/detail", "detail.html"),
                                            ("/static/css/style.css", "css/style.css"),
                                            ("/static/js/detail.js", "js/detail.js")):
                        status, body = await asgi_get(self.main.app, route)
                        self.assertEqual(status, 200, route)
                        self.assertEqual(body, (app_dir / "static" / filename).read_bytes(), route)
                    status, _ = await asgi_get(self.main.app, "/test/offline-status")
                    self.assertEqual(status, 404)
            loop.run_until_complete(exercise())
            for module_name, service_name in (("api.home", "product_service"), ("api.goods", "product_service"),
                                              ("api.user", "user_service"), ("api.order", "order_service"),
                                              ("api.promote", "promote_service")):
                service = getattr(importlib.import_module(module_name), service_name)
                self.assertIsNone(service.repo._db, "health/static startup must not construct a Mongo client")
            self.assertFalse(list(app_dir.rglob("*.log")))

        def test_03_bad_config_errors_do_not_expose_secret_values(self):
            path = app_dir.parent / "bad smoke config.json"
            path.write_text(json.dumps({"local_mongo_port": "secret-port-canary", "web_auth_secret": SMOKE_SECRET}), encoding="utf-8")
            with self.assertRaises(self.settings.SettingsError) as error:
                self.settings.load_settings(path, environ={}, require_credentials=False)
            self.assertNotIn("secret-port-canary", str(error.exception))
            self.assertNotIn(SMOKE_SECRET, str(error.exception))
            path.write_text('{"password": "json-secret-canary",', encoding="utf-8")
            with self.assertRaises(self.settings.SettingsError) as error:
                self.settings.load_settings(path, environ={})
            self.assertNotIn("json-secret-canary", str(error.exception))

        def test_04_logging_is_idempotent_and_utf8(self):
            from dataclasses import replace
            from logging.handlers import BaseRotatingHandler
            from infrastructure.logging_utils import configure_logging
            log_dir = app_dir.parent / "temporary logs 日志"
            configured = replace(self.config.settings, log_dir=log_dir, log_level="INFO")
            logger = logging.getLogger("mobile_web")
            try:
                configure_logging(configured)
                first_handlers = tuple(logger.handlers)
                configure_logging(configured)
                self.assertEqual(tuple(logger.handlers), first_handlers)
                files = [handler for handler in logger.handlers if isinstance(handler, BaseRotatingHandler)]
                self.assertEqual(len(files), 1)
                self.assertTrue(Path(files[0].baseFilename).resolve().is_relative_to(log_dir))
                self.assertGreater(getattr(files[0], "maxBytes", 0) or getattr(files[0], "interval", 0), 0)
                self.assertGreater(files[0].backupCount, 0)
                self.assertTrue(any(not isinstance(handler, logging.FileHandler) and
                                    isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout
                                    for handler in logger.handlers))
                logger.info("独立烟测 UTF-8 logging")
                files[0].flush()
                self.assertIn("独立烟测 UTF-8 logging", Path(files[0].baseFilename).read_text(encoding="utf-8"))
            finally:
                configure_logging(self.config.settings)
            self.assertFalse(any(isinstance(handler, logging.FileHandler) for handler in logger.handlers))

    try:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StandaloneApplicationTests))
        if result.wasSuccessful():
            print("STANDALONE_SMOKE_OK: real application, isolated production copy, no network")
        return 0 if result.wasSuccessful() else 1
    finally:
        socket.getaddrinfo = originals["getaddrinfo"]
        socket.create_connection = originals["create_connection"]
        socket.socket.connect = originals["connect"]
        socket.socket.connect_ex = originals["connect_ex"]
        socket.socket.sendto = originals["sendto"]
        loop.close()


def source_boundary(source):
    """Use a real checkout boundary, or just the uploaded application directory."""
    source = Path(source).resolve()
    return next((directory for directory in (source, *source.parents)
                 if (directory / ".git").exists()), source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--forbidden-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return run_worker(args.app_dir, args.forbidden_root)
    source = args.app_dir.resolve()
    forbidden_root = source_boundary(source)
    with tempfile.TemporaryDirectory(prefix="mobile-web standalone 中文 ") as temporary:
        temporary = Path(temporary)
        if temporary.resolve().is_relative_to(forbidden_root):
            raise RuntimeError("standalone smoke requires a temporary directory outside the checkout")
        deployment = copy_production_tree(source, temporary / "deployment 独立 copy")
        config_file = temporary / "smoke private config.json"
        write_fake_config(config_file)
        cwd = temporary / "unrelated working directory"
        cwd.mkdir()
        result = subprocess.run([
            sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--worker", "--app-dir", str(deployment),
            "--forbidden-root", str(forbidden_root),
        ], cwd=cwd, env=isolated_environment(config_file), check=False, timeout=90)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
