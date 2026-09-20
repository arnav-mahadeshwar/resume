# resume-as-code

> One YAML file. Four artifacts. A CI gate that reads the output the way a parser does.

**Live:** [arnav-mahadeshwar.github.io/resume](https://arnav-mahadeshwar.github.io/resume) · **Author:** Arnav Mahadeshwar · [LinkedIn](https://www.linkedin.com/in/arnav-bala/)

[![build](https://github.com/arnav-mahadeshwar/resume/actions/workflows/deploy.yml/badge.svg)](https://github.com/arnav-mahadeshwar/resume/actions)
![python](https://img.shields.io/badge/python-3.12-blue)
![deps](https://img.shields.io/badge/deps-pyyaml%20%2B%20jinja2-lightgrey)
![js](https://img.shields.io/badge/runtime%20JS-~1%20line-brightgreen)
![tests](https://img.shields.io/badge/tests-stdlib%20unittest-informational)

---

If you're reading this because it was listed as a project *on the resume it builds* — yes, that's intentional. This repository is the pipeline that generates that document. The resume references the project; the project produces the resume. Updating my resume is:

```bash
vim resume.yaml && git push
```

CI does the rest: run the tests, render every target, audit the extracted text layer, render the PDFs, publish.

---

## Why this exists

A resume in 2026 has to satisfy three very different readers, with conflicting requirements:

| Reader | Wants | Punishes |
|---|---|---|
| **ATS parsers** (~97% of large employers) | Linear text, standard headers, complete dates | Columns, graphics, clipped text, icon fonts |
| **Human recruiters** (~7 seconds of attention) | Visual hierarchy, scannability, polish | Walls of plain text |
| **Engineers** (interviews, referrals) | Evidence you can actually build things | Claims without artifacts |

Most people solve this by maintaining multiple documents that drift out of sync. This repo solves it with **one source of truth and per-audience compilation** — the same way you'd never hand-maintain three copies of a config for dev/staging/prod.

```
                        ┌──────────────────────────────┐
                        │         resume.yaml          │
                        │   JSON Resume schema + 3     │
                        │    extensions (see below)    │
                        └──────────────┬───────────────┘
                                       │  python build.py
        ┌──────────────────┬───────────┴────────┬──────────────────┐
        ▼                  ▼                    ▼                  ▼
┌───────────────┐  ┌───────────────┐   ┌────────────────┐  ┌──────────────┐
│dist/index.html│  │ dist/ats.html │   │ dist/*.txt     │  │resume.json   │
│"the           │  │"the           │   │ the text layer │  │JSON Resume   │
│ shapeshifter" │  │ guarantee"    │   │ a parser sees  │  │export        │
└───┬───────┬───┘  └───────┬───────┘   └────────────────┘  └──────────────┘
    │       │              │                    │
screen│     │Ctrl+P /      │Ctrl+P /            │ audited by
    │       │--pdf         │--pdf               │ build.py --check
    ▼       ▼              ▼                    ▼
terminal  Name-Resume  Name-Resume-ATS    CI fails on a
   UI      .pdf            .pdf           non-linear or
        two-column      single-column     clipped text layer
        (humans)        (portals)
```

---

## The interesting part: one DOM, two faces

`dist/index.html` contains **no duplicated content and ~1 line of JavaScript** (`window.print()`), yet renders as two completely different documents:

- **On screen:** a dark, terminal-themed interactive page — window chrome, blinking cursor, syntax-colored skill chips, hover-lifting cards.
- **On print:** a two-column serif document with a teal accent bar, sidebar skills, and dotted entry separators.

Three CSS mechanisms do a job usually handed to a templating engine:

1. **`display: contents`** — on screen, the print layout's column containers (`.side`, `.main`) are dissolved from the layout tree, so every section becomes a sibling in one flex column.
2. **Flexbox `order`** — those siblings are re-sequenced (`.o1`–`.o8`) into a narrative order for scrolling: summary → skills → experience → freelance → projects → education. Flex `order` is purely visual; it never touches the content stream.
3. **At `@media print`**, both are reverted and the columns become real: `.cols { display: table; direction: rtl }` with `.main`/`.side` as table cells. `direction: rtl` is what moves the sidebar to the **visual left while leaving it last in the DOM** — so extraction order stays linear. `display: table` is deliberate rather than grid or flex: table cells stay attached across a page break, while flex and grid children fragment unreliably in print.

No framework, no build-time duplication, no hydration. View source — it's all there.

---

## The engineering problem: PDF text extraction order

This is the part of the project I'd actually defend in an interview.

When an ATS extracts text from a PDF, it uses one of two strategies — and the applicant never knows which:

- **Content-stream order:** text runs are read in the order they were written to the file. Browser print-to-PDF writes them in approximately **DOM order**.
- **Geometric reconstruction:** coordinates are used to rebuild reading order. Good implementations detect columns and read each whole; crude ones interleave lines by y-position — the classic two-column failure mode.

A multi-column layout can therefore never be *provably* safe for all parsers. This repo's response is defense in depth:

1. **DOM order is a valid resume on its own.** The markup is sequenced `header → summary → experience → freelance → projects → skills → education → publications → languages`, and the sidebar moves left *visually* via `direction: rtl` at print time. Stream-order extractors get a textbook linear resume.
2. **Geometry is kept trivially clean.** Full-width identity header (name/contact always parse first and intact), non-overlapping column x-ranges, no clipping, complete written dates (`August 2025 – February 2026`) with `white-space: nowrap`.
3. **A zero-risk fallback exists.** `ats.html` is strictly single-column — under *any* extraction strategy it produces identical, perfect output. That version goes to application portals; the styled version goes to humans.
4. **Structural color is borders, not backgrounds.** The teal top bar and the rules under each heading are `border-top`, not `background`. They survive printing with *background graphics off*, which is also why the headless renderer in CI produces the same document a human gets from Ctrl+P.

This design was informed by a real failure: an earlier template silently **clipped text past the page edge**, so words were missing from the PDF's text layer entirely — dates truncated to `04/2026 - P`, sentences ending mid-word. No visual inspection caught it. Which motivates the next section.

### The 10-second verification test, automated

Open any resume PDF → `Ctrl+A` → copy → paste into a plain-text editor. If the result reads top-to-bottom as complete sentences with complete dates, parsers can handle it. *(Try it on your own resume. Seriously.)*

Doing that by hand is a ritual nobody repeats on every edit, so the pipeline does it. `build.py` extracts the text layer itself — dropping everything that is `display:none` at print time — writes it to `dist/index.txt` and `dist/ats.txt`, and audits it. `--check` fails the build when:

- an unresolved `[bracketed]` draft, `TODO`/`FIXME`, filler phrase (`responsible for`), or first-person pronoun survives into the output
- name, phone, or email is not inside the first few extracted lines
- section headings come out in non-linear order
- there are fewer complete written date ranges than dated entries, or any numeric `04/2026` date form — the exact shape that used to get clipped to `04/2026 - P`

Because the audit runs on the text layer rather than the HTML, the guarantee is about what a parser recovers, not about what the CSS intended. The two `.txt` files are committed artifacts, so a regression shows up as a **diff**, not a surprise six weeks into a job hunt.

---

## Schema

[JSON Resume](https://jsonresume.org/schema/) in YAML, plus three extensions. The upstream schema sets `additionalProperties: true`, so all three remain valid documents:

```yaml
work:                     # salaried employment only
  - name: "Employer"
    position: "Role"
    subsections:          # EXTENSION 1: several projects under one employer,
      - name: "Project A" # so a parser counts one job, not three
        keywords: [ ... ]
        highlights: [ ... ]

engagements:              # EXTENSION 2: paid freelance/contract work,
  - client: "Client"      # deliberately undated (see below)
    role: "Role"
    clientProfile: "who they are, at what scale"
    capacity: "sole engineer / team of N"
    duration: "3-month engagement"
    status: "Delivered; running in production"
    mandate: "the problem you were hired to solve"
    keywords: [ ... ]
    highlights: [ ... ]
    outcome: "what is live now, and who depends on it"

volunteer: [ ... ]        # EXTENSION 3: rendered INSIDE Experience, tagged
                          # "(Volunteer, Part-Time)" — no orphan sections
```

**Why `engagements` has no dates.** Contract work is scoped by deliverable, not by tenure. A date range on a three-month contract invites the wrong question ("why did it end?") instead of the right one ("what shipped, and is it still running?"). So `startDate`/`endDate` are a *validation error* in this section, and `duration` + `status` carry scope and recency instead. The four fields map to what a hiring manager actually asks about freelance work: who else was on it (`capacity`), what problem it solved (`mandate`), what you built (`highlights`), and whether it survived you (`outcome`).

Everything is rendered under **Freelance & Contract Experience** — a heading that contains the word *Experience*, so section classifiers file it as work rather than as a hobby project.

`dist/resume.json` exports the same content as spec-compliant JSON Resume: `subsections` are flattened into `work[].highlights` (and preserved verbatim under `x_subsections`), and each engagement becomes a `projects[]` entry using the upstream `entity` / `roles` / `type` fields, which exist for exactly this case. The content is interoperable with the wider jsonresume ecosystem instead of being trapped in this repo.

Content rules are enforced by convention plus the linter — the pipeline renders whatever the YAML says:

- Every metric must be defensible in an interview
- Acronyms expanded on first use — `Continuous Integration/Continuous Deployment (CI/CD)` — because keyword matchers are literal
- No self-assigned skill levels
- Outcomes over outputs (time saved, not lines of code written)

---

## Usage

```bash
pip install -r requirements.txt

python build.py                 # → dist/ (html ×2, txt ×2, resume.json)
python build.py --check         # + audit the text layer; exit 1 on findings
python build.py --pdf           # + render both PDFs via headless Chrome/Edge
python build.py --links         # audit every URL on the resume (needs network)
python build.py --only ats      # single HTML target

python -m unittest discover -s tests -t . -v
```

`--pdf` uses whatever Chrome, Chromium, or Edge is already installed (or `CHROME_PATH`); there is no headless-browser dependency to install. **Manual export** works too, and needs no special print settings: `Ctrl+P` → Save as PDF → uncheck *Headers and footers* → Margins: *None*. Background graphics are optional — structural color is drawn with borders.

**Deploy:** push to `main`. The [workflow](.github/workflows/deploy.yml) installs pinned deps, runs the test suite, runs `python build.py --check --pdf`, and publishes `dist/` to GitHub Pages. Pull requests run the same gate without deploying. The badge at the top of this README is that pipeline's live status.

```
.
├── resume.yaml              # the single source of truth
├── build.py                 # YAML → validate → Jinja2 → audit → artifacts
├── requirements.txt         # two pinned deps, nothing else
├── templates/
│   ├── index.html.j2        # the dual-face document (screen + print CSS)
│   └── ats.html.j2          # the single-column guarantee
├── tests/
│   └── test_build.py        # stdlib unittest; each test names a past failure
├── .github/workflows/
│   └── deploy.yml           # test → build → audit → PDFs → deploy
└── dist/                    # generated — never edited by hand
    ├── index.html           # terminal (screen) ⇄ macchiato (print)
    ├── ats.html             # linear, parser-guaranteed
    ├── index.txt / ats.txt  # the extracted text layer, committed
    ├── resume.json          # JSON Resume export
    └── Arnav-Mahadeshwar-Resume{,-ATS}.pdf   # CI-rendered, gitignored
```

The PDFs are named after the candidate, not after the build system: `resume.pdf`
is anonymous in a recruiter's downloads folder three weeks later. They are
regenerated by CI on every push and published to Pages, so they are gitignored
rather than committed as binaries — while `dist/*.html`, `dist/*.txt` and
`dist/resume.json` *are* committed, because the text layer is the artifact
under review and a parsing regression should show up as a reviewable diff.

---

## FAQ

**Why not just use a resume builder / LaTeX / one of the many resume-as-code tools?**
I started with one (resumed/JSON-Resume themes). Its two-column template silently clipped text out of the PDF's text layer — undetectable visually, fatal to parsers. Owning the render pipeline means owning the failure modes. The JSON Resume export in `dist/resume.json` means owning them didn't cost interoperability.

**Isn't the styled PDF still risky for ATS?**
Marginally, yes — see [the extraction-order section](#the-engineering-problem-pdf-text-extraction-order). That residual risk is why `ats.html` exists and why portal submissions always use it. Right tool, right audience.

**Why are tests in a resume repo?**
Because the failure mode is invisible and expensive. A broken build announces itself; a resume whose third bullet lost its date announces nothing, and you find out by not hearing back. Every test in `tests/test_build.py` corresponds to something that actually went wrong: clipped dates, shipped `[placeholders]`, a `--check` command that crashed on a Windows cp1252 console before writing a byte, and a README that described a print mechanism the CSS had stopped using.

**Is listing this repo on the resume it generates a bit much?**
It's a working demonstration of YAML schema design, templating, CSS layout internals, CI/CD, test design, and applied knowledge of how hiring systems parse documents — with a live build badge as proof. The self-reference is just the honest shape of the dependency graph.

---

*If you're a recruiter or hiring manager: the [live version](https://arnav-mahadeshwar.github.io/resume) has a `⎙ print` button — press it and watch the page reshape itself. There's a `↓ pdf` link beside it if you'd rather skip the trick. That transformation, and everything above, is the work sample.*
