#!/usr/bin/env python3
"""
resume build pipeline v4 — resume.yaml -> dist/
==============================================================================
One source of truth, four artifact kinds, one ATS contract enforced in CI.

Usage
-----
    pip install -r requirements.txt

    python build.py                  # build every target into ./dist
    python build.py --only ats       # single HTML target (ats | hybrid)
    python build.py --check          # build + audit; exit 1 on any finding
                                     #   (this is the CI gate)
    python build.py --pdf            # also render PDFs via headless Chrome
    python build.py --links          # audit every URL on the resume (network)

Outputs (./dist)
----------------
    index.html   the shapeshifter — terminal UI on screen, two-column
                 "macchiato" document on print. One DOM, no duplicated
                 content, one line of JavaScript.
    ats.html     the guarantee — strictly single-column, standard headers.
                 This is the file that goes to application portals.
    index.txt    the text layer each document yields when a parser extracts
    ats.txt      it: screen-only chrome dropped, block elements newline-
                 separated. This is the Ctrl+A / Ctrl+C test, automated and
                 committed, so a regression is a diff instead of a surprise.
    resume.json  JSON Resume export (schema v1.0.0) so the same content works
                 with the wider jsonresume ecosystem instead of only here.
    <Name>-Resume.pdf / <Name>-Resume-ATS.pdf   with --pdf. Named after the
                 candidate, because that is what a recruiter searches for.

The ATS contract (audit_text_layer)
-----------------------------------
--check fails the build when the extracted text layer stops being a valid
linear resume, not merely when the HTML stops rendering:
    * no unresolved [bracketed] drafts, TODO/FIXME markers, or filler verbs
    * name, phone and email inside the first few lines (identity parses first)
    * section headings in linear order
    * one complete written date range per dated entry ("August 2025 – February
      2026"), and zero numeric "04/2026" forms — the shape that historically
      got clipped to "04/2026 - P" inside the PDF text layer
Nothing here depends on how the PDF was produced, so it holds for both
browser print-to-PDF and the headless renderer.

Schema
------
JSON Resume (https://jsonresume.org/schema/) plus three extensions; the
upstream schema sets additionalProperties: true, so all three stay valid:
    work[].subsections  projects grouped under one employer
    engagements[]       paid freelance/contract work, deliberately undated
    volunteer[]         rendered inside Experience, tagged "(Volunteer,
                        Part-Time)" — never an orphan section
resume.json maps engagements onto JSON Resume's projects[] using its own
entity/roles/type fields, which exist for exactly this case.

CI
--
.github/workflows/deploy.yml: install pinned deps, run the unit tests, run
`python build.py --check --pdf`, upload ./dist as the Pages artifact, deploy.
Console output is ASCII when the terminal cannot encode box glyphs, so the
same commands work on a cp1252 Windows shell.
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser

try:
    import yaml
    from jinja2 import Environment, FileSystemLoader
except ImportError:  # pragma: no cover - environment problem, not logic
    sys.exit("Missing deps. Run: pip install -r requirements.txt")

ROOT = pathlib.Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates"

GENERATOR = "resume-as-code v4 (build.py)"
SCHEMA_URL = ("https://raw.githubusercontent.com/jsonresume/resume-schema/"
              "v1.0.0/schema.json")

# Section naming lives here so the templates, the JSON export and the audit
# can never disagree about what a section is called.
ENGAGEMENTS_HEADING = "Freelance & Contract Experience"
VOLUNTEER_TAG = "Volunteer, Part-Time"

COUNTRY_NAMES = {
    "IN": "India", "US": "United States", "GB": "United Kingdom",
    "DE": "Germany", "JP": "Japan", "SG": "Singapore", "AE": "UAE",
    "CA": "Canada", "AU": "Australia", "NL": "Netherlands",
}

TARGETS = {
    "hybrid": ("index.html", "index.html.j2", "index.txt"),
    "ats": ("ats.html", "ats.html.j2", "ats.txt"),
}
PDF_SUFFIX = {"hybrid": "Resume", "ats": "Resume-ATS"}

def pdf_names(data):
    """`Arnav-Mahadeshwar-Resume.pdf`, not `resume.pdf`.

    The filename is the first thing a recruiter sees in their downloads folder
    and the last thing they search for three weeks later, so it carries the
    candidate's name rather than the build system's.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", data["basics"]["name"]).strip("-")
    return {t: f"{slug}-{suffix}.pdf" for t, suffix in PDF_SUFFIX.items()}

# --------------------------------------------------------------- console
# A resume pipeline that only prints on UTF-8 terminals is a resume pipeline
# half its readers cannot run. Pick glyphs the stream can actually encode.

def _encodable(text, stream=None):
    enc = getattr(stream or sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False

OK = "\u2713" if _encodable("\u2713") else "[ok]"
WARN = "\u26a0" if _encodable("\u26a0") else "[!]"
DASH = "\u2192" if _encodable("\u2192") else "->"

def say(*parts):
    """print() that degrades instead of raising UnicodeEncodeError."""
    msg = " ".join(str(p) for p in parts)
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, "replace").decode(enc, "replace"))

# --------------------------------------------------------------- helpers

MONTHS = ("January February March April May June July August September "
          "October November December").split()

def fmt_date(iso, default="Present"):
    if not iso:
        return default
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
    except ValueError:
        sys.exit(f"resume.yaml: unparseable date {iso!r} - expected YYYY-MM-DD")
    return f"{MONTHS[d.month - 1]} {d.year}"

def daterange(item):
    return f"{fmt_date(item.get('startDate'))} \u2013 {fmt_date(item.get('endDate'))}"

def country_name(code):
    return COUNTRY_NAMES.get(str(code).upper(), str(code))

def jinja_env(template_dir=TEMPLATE_DIR):
    # Default (non-strict) Undefined on purpose: optional schema keys such as
    # engagements[].outcome are tested for truthiness in the templates, and
    # required keys are already guaranteed by validate() with better messages
    # than any Jinja traceback.
    e = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True,
                    trim_blocks=True, lstrip_blocks=True)
    e.filters["daterange"] = daterange
    e.filters["fmtdate"] = fmt_date
    e.filters["country"] = country_name
    return e

def _first(profiles, network):
    for p in profiles or []:
        if (p.get("network") or "").lower() == network.lower():
            return p
    return None

def context(data):
    """Everything the templates need, computed in Python, not in Jinja."""
    b = data["basics"]
    loc = b["location"]
    gh = _first(b.get("profiles"), "GitHub")
    return {
        "r": data,
        "b": b,
        "gh": gh,
        "li": _first(b.get("profiles"), "LinkedIn"),
        "loc": f"{loc['city']}, {loc['region']}, {country_name(loc['countryCode'])}",
        "generator": GENERATOR,
        "engagements_heading": ENGAGEMENTS_HEADING,
        "volunteer_tag": VOLUNTEER_TAG,
        "pdf_name": pdf_names(data)["hybrid"],
        "ats_name": TARGETS["ats"][0],
        "source_url": (f"{gh['url']}/resume/blob/main/resume.yaml" if gh
                       else "https://jsonresume.org/schema/"),
    }

# --------------------------------------------------------------- validation

def validate(r):
    """Fail fast with readable errors instead of Jinja stack traces."""
    errors = []
    b = r.get("basics") or {}
    for key in ("name", "label", "email", "phone", "summary", "location", "url"):
        if not b.get(key):
            errors.append(f"basics.{key} is missing")
    for key in ("city", "region", "countryCode"):
        if not (b.get("location") or {}).get(key):
            errors.append(f"basics.location.{key} is missing")
    if len((b.get("name") or "").split()) < 2:
        errors.append("basics.name needs at least two words (the header splits it)")

    if not r.get("work"):
        errors.append("work section is empty")
    for i, w in enumerate(r.get("work") or []):
        who = w.get("name", "?")
        for key in ("name", "position", "startDate"):
            if not w.get(key):
                errors.append(f"work[{i}] ({who}): {key} missing")
        if not w.get("highlights") and not w.get("subsections"):
            errors.append(f"work[{i}] ({who}): no highlights or subsections")
        for j, sub in enumerate(w.get("subsections") or []):
            if not sub.get("name") or not sub.get("highlights"):
                errors.append(f"work[{i}].subsections[{j}]: needs name + highlights")

    for i, en in enumerate(r.get("engagements") or []):
        who = en.get("client", "?")
        for key in ("client", "role", "highlights"):
            if not en.get(key):
                errors.append(f"engagements[{i}] ({who}): {key} missing")
        for banned in ("startDate", "endDate"):
            if en.get(banned):
                errors.append(
                    f"engagements[{i}] ({who}): {banned} is not allowed - "
                    "contract work is scoped by deliverable, not tenure; "
                    "use `duration` (e.g. \"3-month engagement\")")

    for i, v in enumerate(r.get("volunteer") or []):
        for key in ("organization", "position", "startDate", "highlights"):
            if not v.get(key):
                errors.append(f"volunteer[{i}] ({v.get('organization','?')}): {key} missing")

    for i, p in enumerate(r.get("projects") or []):
        for key in ("name", "highlights", "startDate"):
            if not p.get(key):
                errors.append(f"projects[{i}] ({p.get('name','?')}): {key} missing")

    for i, e in enumerate(r.get("education") or []):
        for key in ("institution", "area", "studyType", "startDate", "score"):
            if not e.get(key):
                errors.append(f"education[{i}]: {key} missing")

    if not r.get("skills"):
        errors.append("skills section is empty")
    if not r.get("education"):
        errors.append("education section is empty")
    if not r.get("languages"):
        errors.append("languages section is empty")
    if errors:
        sys.exit("Schema validation failed:\n  - " + "\n  - ".join(errors))

# --------------------------------------------------------------- text layer
# What a parser recovers from the printed document: screen-only chrome is
# display:none at print time, so it is dropped here too.

SKIP_TAGS = {"style", "script", "head", "title", "nav", "button"}
SKIP_CLASSES = {"screen-only", "term-bar", "prompt-line", "pills", "cursor",
                "btn-print", "hint"}
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "div", "section",
              "article", "aside", "main", "footer", "header", "tr", "ul", "ol"}
VOID_TAGS = {"br", "img", "meta", "link", "hr", "input", "source", "area",
             "base", "col", "embed", "param", "track", "wbr"}

class _TextLayer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip_depth = 0
        self._stack = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            if tag == "br" and not self._skip_depth:
                self.out.append("\n")
            return
        classes = set((dict(attrs).get("class") or "").split())
        hidden = tag in SKIP_TAGS or bool(classes & SKIP_CLASSES)
        self._stack.append(hidden)
        if hidden:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS and not self._skip_depth:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if self._stack and self._stack.pop():
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in BLOCK_TAGS and not self._skip_depth:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.out.append(data)

def text_layer(html):
    """Approximate what a parser extracts from the printed document."""
    p = _TextLayer()
    p.feed(html)
    p.close()
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip()
             for ln in "".join(p.out).split("\n")]
    return "\n".join(ln for ln in lines if ln) + "\n"

# --------------------------------------------------------------- audit

PLACEHOLDER_RX = re.compile(r"\[(?:[A-Za-z][^\]\n]{0,40})\]")
MARKER_RX = re.compile(r"\bTODO\b|\bFIXME\b|\bXXX\b|\bLorem ipsum\b", re.I)
FILLER_RX = re.compile(
    r"\b(responsible for|duties included|helped with|worked on|"
    r"various tasks|team player|hard.?working)\b", re.I)
PRONOUN_RX = re.compile(r"(?<=\s)(I|my|me)(?=[\s,.])")
MONTH_ALT = "|".join(MONTHS)
FULL_RANGE_RX = re.compile(
    rf"(?:{MONTH_ALT}) \d{{4}} \u2013 (?:(?:{MONTH_ALT}) \d{{4}}|Present)")
NUMERIC_DATE_RX = re.compile(r"\b\d{1,2}/\d{2,4}\b")

ORDER = {
    "ats": ["Professional Summary", "Core Competencies", "Work Experience",
            ENGAGEMENTS_HEADING, "Projects", "Education", "Publications",
            "Languages"],
    "hybrid": ["Summary", "Experience", ENGAGEMENTS_HEADING, "Projects",
               "Skills", "Education", "Publication", "Languages"],
}

def dated_entry_count(r):
    """How many complete written date ranges the text layer must contain."""
    n = len(r.get("work") or []) + len(r.get("volunteer") or [])
    n += len(r.get("education") or []) + len(r.get("projects") or [])
    return n  # engagements are undated by design

def audit_text_layer(target, text, r):
    """The ATS contract. Findings here fail --check."""
    f = []
    for m in PLACEHOLDER_RX.finditer(text):
        f.append(f"{target}: unresolved placeholder {m.group(0)!r}")
    for m in MARKER_RX.finditer(text):
        f.append(f"{target}: leftover marker {m.group(0)!r}")
    for m in FILLER_RX.finditer(text):
        f.append(f"{target}: filler phrase {m.group(0)!r} (say what changed instead)")
    for m in PRONOUN_RX.finditer(text):
        f.append(f"{target}: first-person pronoun {m.group(0)!r}")

    lines = text.splitlines()
    head = "\n".join(lines[:6])
    b = r["basics"]
    if b["name"].upper() not in head.upper():
        f.append(f"{target}: name is not in the first 6 extracted lines")
    for field in ("phone", "email"):
        if b[field] not in head:
            f.append(f"{target}: {field} is not in the first 6 extracted lines")

    # Headings occupy a line of their own in the text layer. Match whole lines,
    # never substrings: "Educational Loan product" is not the Education
    # section, and a "Languages:" skill group is not the Languages section.
    heads = {}
    for i, line in enumerate(lines):
        heads.setdefault(line.strip().rstrip(":"), i)
    pos = -1
    for heading in ORDER.get(target, []):
        at = heads.get(heading)
        if at is None:
            f.append(f"{target}: section {heading!r} missing from text layer")
            continue
        if at < pos:
            f.append(f"{target}: section {heading!r} is out of linear order")
        pos = at

    expected = dated_entry_count(r)
    found = len(FULL_RANGE_RX.findall(text))
    if found < expected:
        f.append(f"{target}: {found} complete date ranges, expected {expected} "
                 "(a clipped or partial date is an unparseable date)")
    for m in NUMERIC_DATE_RX.finditer(text):
        f.append(f"{target}: numeric date {m.group(0)!r} - write it out in full")
    return f

# --------------------------------------------------------------- json resume

def to_json_resume(r, now=None):
    """Export as JSON Resume v1.0.0 so the content is not trapped in this repo."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = {"$schema": SCHEMA_URL, "basics": json.loads(json.dumps(r["basics"]))}

    work = []
    for w in r.get("work") or []:
        item = {k: w[k] for k in
                ("name", "position", "location", "url", "startDate", "endDate", "summary")
                if w.get(k)}
        highlights = list(w.get("highlights") or [])
        for sub in w.get("subsections") or []:
            highlights += [f"{sub['name']}: {h}" for h in sub.get("highlights") or []]
        item["highlights"] = highlights
        if w.get("subsections"):
            item["x_subsections"] = w["subsections"]
        work.append(item)
    out["work"] = work

    if r.get("volunteer"):
        out["volunteer"] = [
            {**{k: v[k] for k in ("organization", "position", "url", "startDate",
                                  "endDate", "summary", "highlights") if v.get(k)},
             "x_tag": VOLUNTEER_TAG}
            for v in r["volunteer"]]

    projects = []
    for en in r.get("engagements") or []:
        # projects[] carries entity/roles/type in the upstream schema
        # precisely for client work, so no extension is needed here.
        item = {
            "name": en["client"],
            "entity": en["client"],
            "type": "freelance engagement",
            "roles": [en["role"]],
            "description": " ".join(
                s.strip() for s in (en.get("clientProfile"), en.get("mandate")) if s),
            "highlights": list(en.get("highlights") or []),
        }
        if en.get("outcome"):
            item["highlights"].append(f"Outcome: {en['outcome'].strip()}")
        if en.get("keywords"):
            item["keywords"] = en["keywords"]
        if en.get("url"):
            item["url"] = en["url"]
        for k in ("capacity", "duration", "status"):
            if en.get(k):
                item[f"x_{k}"] = en[k]
        projects.append(item)
    for p in r.get("projects") or []:
        item = {k: p[k] for k in
                ("name", "description", "url", "startDate", "endDate",
                 "keywords", "highlights") if p.get(k)}
        item["type"] = "personal project"
        projects.append(item)
    out["projects"] = projects

    for key in ("education", "publications", "skills", "languages", "awards",
                "certificates", "interests", "references"):
        if r.get(key):
            out[key] = r[key]

    out["meta"] = {
        "canonical": r["basics"]["url"].rstrip("/") + "/resume.json",
        "version": "v1.0.0",
        "lastModified": now.replace(microsecond=0).isoformat(),
        "generator": GENERATOR,
        "x_note": ("engagements[] are paid contracts, exported as projects[] "
                   f"with type='freelance engagement'; volunteer[] renders "
                   f"inside Experience tagged '{VOLUNTEER_TAG}'."),
    }
    return out

# --------------------------------------------------------------- pdf

CHROME_CANDIDATES = (
    "chrome", "google-chrome", "google-chrome-stable", "chromium",
    "chromium-browser", "msedge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

def find_browser():
    env_path = os.environ.get("CHROME_PATH")
    if env_path and pathlib.Path(env_path).exists():
        return env_path
    for cand in CHROME_CANDIDATES:
        found = shutil.which(cand) or (cand if pathlib.Path(cand).exists() else None)
        if found:
            return found
    return None

def render_pdf(browser, html_path, pdf_path):
    """Print a target to PDF headlessly.

    The print stylesheet deliberately uses borders rather than background
    fills for structural colour, so the output matches a manual Ctrl+P even
    though headless Chrome prints without background graphics.
    """
    pdf_path.unlink(missing_ok=True)
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--no-pdf-header-footer", "--virtual-time-budget=4000",
           f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"headless render failed for {html_path.name}: "
                           + " | ".join(tail))
    return pdf_path

# --------------------------------------------------------------- link audit

def collect_urls(r):
    urls = {}

    def add(url, where):
        if url:
            urls.setdefault(url, where)

    b = r.get("basics") or {}
    add(b.get("url"), "basics.url")
    for p in b.get("profiles") or []:
        add(p.get("url"), f"profiles[{p.get('network')}]")
    for key, label in (("work", "name"), ("engagements", "client"),
                       ("volunteer", "organization"), ("projects", "name"),
                       ("publications", "name"), ("education", "institution")):
        for item in r.get(key) or []:
            add(item.get("url"), f"{key}[{item.get(label, '?')}]")
    return urls

# Sites that block automated HEAD/GET but are fine in a browser.
SOFT_STATUS = {401, 403, 405, 429, 999}

def check_links(r, timeout=20):
    import urllib.error
    import urllib.request

    hard, soft = [], []
    for url, where in collect_urls(r).items():
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "Mozilla/5.0 (resume-as-code link audit)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code in SOFT_STATUS or code == 501:  # HEAD unsupported -> retry
                try:
                    req.method = "GET"
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        code = resp.status
                except urllib.error.HTTPError as exc2:
                    code = exc2.code
                except Exception as exc2:  # noqa: BLE001 - network is messy
                    soft.append(f"{where}: {url} unverifiable ({exc2})")
                    continue
        except Exception as exc:  # noqa: BLE001
            hard.append(f"{where}: {url} unreachable ({exc})")
            continue
        if 200 <= code < 400:
            say(f"  {OK} {code} {url}")
        elif code in SOFT_STATUS:
            soft.append(f"{where}: {url} returned {code} (bot-blocked, verify by hand)")
        else:
            hard.append(f"{where}: {url} returned {code}")
    return hard, soft

# --------------------------------------------------------------- build

def build(src=ROOT / "resume.yaml", out=ROOT / "dist", only=None,
          template_dir=TEMPLATE_DIR):
    """Render every artifact. Returns {'html': {...}, 'text': {...}, 'data': r}."""
    src, out = pathlib.Path(src), pathlib.Path(out)
    if not src.exists():
        sys.exit(f"{src} not found")
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    validate(data)
    out.mkdir(parents=True, exist_ok=True)

    e = jinja_env(template_dir)
    ctx = context(data)
    targets = {only: TARGETS[only]} if only else dict(TARGETS)

    html, text = {}, {}
    for name, (fname, tpl, txt_name) in targets.items():
        rendered = e.get_template(tpl).render(**ctx)
        (out / fname).write_text(rendered, encoding="utf-8")
        html[name] = rendered
        text[name] = text_layer(rendered)
        (out / txt_name).write_text(text[name], encoding="utf-8")
        say(f"{OK} {name:6s} {DASH} {out / fname}  (+ {txt_name})")

    payload = to_json_resume(data)
    (out / "resume.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    say(f"{OK} json   {DASH} {out / 'resume.json'}")
    return {"html": html, "text": text, "data": data, "out": out}

# --------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="resume.yaml -> dist/")
    ap.add_argument("--src", default=str(ROOT / "resume.yaml"))
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--only", choices=sorted(TARGETS))
    ap.add_argument("--check", action="store_true",
                    help="audit the extracted text layer; exit 1 on findings (CI gate)")
    ap.add_argument("--pdf", action="store_true",
                    help="also render PDFs with headless Chrome/Edge")
    ap.add_argument("--links", action="store_true",
                    help="audit every URL on the resume (needs network)")
    args = ap.parse_args(argv)

    result = build(args.src, args.out, args.only)
    out = result["out"]
    hard_error = False      # always fatal: the thing you asked for did not work
    contract_failed = False  # fatal under --check: the document is not shippable

    if args.pdf:
        browser = find_browser()
        if not browser:
            say(f"{WARN} no Chrome/Edge/Chromium found; set CHROME_PATH to render PDFs")
            hard_error = True
        else:
            names = pdf_names(result["data"])
            for name in result["html"]:
                pdf = out / names[name]
                try:
                    render_pdf(browser, out / TARGETS[name][0], pdf)
                    kb = pdf.stat().st_size // 1024
                    say(f"{OK} pdf    {DASH} {pdf} ({kb} KB)")
                except Exception as exc:  # noqa: BLE001
                    say(f"{WARN} {exc}")
                    hard_error = True

    findings = []
    for name, text in result["text"].items():
        findings += audit_text_layer(name, text, result["data"])
    if findings:
        say(f"\n{WARN} ATS contract violations:")
        for f in findings:
            say(f"  - {f}")
        contract_failed = True
    else:
        say(f"{OK} ats contract: text layer is linear, dated and placeholder-free")

    if args.links:
        say("\nlink audit:")
        hard, soft = check_links(result["data"])
        for s in soft:
            say(f"{WARN} {s}")
        for h in hard:
            say(f"{WARN} dead link {DASH} {h}")
        if hard:
            hard_error = True
        elif not soft:
            say(f"{OK} every URL on the resume resolves")

    if hard_error or (args.check and contract_failed):
        say(f"\n{WARN} build finished with failures")
        return 1
    ats_pdf = pdf_names(result["data"])["ats"]
    say(f"\nNext: open {out / 'index.html'} | Ctrl+P for the macchiato PDF")
    say(f"      send {out / TARGETS['ats'][0]} (or {ats_pdf}) to job portals")
    return 0

if __name__ == "__main__":
    sys.exit(main())
