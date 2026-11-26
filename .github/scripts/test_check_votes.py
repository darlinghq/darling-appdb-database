import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

import check_votes
from check_votes import check_votes_text


class CheckVotesTextTests(unittest.TestCase):
    def test_zero_byte_file_is_valid(self):
        self.assertEqual(check_votes_text(""), [])

    def test_well_formed_file_is_valid(self):
        self.assertEqual(check_votes_text("ann\nbob\n"), [])

    def test_missing_trailing_newline_flagged(self):
        self.assertEqual(len(check_votes_text("ann\nbob")), 1)

    def test_unsorted_flagged(self):
        self.assertEqual(len(check_votes_text("bob\nann\n")), 1)

    def test_duplicate_flagged(self):
        self.assertEqual(len(check_votes_text("ann\nann\n")), 1)

    def test_blank_line_flagged(self):
        self.assertEqual(len(check_votes_text("ann\n\nbob\n")), 1)

    def test_invalid_login_flagged(self):
        self.assertEqual(len(check_votes_text("bad_name\n")), 1)

    def test_uppercase_and_edge_hyphens_flagged(self):
        self.assertEqual(len(check_votes_text("-ab\n")), 1)
        self.assertEqual(len(check_votes_text("Ann\n")), 1)

    def test_single_char_and_max_length_valid(self):
        self.assertEqual(check_votes_text("a\n" + "b" * 39 + "\n"), [])
        self.assertEqual(len(check_votes_text("b" * 40 + "\n")), 1)


class MainTests(unittest.TestCase):
    def run_main(self, files):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel, text in files.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            out = io.StringIO()
            old = os.getcwd()
            os.chdir(root)
            try:
                with contextlib.redirect_stdout(out):
                    code = check_votes.main(root)
            finally:
                os.chdir(old)
            return code, out.getvalue()

    def test_missing_directory_exits_zero(self):
        self.assertEqual(self.run_main({}), (0, ""))

    def test_clean_files_exit_zero(self):
        code, out = self.run_main({"data/wishlist/a/votes.txt": "ann\n"})
        self.assertEqual((code, out), (0, ""))

    def test_problem_printed_with_relative_path_and_exit_one(self):
        code, out = self.run_main({"data/wishlist/a/votes.txt": "bob\nann\n"})
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("data/wishlist/a/votes.txt: "))

    def test_symlinked_votes_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            root = Path(d)
            target = Path(outside) / "votes.txt"
            target.write_text("bob\nann\n")  # would be flagged as unsorted
            entry = root / "data" / "wishlist" / "a" / "_" / "a"
            entry.mkdir(parents=True)
            (entry / "votes.txt").symlink_to(target)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = check_votes.main(root)
            self.assertEqual((code, out.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
