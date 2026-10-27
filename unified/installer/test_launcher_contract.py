"""installer/test_launcher_contract.py — контракт-тесты загрузчика (регрессия OBS-14).

Класс дефекта: run_unified.pyw вызывал create_server(queue_path=…) — параметра
нет в сигнатуре фабрики линии A; в Linux-песочнице это не ловилось (verify_install
стартует платформу через CLI, а не через загрузчик), проявилось только на
Windows-стенде. Тесты ниже сверяют ЗАГРУЗЧИК с РЕАЛЬНЫМИ сигнатурами/путями,
не запуская Windows-код.

Запуск: python3 test_launcher_contract.py (из любого каталога; нужен только репозиторий).
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
UNIFIED = os.path.dirname(HERE)
REPO = os.path.dirname(UNIFIED)
LAUNCHER = os.path.join(HERE, "run_unified.pyw")
sys.path.insert(0, os.path.join(REPO, "afina", "src"))


class TestLauncherContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(LAUNCHER, encoding="utf-8").read()
        cls.tree = ast.parse(cls.src)

    def _calls(self, func_name: str):
        out = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name == func_name:
                    out.append(node)
        return out

    def test_create_server_kwargs_match_signature(self):
        """OBS-14: kwargs загрузчика ⊆ фактическая сигнатура create_server()."""
        from afina.server.app import create_server
        allowed = set(inspect.signature(create_server).parameters)
        calls = self._calls("create_server")
        self.assertTrue(calls, "загрузчик не вызывает create_server")
        for c in calls:
            used = {kw.arg for kw in c.keywords if kw.arg}
            extra = used - allowed
            self.assertFalse(extra,
                             f"create_server вызван с несуществующими параметрами: {sorted(extra)} "
                             f"(сигнатура: {sorted(allowed)}) — регрессия OBS-14")

    def test_seed_build_exists(self):
        from afina.catalog import seed
        self.assertTrue(callable(getattr(seed, "build", None)), "afina.catalog.seed.build исчез")

    def test_engine_entrypoint_path(self):
        """Popen-команда движка: api/main.mjs существует относительно eng6-service."""
        self.assertIn('"api/main.mjs"', self.src)
        self.assertTrue(os.path.isfile(os.path.join(REPO, "eng6-service", "api", "main.mjs")))

    def test_no_orphan_engine_on_failure_paths(self):
        """Каждый fail() ПОСЛЕ старта движка обязан предваряться engine.terminate()
        (OBS-14: сирота node.exe на 8080 после падения платформы)."""
        # грубая, но надёжная проверка по тексту: между запуском engine = subprocess.Popen
        # и концом main() каждый вызов fail(...) должен находиться в блоке, где выше
        # по тому же уровню вложенности есть engine.terminate()
        start = self.src.index("engine = subprocess.Popen")
        body = self.src[start:]
        for m in re.finditer(r"fail\(", body):
            prefix = body[:m.start()]
            self.assertIn("engine.terminate()", prefix,
                          "найден fail() после старта движка без предварительного engine.terminate()")

    def test_pidfile_written_before_platform_start(self):
        """pid-файл пишется СРАЗУ после старта движка (орфана можно убить «Остановить»)."""
        i_pid = self.src.index("write_pid(platform=\"starting\")")
        i_srv = self.src.index("create_server(")
        self.assertLess(i_pid, i_srv, "pid-файл должен писаться до create_server")


if __name__ == "__main__":
    unittest.main()
