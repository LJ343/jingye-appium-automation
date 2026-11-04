"""Real candidate AST functions, local fixtures and fake ADB only."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_pdf_transfer_integrity import FakeAdb, PDF, namespace


class PdfLongPathTest(unittest.TestCase):
    def test_short_windows_path_is_unchanged(self):
        ns = namespace(FakeAdb())
        with patch.object(os, "name", "nt"):
            self.assertEqual(ns["_pdf_local_io_path"](r"C:\short\old.pdf"), r"C:\short\old.pdf")

    def test_drive_path_is_absolute_normalized_and_extended(self):
        ns = namespace(FakeAdb())
        path = "C:\\fixtures\\" + "d"*240 + "\\..\\" + "e"*240 + "\\file.pdf"
        with patch.object(os, "name", "nt"):
            actual = ns["_pdf_local_io_path"](path)
        self.assertTrue(actual.startswith("\\\\?\\C:\\fixtures\\"))
        self.assertNotIn("\\..\\", actual)

    def test_unc_and_existing_extended_paths(self):
        ns = namespace(FakeAdb())
        path = "\\\\server\\share\\" + "d"*240 + "\\file.pdf"
        with patch.object(os, "name", "nt"):
            actual = ns["_pdf_local_io_path"](path)
            self.assertEqual(actual, "\\\\?\\UNC\\" + path[2:])
            self.assertEqual(ns["_pdf_local_io_path"](actual), actual)

    def test_non_windows_path_unchanged(self):
        ns = namespace(FakeAdb())
        path = "/tmp/" + "d"*270 + "/file.pdf"
        with patch.object(os, "name", "posix"):
            self.assertEqual(ns["_pdf_local_io_path"](path), path)

    @unittest.skipUnless(os.name == "nt", "Actual Win32 long-file I/O boundary")
    def test_real_long_pull_sidecar_manifest_and_valid_reuse(self):
        self._actual_long_path(False)

    @unittest.skipUnless(os.name == "nt", "Actual Win32 long-file I/O boundary")
    def test_equal_size_corrupt_long_part_is_rejected(self):
        self._actual_long_path(True)

    def _actual_long_path(self, corrupt):
        adb = FakeAdb("equal_length_corruption" if corrupt else "ok")
        ns = namespace(adb)
        local = ns["_pdf_local_io_path"]
        with tempfile.TemporaryDirectory() as temp:
            parent = os.path.join(temp, "d"*(265-len(temp)-1))
            os.mkdir(local(parent))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = ns["pull_phone_pdf"]("/synthetic/fixture.pdf", "WB-PROBE", "CAR-PROBE", parent, "1")
                self.assertEqual(result, not corrupt)
                names = os.listdir(local(parent))
                if corrupt:
                    self.assertEqual(names, [])
                    return
                logical = os.path.join(parent, next(name for name in names if name.endswith(".pdf")))
                self.assertGreater(len(logical+".integrity.json.part"), 260)
                self.assertEqual(Path(local(logical)).read_bytes(), PDF)
                pull = next(command for command, options in adb.calls if command[3] == "pull")
                self.assertTrue(pull[-1].startswith("\\\\?\\"))
                before = len(adb.calls)
                self.assertEqual(ns["find_existing_archived_pdf"](parent, "WB-PROBE", "CAR-PROBE", "1"), logical)
                self.assertEqual(len(adb.calls), before)
                ns["register_expected_pdf_slots"](parent, "WB-PROBE", "CAR-PROBE", 1, indexed=True)
                ns["complete_expected_pdf_slots"](parent, "WB-PROBE", "CAR-PROBE")
                ns["assert_all_pdf_batches_complete"]([parent])
                with open(local(logical+".integrity.json"), encoding="utf-8") as stream:
                    self.assertEqual(json.load(stream)["verification"], "adb-sha256-v1")
            finally:
                for name in os.listdir(local(parent)):
                    os.remove(local(os.path.join(parent, name)))
                os.rmdir(local(parent))

    def test_safe_error_diagnostic_contains_codes_and_lengths_not_paths(self):
        adb = FakeAdb()
        ns = namespace(adb)
        def missing(*args, **kwargs):
            raise FileNotFoundError(2, "private-secret-path-must-not-escape", "PRIVATE-FILENAME")
        with tempfile.TemporaryDirectory() as temp, patch.object(adb, "run", side_effect=missing), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(ns["pull_phone_pdf"]("/private/remote", "WB", "CAR", temp))
        text = output.getvalue()
        self.assertIn('"errno":2', text)
        self.assertIn('"metadataChars":', text)
        self.assertNotIn("private-secret", text)
        self.assertNotIn("PRIVATE-FILENAME", text)
        self.assertNotIn("/private/remote", text)


if __name__ == "__main__":unittest.main()
