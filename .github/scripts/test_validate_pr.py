"""Tests for validate_pr.py. Each test builds a throwaway git repo (a "base"
with pearCalc and one category) and a PR commit on top, like GitHub's
refs/pull/N/head, then evaluates it."""
import datetime
import json
import os
import subprocess
import tempfile
import unittest

import validate_pr as v

NOW = datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc)
OLD = NOW - datetime.timedelta(days=400)
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 32
GOOD = dict(merged_prs=2, created_at=OLD, open_prs=1)
SW = "data/software/n/ne/newapp"
PC = "data/software/p/pe/pearcalc"


def j(obj):
    return json.dumps(obj, indent="\t").encode()


BASE = {
    "data/categories/productivity.json": j({"name": {"en": "Productivity"}}),
    PC + "/index.json": j({"name": "pearCalc", "links": {"product": "http://www.pearworks.com/"},
                           "cpu": ["x86-64"], "free": True, "categories": ["productivity"]}),
    PC + "/versions/0.6.5a/index.json": j({"version": "0.6.5a", "download": "http://www.pearworks.com/a.zip", "issues": [724]}),
    PC + "/versions/0.6.5a/reports/0001.json": j({"status": "garbage", "description": "Doesn't start"}),
}


def new_software(name="newapp", **over):
    d = {"name": name, "links": {"product": "https://example.com"}, "cpu": ["x86-64"],
         "free": True, "categories": ["productivity"]}
    d.update(over)
    return j(d)


def new_version(ver="1.1", **over):
    d = {"version": ver, "download": "https://example.com/1.1.zip", "issues": None}  # the backend writes issues: null
    d.update(over)
    return j(d)


def new_report(shots=None, **over):
    d = {"status": "gold", "description": "works"}
    if shots is not None:
        d["screenshots"] = shots
    d.update(over)
    return j(d)


class Repo:
    def __init__(self, test):
        self.dir = tempfile.mkdtemp()
        test.addCleanup(lambda: subprocess.run(["rm", "-rf", self.dir]))
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.write(BASE)
        self.base = self.commit("base")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True).stdout.decode().strip()

    def write(self, files):
        for path, content in files.items():
            full = os.path.join(self.dir, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(content)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def index(self, path, content, mode):
        """Stage a blob straight into the index (bypasses the working tree, so
        case-insensitive filesystems cannot merge e.g. P/ and p/)."""
        sha = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.dir, input=content,
                             check=True, capture_output=True).stdout.decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "%s,%s,%s" % (mode, sha, path))

    def pr(self, files=None, delete=(), symlinks=None):
        self.git("checkout", "-q", "-b", "pr", self.base)
        for path, content in (files or {}).items():
            self.index(path, content, "100644")
        for path in delete:
            self.git("rm", "-q", "--cached", path)
        for path, target in (symlinks or {}).items():
            self.index(path, target.encode(), "120000")
        self.git("commit", "-q", "--allow-empty", "-m", "pr")
        return self.git("rev-parse", "HEAD")


class ValidatePR(unittest.TestCase):
    def evaluate(self, files=None, facts=None, **kw):
        repo = Repo(self)
        head = repo.pr(files, **kw)
        return v.evaluate(repo.dir, repo.base, head, facts or GOOD, now=NOW)

    def assertValid(self, r):
        self.assertEqual(r.errors, [], r.errors)

    def assertError(self, r, text):
        self.assertTrue(any(text in e for e in r.errors), "no error containing %r in %s" % (text, r.errors))

    def assertReason(self, r, text):
        self.assertEqual(r.errors, [], r.errors)
        self.assertTrue(any(text in x for x in r.reasons), "no reason containing %r in %s" % (text, r.reasons))
        self.assertFalse(r.eligible)

    # --- happy paths --------------------------------------------------

    def test_report_with_screenshot_for_existing_version_is_eligible(self):
        base = PC + "/versions/0.6.5a/reports"
        r = self.evaluate({base + "/ab12cd34.json": new_report(["screenshot-0.png"]), base + "/ab12cd34/screenshot-0.png": PNG})
        self.assertValid(r)
        self.assertEqual(r.reasons, [])
        self.assertTrue(r.eligible)

    def test_bundle_of_new_software_version_and_report_validates(self):
        files = {SW + "/index.json": new_software(), SW + "/versions/1.1/index.json": new_version(),
                 SW + "/versions/1.1/reports/ab12cd34.json": new_report(["screenshot-0.jpg"]),
                 SW + "/versions/1.1/reports/ab12cd34/screenshot-0.jpg": JPG}
        r = self.evaluate(files)
        self.assertValid(r)
        self.assertTrue(r.eligible)

    # --- trust: software/versions carry links ---------------------------

    def test_first_time_author_adding_software_needs_review_but_is_valid(self):
        r = self.evaluate({SW + "/index.json": new_software()}, dict(GOOD, merged_prs=0))
        self.assertReason(r, "earlier merged")

    def test_first_time_author_report_only_is_still_eligible(self):
        base = PC + "/versions/0.6.5a/reports"
        r = self.evaluate({base + "/ab12cd34.json": new_report()}, dict(GOOD, merged_prs=0))
        self.assertTrue(r.eligible, r.reasons)

    def test_non_https_or_odd_links_need_review(self):
        for bad in ["http://example.com/a.zip", "https://192.168.1.5/a.zip", "https://bit.ly/abc", "https://user@example.com/a.zip", "https://localhost/a.zip"]:
            r = self.evaluate({SW + "/index.json": new_software(), SW + "/versions/1.1/index.json": new_version(download=bad)})
            self.assertReason(r, "link")

    def test_non_http_scheme_is_an_error(self):
        r = self.evaluate({SW + "/index.json": new_software(links={"product": "javascript:alert(1)"})})
        self.assertError(r, "links")

    # --- spam limits -----------------------------------------------------

    def test_new_account_needs_review(self):
        base = PC + "/versions/0.6.5a/reports"
        r = self.evaluate({base + "/ab12cd34.json": new_report()}, dict(GOOD, created_at=NOW - datetime.timedelta(days=2)))
        self.assertReason(r, "account")

    def test_many_open_prs_need_review(self):
        base = PC + "/versions/0.6.5a/reports"
        r = self.evaluate({base + "/ab12cd34.json": new_report()}, dict(GOOD, open_prs=9))
        self.assertReason(r, "open pull requests")

    # --- shape: valid but not for auto-merge -----------------------------

    def test_modifying_an_existing_file_needs_review(self):
        r = self.evaluate({PC + "/index.json": new_software("pearCalc")})
        self.assertReason(r, "modif")

    def test_deleting_a_file_needs_review(self):
        r = self.evaluate(delete=[PC + "/versions/0.6.5a/reports/0001.json"])
        self.assertReason(r, "modif")

    def test_files_outside_the_data_layout_need_review_not_an_error(self):
        r = self.evaluate({".github/workflows/evil.yml": b"on: push"})
        self.assertReason(r, "outside")

    def test_two_software_in_one_pr_need_review(self):
        other = "data/software/o/ot/other"
        r = self.evaluate({SW + "/index.json": new_software(), other + "/index.json": new_software("other")})
        self.assertReason(r, "more than one software")

    def test_too_many_files_need_review(self):
        base = PC + "/versions/0.6.5a/reports"
        files = {"%s/%08x.json" % (base, i): new_report() for i in range(v.MAX_FILES + 1)}
        self.assertReason(self.evaluate(files), "files")

    # --- malformed submissions are hard errors ---------------------------

    def test_invalid_json_is_an_error(self):
        self.assertError(self.evaluate({SW + "/index.json": b"{not json"}), "JSON")

    def test_name_must_match_directory(self):
        self.assertError(self.evaluate({SW + "/index.json": new_software("otherapp")}), "name")

    def test_display_name_may_add_spaces_and_punctuation(self):
        self.assertValid(self.evaluate({SW + "/index.json": new_software("new app")}))
        self.assertValid(self.evaluate({SW + "/index.json": new_software("new·app!")}))

    def test_display_name_must_reduce_to_the_directory_name(self):
        self.assertError(self.evaluate({SW + "/index.json": new_software("New App")}), "name")
        self.assertError(self.evaluate({SW + "/index.json": new_software("new app 2")}), "name")

    def test_display_name_spacing_and_length(self):
        for bad in ("", " newapp", "newapp ", "new  app", "new\tapp", "new\napp", "newapp" + "!" * 100):
            self.assertError(self.evaluate({SW + "/index.json": new_software(bad)}), "name")

    def test_wrong_shard_directory_is_an_error(self):
        self.assertError(self.evaluate({"data/software/x/yy/newapp/index.json": new_software()}), "shard")

    def test_name_differing_only_by_case_from_existing_is_an_error(self):
        self.assertError(self.evaluate({"data/software/P/Pe/PearCalc/index.json": new_software("PearCalc")}), "case")

    def test_unknown_category_and_cpu_are_errors(self):
        self.assertError(self.evaluate({SW + "/index.json": new_software(categories=["nope"])}), "categor")
        self.assertError(self.evaluate({SW + "/index.json": new_software(cpu=["z80"])}), "cpu")

    def test_every_known_cpu_is_accepted(self):
        self.assertValid(self.evaluate({SW + "/index.json": new_software(cpu=["x86-64", "i386", "arm64", "ppc"])}))

    def test_cpu_must_be_a_list_of_known_names(self):
        for bad in ("x86-64", ["x86_64"], ["ppc", 1], ["ppc", None], {"x86-64": True}):
            self.assertError(self.evaluate({SW + "/index.json": new_software(cpu=bad)}), "cpu")

    def test_unknown_keys_are_errors(self):
        self.assertError(self.evaluate({SW + "/index.json": new_software(admin=True)}), "unknown")

    def test_version_for_unknown_software_is_an_error(self):
        self.assertError(self.evaluate({SW + "/versions/1.1/index.json": new_version()}), "does not exist")

    def test_report_for_unknown_version_is_an_error(self):
        self.assertError(self.evaluate({PC + "/versions/9.9/reports/ab12cd34.json": new_report()}), "does not exist")

    def test_version_field_must_match_directory(self):
        self.assertError(self.evaluate({SW + "/index.json": new_software(), SW + "/versions/1.1/index.json": new_version("2.0")}), "version")

    def test_version_may_be_display_text_stored_under_its_directory(self):
        self.assertValid(self.evaluate({SW + "/index.json": new_software(), SW + "/versions/1.0_build_5/index.json": new_version("1.0 (build 5)")}))
        self.assertError(self.evaluate({SW + "/index.json": new_software(), SW + "/versions/1.0build5/index.json": new_version("1.0 (build 5)")}), "1.0_build_5")

    def test_version_spacing_and_length(self):
        for bad in ("1.0 ", "1.0  b", "1.0\tb", "1" * (v.MAX_VERSION + 1)):
            d = v.version_directory(bad) or "x"
            self.assertError(self.evaluate({SW + "/index.json": new_software(), SW + "/versions/%s/index.json" % d: new_version(bad)}), "version")

    def test_bad_report_status_and_empty_description_are_errors(self):
        base = PC + "/versions/0.6.5a/reports/ab12cd34.json"
        self.assertError(self.evaluate({base: new_report(status="shiny")}), "status")
        self.assertError(self.evaluate({base: new_report(description="")}), "description")

    def test_report_referencing_a_missing_screenshot_is_an_error(self):
        base = PC + "/versions/0.6.5a/reports/ab12cd34.json"
        self.assertError(self.evaluate({base: new_report(["screenshot-0.png"])}), "screenshot")

    def test_orphan_screenshot_is_an_error(self):
        base = PC + "/versions/0.6.5a/reports"
        self.assertError(self.evaluate({base + "/ab12cd34/screenshot-0.png": PNG}), "not referenced")

    def test_screenshot_must_be_a_real_small_image(self):
        base = PC + "/versions/0.6.5a/reports"
        rep = {base + "/ab12cd34.json": new_report(["screenshot-0.png"])}
        self.assertError(self.evaluate(dict(rep, **{base + "/ab12cd34/screenshot-0.png": b"<html>not an image</html>"})), "image")
        self.assertError(self.evaluate(dict(rep, **{base + "/ab12cd34/screenshot-0.png": PNG + b"\0" * (v.MAX_SCREENSHOT_BYTES + 1)})), "150")
        self.assertError(self.evaluate(dict(rep, **{base + "/ab12cd34/screenshot-0.png": JPG})), "image")  # JPEG bytes named .png

    def test_symlinks_are_errors(self):
        self.assertError(self.evaluate(symlinks={SW + "/index.json": "/etc/passwd"}), "regular file")


class Unit(unittest.TestCase):
    def test_result_json_has_the_fields_the_workflow_reads(self):
        out = json.loads(v.Result(errors=[], reasons=["r"]).to_json())
        self.assertEqual((out["valid"], out["eligible"]), (True, False))
        self.assertIn("markdown", out)


if __name__ == "__main__":
    unittest.main()
