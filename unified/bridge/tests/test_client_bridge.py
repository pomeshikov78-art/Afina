"""Статические тесты postMessage-моста клиента платформы (UNIF-21, правка линии A №3).

Браузер в песочнице недоступен — проверяется контракт и безопасность по исходнику:
  * маркеры контракта: afina:ready / afina:scenario-request / afina:scenario;
  * токен сессии НЕ передаётся через postMessage (скан блока моста);
  * JS всего клиента синтаксически валиден (node --check) — регрессионный барьер
    на дефект класса OBS-12 (синтаксическая ошибка обнуляла весь скрипт клиента).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(BRIDGE))
CLIENT = os.path.join(REPO, "afina", "src", "client", "index.html")
HAS_NODE = shutil.which("node") is not None


class TestClientBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = open(CLIENT, encoding="utf-8").read()

    def test_contract_markers_present(self):
        for marker in ("afina:ready", "afina:scenario-request", "afina:scenario",
                       "UNIFIED_BRIDGE_VERSION", "unifiedSendScenario"):
            self.assertIn(marker, self.html, f"маркер контракта отсутствует: {marker}")

    def test_token_never_crosses_the_bridge(self):
        # изолируем блок моста и проверяем: ЗНАЧЕНИЕ токена не передаётся.
        # Булево использование (!!TOKEN — флаг сессии, if(TOKEN) — охрана) допустимо;
        # запрещены: присваивание токена полем сообщения и его сериализация.
        m = re.search(r"/\* -+ Мост единого рабочего места.*?\n(?=/\*|</script>)",
                      self.html, re.S)
        self.assertIsNotNone(m, "блок моста не найден")
        block = m.group(0)
        self.assertIsNone(re.search(r"[:=]\s*TOKEN\b", block),
                          "TOKEN присваивается полю сообщения — утечка сессии")
        for bad in ("JSON.stringify(TOKEN", "password", "Authorization", "token:"):
            self.assertNotIn(bad, block)
        # payload содержит только роль/логин для отображения
        self.assertIn("me: ME ? {login: ME.login, role: ME.role} : null", block)

    def test_embedded_guard(self):
        # мост активен только во фрейме (window.parent !== window)
        self.assertIn("unifiedIsEmbedded", self.html)
        self.assertIn("window.parent !== window", self.html)

    @unittest.skipUnless(HAS_NODE, "node недоступен")
    def test_client_js_syntactically_valid(self):
        # регрессия OBS-12: синтаксическая ошибка в любом месте скрипта
        # обнуляла ВСЁ дерево функций клиента (браузерные смоуки HTTP этого не ловят)
        scripts = re.findall(r"<script>(.*?)</script>", self.html, re.S)
        self.assertTrue(scripts, "в клиенте нет <script> блоков")
        with tempfile.TemporaryDirectory() as td:
            for i, sc in enumerate(scripts):
                p = os.path.join(td, f"client_{i}.js")
                open(p, "w", encoding="utf-8").write(sc)
                r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0,
                                 f"скрипт #{i} синтаксически невалиден: {r.stderr[:300]}")


if __name__ == "__main__":
    unittest.main()
