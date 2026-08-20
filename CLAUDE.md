# CLAUDE.md — PithLocatorCT

## Git workflow

**Commit straight to `main` and push there.** Do not create feature branches and
do not open pull requests for this repository. It is a small single-purpose lab
tool with one user; a branch here is overhead with no reviewer on the other end.

```sh
git add -A && git commit -m "..." && git push origin main
```

## What this is

A local web tool that estimates the distance from the innermost indicated ring of
an increment core to the pith, for cores measured with
[RingIndicator](https://github.com/UGent-Woodlab/RingIndicator). Read `README.md`
first — it documents the three measurement cases, the naming rules, and the
output columns, and is the user-facing contract.

Python HTTP server + a single-page canvas front end. No build step, no
framework, no bundler. Four runtime dependencies: numpy, tifffile, Pillow,
openpyxl.

```
pithlocator.py        the whole backend: scanning, TIFF -> PNG, results, HTTP
static/index.html     markup, the in-tool manual, the species editor
static/app.js         canvas viewport, ring geometry, the three cases
static/style.css      dark theme; tokens at the top
run.bat / run.sh      launchers that find a Python before doing anything else
```

## Invariants worth knowing before editing

These are the things that look wrong and are not, or that break silently.

1. **World coordinates are pixels of the rendered preview PNG, not of the TIFF.**
   A large TIFF is downsampled when rendered, and `step` is that factor.
   `mmPerWorld = mm_per_px * step` converts world units to millimetres, and
   positions written to the spreadsheet are converted back to original 1-based
   image pixels (`x * step + 1`). Mixing the two spaces produces answers that
   look plausible and are wrong by the downsample factor.

2. **Ring geometry mirrors `ri.view.RingGeometry` in RingIndicator.** A boundary
   at `zpos` with tilt `theta` is the line through `(zpos, (H-1)/2)` with
   direction `(sin θ, cos θ)`, so its endpoints are `zpos ∓ (H-1)/2·tan θ`. Keep
   it that way: if the two tools disagree about where a ring is, the offsets are
   not comparable with the ring widths they will be combined with.

3. **The saved offset is the *perpendicular* distance to the innermost
   boundary**, not the centre-to-centre distance. That is the direction in which
   RingIndicator measures its tilt-corrected ring widths, so it is the one that
   is consistent with `ΣRW`. Inner rings are often tilted 40°+, where the two
   differ by millimetres. Both are recorded; only the perpendicular one feeds
   `Distance_to_Pith_mm`.

4. **`_deep_clean` exists because bare `NaN` is invalid JSON.** A border fracture
   in `_ringwidth.txt` deliberately carries a NaN year. `json.dumps` writes it as
   `NaN`, which `JSON.parse` rejects, so one fractured ring would take down the
   whole `/api/core` response. Everything leaving the server goes through
   `_deep_clean` with `allow_nan=False`.

5. **The `.xlsx` is rebuilt from `pith_offsets.json` on every save**, not
   appended to. The JSON is the source of truth and what resume reads. If the
   spreadsheet is open in Excel the write fails, and that is reported without
   losing the result.

6. **Case-3 inputs are cleared between trees; the species is not.** A
   carried-over diameter would be both wrong and invisible; a carried-over
   species is visible in the dropdown and saves re-picking it for a whole folder.
   Do not "fix" this asymmetry — see `applySaved`.

7. **The no-separator naming convention only applies when the folder confirms
   it.** `parse_stem_structural` reads `ABC123A` as tree `ABC123`, core `A`, but
   `resolve_stem_names` only accepts that split when some *other* stem's
   structural split lands on the same tree -- once confirmed anywhere in the
   folder it applies to every stem, so `TreeID` never mixes split and unsplit
   shapes in one output. A stem matching neither the separator rule nor the
   structural rule becomes its own tree: showing a core alone is recoverable,
   filing it under the wrong tree is not.

8. **Sections are pieces of one broken core, not separate cores or repeat
   scans.** `ABC123A1`/`ABC123A2` (or `KOR-014-A2`/`KOR-014-A3`) share a tree
   and a core letter (`core_letter`), differing only in `section_number`.
   `App.section_group()` finds all of them; case 3 sums their `accum_rw_mm` by
   default (`_save_result`'s `sum_sections`, default `True`) because measuring
   only the opened section understates ΣRW and silently inflates the computed
   distance to the pith. Do not "simplify" this back to a single file's value
   -- that is the bug this was written to fix.

9. **The species CSV is versioned.** `pith_species_shrinkage.csv` in a data
   folder overrides the shipped table, so it carries a version marker; a file
   from an older build is moved to `.old.csv` rather than read or deleted. Bump
   `SPECIES_TABLE_VERSION` whenever the shipped values change, or users keep
   silently measuring against superseded numbers.

## The launchers

`run.bat` and `run.sh` both do three things **in this order**, and the order is
the point: find a Python that runs, offer to install one if there is none, and
only then look at packages. Announcing a package install before knowing whether
Python exists is the bug this replaced.

Candidates are validated by **executing** them, never by testing that the file
exists. On Windows, `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` is on PATH
on many machines and is a stub that only opens the Microsoft Store; an existence
check "finds" it and everything afterwards fails confusingly.

`run.bat` is CRLF and ASCII-only, pinned by `.gitattributes`. Inside a
`call :label` routine `%0` is the label, not the script, so the script directory
is captured once into `HERE` at top level. The Python version floor is enforced
in `pithlocator.py`, not in the launchers, so the shell files stay free of
metacharacters like `>`.

## Testing

There is no unit-test suite; the tool is verified by driving the real UI with
Playwright, which is how every behavioural claim in the README was checked.

```sh
python3 -c "import ast; ast.parse(open('pithlocator.py').read())"
node --check static/app.js
python3 pithlocator.py /path/to/test/cores --port 8792 --no-browser
```

Then drive it with Playwright (Chromium at `/opt/pw-browsers/chromium` in the
cloud sandbox) and assert on the DOM: `#readoutValue`, `#geoWork`, `.tab.active`,
`.chip.active`, `#btnSave` disabled state. Always check for zero console errors —
two real bugs in this codebase were caught only by `pageerror`.

Test folders are built from the RingIndicator fixtures in
`tests/fixtures/CAM633-3_*`: take the outermost *n* boundaries to make a core
that starts at a younger year, and rename the stems to exercise the grouping
rules (`TREE-A`/`TREE-B`, `TREE-A2`/`TREE-A3` with equal and with differing
oldest years, the no-separator convention, a name with no separator that
should NOT split, and one core with no `_Tv.tif`).

Worth re-checking after any change to grouping or selection:

| fixture | expected |
|---|---|
| `TREE1-A` (young) + `TREE1-B` (old) | `TREE1-B` opens — year wins |
| `TREE2-A2` + `TREE2-A3`, same year | `TREE2-A3` opens — section number breaks the tie |
| `TREE3-A2` (old) + `TREE3-A3` (young) | `TREE3-A2` opens — year beats the number |
| `ABC123A` + `ABC123B` | tree `ABC123`, cores `A`/`B` -- structural, no separator |
| `ABC123A` alone, no sibling in the folder | its own tree `ABC123A`, NOT split -- unconfirmed |
| `ABC123A` + `ABC124A` in the same folder | two trees, `ABC123`/`ABC124` -- never merged |
| `XYZ200A1` + `XYZ200A2` | one tree, one core in two sections; case 3's ΣRW is their sum |
| `SHP856A2N1` alone | its own tree, not filed under `SHP856A2N` |

`run.bat` cannot be executed in the sandbox. Its winget branch is the one path
that has never run on real Windows; review it by reading, and be careful with
quoting and with delayed expansion inside parenthesised blocks.
