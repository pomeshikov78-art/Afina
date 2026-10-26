"""Тесты ИИ-слоя (AI-001/002/003, MIL-12)."""
import os, sys, json, tempfile, unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from afina.ai import rag


class RagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = rag.RagIndex()
        cls.idx.add_text("ТЗ.АФИНА.11.0", "§4.5.10", "Паспорт расчётного запуска фиксирует ID запуска, версию модели, хэши входных данных, вычислительную среду, четыре компонента неопределённости и допущения.")
        cls.idx.add_text("ТЗ.АФИНА.11.0", "§4.4.2", "Классификатор технического состояния TS-0 не оценено, TS-1 исправно, TS-4 неисправно. Не оценено не равно неисправно.")
        cls.idx.add_text("реестр моделей", "MOD-114", "SPM калиброванный, уровень L1, область применимости 300 МГц – 6 ГГц, дальность до 20 км, статус VALIDATED.")
        cls.idx.add_json_records("каталог", [{"id": "COM-014", "name": "Р-444 Пересвет", "bands": "146-174 МГц", "power_w": 50}])

    def test_answer_has_sources_and_limits(self):
        a = self.idx.answer("что фиксирует паспорт расчёта")
        self.assertEqual(a["status"], "ok")
        self.assertTrue(a["sources"])
        self.assertTrue(a["limitations"])
        self.assertTrue(a["assumptions"])
        self.assertIn("ТЗ.АФИНА.11.0", a["sources"][0]["doc"])

    def test_uncertainty_on_irrelevant(self):
        a = self.idx.answer("квантовая телепортация антарктических пингвинов")
        self.assertIn(a["status"], ("insufficient_data", "ok"))
        if a["status"] == "insufficient_data":
            self.assertIn("запрашиваю данные", a["answer"].lower())

    def test_ai003_disable(self):
        off = rag.RagIndex(enabled=False)
        off.add_text("x", "y", "текст")
        self.assertEqual(off.chunks, [])
        self.assertEqual(off.answer("вопрос")["status"], "disabled")

    def test_tz_extraction(self):
        tz = os.path.join(os.path.dirname(__file__), "..", "..", "Афина_Техническое_задание.md")
        if os.path.exists(tz):
            reqs = rag.extract_requirements_from_tz(tz)
            self.assertGreaterEqual(len(reqs), 80)
            ids = {r["id"] for r in reqs}
            self.assertIn("ENG-001", ids)
            for r in reqs:
                self.assertIn("source", r); self.assertIn("confidence", r)

    def test_anomaly_scan(self):
        rows = [
            {"id": "X-1", "class": "COM", "power_w": 5000, "year_from": 2020, "year_to": None, "status_kind": "эксплуатируется"},
            {"id": "X-2", "class": "COM", "power_w": 50, "year_from": 2020, "year_to": 2019, "status_kind": "опытный"},
            {"id": "X-3", "class": "COM", "power_w": 50, "year_from": 1990, "year_to": 2010, "status_kind": "эксплуатируется"},
        ]
        found = rag.scan_anomalies(rows)
        types = {f["type"] for f in found}
        self.assertIn("подозрительное значение", types)
        self.assertIn("противоречие дат", types)
        self.assertIn("признак EOL", types)

    def test_markdown_index(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("# Раздел 1\nТекст про рельеф и матрицы высот MTW.\n\n## 1.2 Подраздел\nПро зоны видимости и профиль местности.\n")
            p = f.name
        idx = rag.RagIndex()
        n = idx.add_markdown(p)
        self.assertGreaterEqual(n, 1)
        hits = idx.search("профиль местности зоны видимости")
        self.assertTrue(hits)
        os.unlink(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
