import builtins
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from cf_submit.cli import confirm
from cf_submit.config import Competition, Problemset, Store
from cf_submit.exceptions import CacheNotFound, ConfigError, FileNotFound, InvalidFilename, LanguageNotFound
from cf_submit.language import choose_language
from cf_submit.problem import Source, problem_index


class ProblemTests(unittest.TestCase):
    def test_infer_strict_filenames(self):
        for filename, index in (("A.cpp", "A"), ("B1.cpp", "B1"), ("b2.cpp", "B2")):
            with self.subTest(filename=filename):
                self.assertEqual(problem_index(Path("dir", filename), None), index)

    def test_override_and_invalid_filenames(self):
        for filename in ("main.cpp", "AA.cpp", "A.cpp.bak", "A.CPP", "あ.cpp", "A.cpp\n"):
            with self.subTest(filename=filename), self.assertRaises(InvalidFilename):
                problem_index(Path(filename), None)
        self.assertEqual(problem_index(Path("main.cpp"), "b2"), "B2")
        with self.assertRaises(InvalidFilename):
            problem_index(Path("A.txt"), "A")

    def test_validate_file_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "A.cpp")
            with self.assertRaises(FileNotFound):
                Source.read(path)
            path.write_bytes(b" ")
            with self.assertRaises(InvalidFilename):
                Source.read(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(InvalidFilename):
                Source.read(path)
            path.write_bytes(b"int main() {}\r\n")
            source = Source.read(path)
            self.assertEqual(source.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
            source.verify_unchanged()
            path.write_bytes(b"int main() {}\n")
            with self.assertRaisesRegex(InvalidFilename, "changed"):
                source.verify_unchanged()


class ConfigTests(unittest.TestCase):
    def test_cache_survives_switch_and_new_process_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "config")
            contest = Competition("contest", 4)
            gym = Competition("gym", 104172)
            store = Store(root)
            with self.assertRaises(ConfigError):
                store.current()
            with self.assertRaises(CacheNotFound):
                store.problemset(contest)
            store.save_competition(contest, Problemset("Contest", {"A": "Watermelon"}))
            store.save_competition(gym, Problemset("Gym", {"A": "TreeScript"}))
            restored = Store(root)
            self.assertEqual(restored.current(), gym)
            self.assertEqual(restored.problemset(contest).problems, {"A": "Watermelon"})
            self.assertEqual(restored.problemset(gym).problems, {"A": "TreeScript"})
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(ConfigError):
                restored.save_competition(contest, Problemset("Contest", {"A": "New name"}), refresh=True)
            self.assertEqual(restored.problemset(contest).problems["A"], "Watermelon")

    def test_invalid_competition(self):
        for args in (("group", 4), ("gym", 0), ("contest", True), ("contest", 4, "C++20")):
            with self.subTest(args=args), self.assertRaises(ConfigError):
                Competition(*args)


class LanguageTests(unittest.TestCase):
    def test_uses_id_from_html_and_current_name(self):
        for label in ("GNU G++23 14.2 (64 bit, msys2)", "C++23 (GCC 14-64, msys2)"):
            with self.subTest(label=label):
                page = BeautifulSoup(f"""
                    <select><option value="15">GNU G++20</option>
                    <optgroup disabled><option value="44">GNU G++23</option></optgroup>
                    <option value="237">{label}</option></select>
                """, "html.parser")
                self.assertEqual(choose_language(page.find("select")), ("237", label))

    def test_rejects_missing_or_ambiguous_versions(self):
        for options in (
            '<option value="1">Clang C++23</option><option value="2">GNU G++20</option>',
            '<option value="1">GNU G++23</option><option value="2">C++23 (GCC 14)</option>',
            '<option value="1">GNU G++23 Diagnostics</option>',
        ):
            with self.subTest(options=options), self.assertRaises(LanguageNotFound):
                choose_language(BeautifulSoup(f"<select>{options}</select>", "html.parser").find("select"))


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_rules(self):
        for responses, expected in ((["Y"], True), (["y"], True), (["N"], False),
                                    (["n"], False), ([""], False), (["?", "y"], True)):
            with self.subTest(responses=responses), patch.object(builtins, "input", side_effect=responses):
                self.assertEqual(confirm(), expected)
