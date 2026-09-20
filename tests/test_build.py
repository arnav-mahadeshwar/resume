"""Tests for the resume pipeline. Stdlib unittest only — no new dependencies.

    python -m unittest discover -s tests -v

The suite exists because of specific past failures, and each test names the
one it prevents:
  * a two-column template silently clipped text out of the PDF text layer
  * [bracketed] drafts shipped to the live site and the ATS fallback
  * `python build.py --check` crashed on a cp1252 Windows console
  * the README described a print mechanism the CSS had stopped using
"""
import contextlib
import copy
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build  # noqa: E402


def load():
    import yaml
    return yaml.safe_load((ROOT / "resume.yaml").read_text(encoding="utf-8"))


class BuildOnce(unittest.TestCase):
    """One real build, reused by the assertions below."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = pathlib.Path(cls._tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            cls.result = build.build(ROOT / "resume.yaml", cls.out)
        cls.data = cls.result["data"]
        cls.text = cls.result["text"]
        cls.html = cls.result["html"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def lines(self, target):
        return [ln.strip() for ln in self.text[target].splitlines()]

    def between(self, target, start, end):
        """Text layer slice between two heading lines (end exclusive)."""
        lines = self.lines(target)
        i = lines.index(start)
        j = lines.index(end, i + 1)
        return lines[i + 1:j]


class TestArtifacts(BuildOnce):
    def test_every_artifact_is_written(self):
        for name in ("index.html", "ats.html", "index.txt", "ats.txt",
                     "resume.json"):
            with self.subTest(artifact=name):
                self.assertTrue((self.out / name).is_file(), f"{name} missing")
                self.assertGreater((self.out / name).stat().st_size, 1000)

    def test_single_dom_carries_both_faces(self):
        html = self.html["hybrid"]
        self.assertIn("display:contents", html)       # screen: dissolve columns
        self.assertIn("direction:rtl", html)          # print: swap them visually
        self.assertIn("@media print", html)
        # "~1 line of JavaScript" is a load-bearing claim in the README.
        self.assertEqual(html.count("<script"), 0)
        self.assertEqual(html.count("onclick="), 1)

    def test_print_colour_does_not_depend_on_background_graphics(self):
        # Structural colour must be a border, so headless Chrome (which prints
        # without background graphics) matches a manual Ctrl+P.
        topbar = re.search(r"\.print-topbar\{display:block;[^}]*\}",
                           self.html["hybrid"])
        self.assertIsNotNone(topbar)
        self.assertIn("border-top:10px solid", topbar.group(0))

    def test_pdfs_are_named_after_the_candidate(self):
        slug = self.data["basics"]["name"].replace(" ", "-")
        names = build.pdf_names(self.data)
        self.assertEqual(names["hybrid"], f"{slug}-Resume.pdf")
        self.assertEqual(names["ats"], f"{slug}-Resume-ATS.pdf")
        # the download button on the page must point at the file CI produces
        self.assertIn(f'href="{names["hybrid"]}"', self.html["hybrid"])
        self.assertNotIn('href="resume.pdf"', self.html["hybrid"])


class TestAtsContract(BuildOnce):
    def test_contract_holds_for_both_targets(self):
        for target, text in self.text.items():
            with self.subTest(target=target):
                self.assertEqual(
                    build.audit_text_layer(target, text, self.data), [])

    def test_identity_parses_first(self):
        for target in self.text:
            with self.subTest(target=target):
                head = self.lines(target)[:4]
                self.assertIn(self.data["basics"]["name"].upper(),
                              "\n".join(head).upper())
                self.assertTrue(any(self.data["basics"]["email"] in ln
                                    for ln in head))

    def test_sections_are_in_linear_order(self):
        for target, expected in build.ORDER.items():
            with self.subTest(target=target):
                lines = self.lines(target)
                seen = [lines.index(h) for h in expected]
                self.assertEqual(seen, sorted(seen),
                                 f"{target} text layer is not linear")

    def test_no_placeholders_reach_the_output(self):
        for target, text in self.text.items():
            with self.subTest(target=target):
                self.assertIsNone(build.PLACEHOLDER_RX.search(text))
                self.assertIsNone(build.MARKER_RX.search(text))

    def test_dates_are_written_out_in_full(self):
        for target, text in self.text.items():
            with self.subTest(target=target):
                self.assertGreaterEqual(
                    len(build.FULL_RANGE_RX.findall(text)),
                    build.dated_entry_count(self.data))
                self.assertIsNone(build.NUMERIC_DATE_RX.search(text))

    def test_audit_catches_the_placeholder_that_actually_shipped(self):
        broken = copy.deepcopy(self.data)
        broken["engagements"][0]["highlights"][0] = (
            "eliminating [X hours/week] of manual coordination")
        with contextlib.redirect_stdout(io.StringIO()):
            res = build.build(ROOT / "resume.yaml", self.out / "broken")
        # rebuild from the mutated data through the same template path
        html = build.jinja_env().get_template("ats.html.j2").render(
            **build.context(broken))
        findings = build.audit_text_layer("ats", build.text_layer(html), broken)
        self.assertTrue(any("placeholder" in f for f in findings), findings)
        self.assertEqual([], build.audit_text_layer("ats", res["text"]["ats"],
                                                    res["data"]))

    def test_audit_catches_clipped_and_numeric_dates(self):
        clipped = "ARNAV\nWork Experience\nApril 2026 - P\n"
        self.assertTrue(any("complete date ranges" in f for f in
                            build.audit_text_layer("ats", clipped, self.data)))
        numeric = "04/2026 - 07/2026"
        self.assertTrue(any("numeric date" in f for f in
                            build.audit_text_layer("ats", numeric, self.data)))

    def test_audit_catches_filler_and_pronouns(self):
        for bad, word in (("Responsible for deployments", "filler"),
                          ("Then I shipped it", "first-person")):
            with self.subTest(text=bad):
                findings = build.audit_text_layer("ats", bad, self.data)
                self.assertTrue(any(word in f for f in findings), findings)


class TestTextLayer(BuildOnce):
    def test_screen_only_chrome_is_not_in_the_print_text(self):
        text = self.text["hybrid"]
        for chrome in ("zsh", "cat resume.yaml", "render --target",
                       "macchiato", "view-source", "ats version"):
            with self.subTest(chrome=chrome):
                self.assertNotIn(chrome, text)

    def test_print_only_skill_labels_are_kept(self):
        # The sidebar carries snake_case labels on screen and proper-case in
        # print; only the print pair may appear in the text layer.
        self.assertIn("Programming Languages", self.text["hybrid"])
        self.assertNotIn("programming_languages", self.text["hybrid"])

    def test_block_elements_become_separate_lines(self):
        html = "<div><h2>Skills</h2><p>a</p><ul><li>b</li><li>c</li></ul></div>"
        self.assertEqual(build.text_layer(html), "Skills\na\nb\nc\n")

    def test_entities_are_decoded_like_a_parser_would(self):
        self.assertEqual(build.text_layer("<p>AI &amp; ML</p>"), "AI & ML\n")


class TestStructure(BuildOnce):
    def test_work_holds_only_salaried_employment(self):
        self.assertEqual(len(self.data["work"]), 1)
        self.assertEqual(self.data["work"][0]["position"], "AI & ML Engineer")
        self.assertNotIn("intern", self.data["work"][0]["position"].lower())

    def test_volunteer_renders_inside_experience(self):
        block = self.between("ats", "Work Experience",
                             build.ENGAGEMENTS_HEADING)
        org = self.data["volunteer"][0]["organization"]
        self.assertTrue(any(org in ln and build.VOLUNTEER_TAG in ln
                            for ln in block),
                        "volunteer entry must sit inside Experience, tagged")
        self.assertNotIn("Volunteer Experience", self.text["ats"])

    def test_engagements_are_undated_but_scoped(self):
        block = "\n".join(self.between("ats", build.ENGAGEMENTS_HEADING,
                                       "Projects"))
        self.assertIsNone(build.FULL_RANGE_RX.search(block),
                          "engagements must not carry a date range")
        en = self.data["engagements"][0]
        for field in ("duration", "capacity", "status"):
            self.assertIn(en[field], block)
        self.assertIn("Mandate:", block)
        self.assertIn("Outcome:", block)

    def test_engagement_dates_are_a_validation_error(self):
        bad = copy.deepcopy(self.data)
        bad["engagements"][0]["startDate"] = "2026-04-01"
        with self.assertRaises(SystemExit) as ctx:
            build.validate(bad)
        self.assertIn("not allowed", str(ctx.exception))

    def test_validation_reports_every_missing_field_at_once(self):
        with self.assertRaises(SystemExit) as ctx:
            build.validate({"basics": {"name": "Ada"}})
        msg = str(ctx.exception)
        for expected in ("basics.email", "basics.location.city",
                         "work section is empty", "skills section is empty"):
            self.assertIn(expected, msg)


class TestJsonResumeExport(BuildOnce):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.payload = json.loads((cls.out / "resume.json").read_text("utf-8"))

    def test_only_spec_or_namespaced_keys_at_top_level(self):
        spec = {"$schema", "basics", "work", "volunteer", "education", "awards",
                "certificates", "publications", "skills", "languages",
                "interests", "references", "projects", "meta"}
        self.assertTrue(set(self.payload) <= spec, set(self.payload) - spec)

    def test_employer_subsections_are_flattened_for_consumers(self):
        work = self.payload["work"][0]
        self.assertTrue(work["highlights"])
        sub = self.data["work"][0]["subsections"][0]
        self.assertIn(f"{sub['name']}: {sub['highlights'][0]}",
                      work["highlights"])
        self.assertIn("x_subsections", work)  # lossless round-trip kept

    def test_engagements_map_onto_project_entity_roles_type(self):
        en = self.data["engagements"][0]
        match = [p for p in self.payload["projects"]
                 if p.get("entity") == en["client"]]
        self.assertEqual(len(match), 1)
        item = match[0]
        self.assertEqual(item["type"], "freelance engagement")
        self.assertEqual(item["roles"], [en["role"]])
        self.assertNotIn("startDate", item)
        self.assertNotIn("endDate", item)
        self.assertTrue(any(h.startswith("Outcome:") for h in item["highlights"]))

    def test_meta_is_self_describing(self):
        meta = self.payload["meta"]
        self.assertTrue(meta["canonical"].endswith("resume.json"))
        self.assertEqual(meta["version"], "v1.0.0")
        self.assertIn(build.VOLUNTEER_TAG, meta["x_note"])


class TestConsolePortability(unittest.TestCase):
    """The build used to die on a cp1252 console before writing a byte."""

    def test_glyphs_are_chosen_from_stream_capability(self):
        class Fake:
            encoding = "cp1252"
        self.assertFalse(build._encodable("\u2713", Fake()))
        self.assertTrue(build._encodable("[ok]", Fake()))
        class Utf8:
            encoding = "utf-8"
        self.assertTrue(build._encodable("\u2713", Utf8()))

    def test_say_never_raises_on_an_ascii_stream(self):
        buf = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
        with contextlib.redirect_stdout(buf):
            build.say("check \u2713 \u2192 done")  # must not raise

    def test_cli_check_succeeds_on_a_cp1252_console(self):
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "build.py"), "--check",
                 "--out", tmp],
                capture_output=True, text=True, env=env, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0,
                         f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        self.assertNotIn("UnicodeEncodeError", proc.stderr)


class TestHelpers(unittest.TestCase):
    def test_dates_are_rendered_long_form(self):
        self.assertEqual(build.fmt_date("2025-08-01"), "August 2025")
        self.assertEqual(build.fmt_date(None), "Present")
        self.assertEqual(build.daterange({"startDate": "2025-08-01"}),
                         "August 2025 \u2013 Present")

    def test_country_comes_from_the_code_not_a_hardcoded_string(self):
        self.assertEqual(build.country_name("in"), "India")
        self.assertEqual(build.country_name("JP"), "Japan")
        self.assertEqual(build.country_name("ZZ"), "ZZ")

    def test_every_declared_url_is_collected_for_auditing(self):
        urls = build.collect_urls(load())
        self.assertIn("https://www.poonawallafincorp.com", urls)
        self.assertTrue(any("linkedin.com" in u for u in urls))


class TestDocsMatchCode(unittest.TestCase):
    """Karen's third finding: docs that lie are worse than no docs."""

    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.tpl = (ROOT / "templates" / "index.html.j2").read_text("utf-8")
        cls.docstring = build.__doc__

    @staticmethod
    def squeeze(text):
        """Compare CSS claims ignoring the spaces prose likes to add."""
        return re.sub(r"\s+", "", text)

    def test_readme_describes_the_print_mechanism_the_css_uses(self):
        readme, tpl = self.squeeze(self.readme), self.squeeze(self.tpl)
        for mechanism in ("display:table", "direction:rtl", "display:contents"):
            with self.subTest(mechanism=mechanism):
                self.assertIn(mechanism, readme, "README drifted from the CSS")
                self.assertIn(mechanism, tpl, "CSS drifted from the README")

    def test_readme_documents_volunteer_placement_as_built(self):
        self.assertIn(build.VOLUNTEER_TAG, self.readme)
        self.assertIn(build.ENGAGEMENTS_HEADING, self.readme)

    def test_docstring_matches_the_real_workflow(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8")
        self.assertIn("build.py --check", workflow)
        self.assertNotIn("peaceiris", self.docstring)
        self.assertNotIn("peaceiris", workflow)
        self.assertIn("upload-pages-artifact", workflow)

    def test_every_repo_path_named_in_the_readme_exists(self):
        named = set(re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|ya?ml|json|txt|j2))`",
                               self.readme))
        for path in sorted(named):
            if path.startswith("dist/") or path == "resume.json":
                continue  # generated artifacts, covered by TestArtifacts
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), f"README names {path}")


@unittest.skipUnless(os.environ.get("RESUME_LINK_CHECK") == "1",
                     "network test; set RESUME_LINK_CHECK=1")
class TestLinks(unittest.TestCase):
    def test_no_dead_links_on_the_resume(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hard, _soft = build.check_links(load())
        self.assertEqual(hard, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
