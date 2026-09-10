# CLAUDE.md — PithLocatorCT

## Git workflow

**Commit straight to `main` and push there.** Do not create feature branches and
do not open pull requests for this repository. It is a small single-purpose lab
tool with one user; a branch here is overhead with no reviewer on the other end.

```sh
git add -A && git commit -m "..." && git push origin main
```

This holds even when a session's own harness instructions say otherwise (e.g. a
"develop on branch `X`" directive from the calling environment). Finish the work
on whatever branch the session set up if one was forced, then merge or
fast-forward it into `main` and push `main` -- `main` is where this repository's
history lives, not a side branch, regardless of what a particular session was
told to check out.

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

   `parse_stem_numbered_section` needs the same kind of corroboration, and the
   reason is sharper: `GHE-F015-1-A` (section A of core 1) and `KOR-014-A`
   (core A) are the *same shape*. The peel is accepted only when another stem
   splits, by the plain separator rule, to the same peeled tree with a purely
   numeric token -- `GHE-F015-2`. Two peel candidates must never confirm each
   other, or `KOR-014-A` and `KOR-014-B` would invent a tree `KOR`; this is
   why the check is per tree and not a folder-wide flag like `confirmed`.

8. **Sections are pieces of one broken core, not separate cores or repeat
   scans.** `ABC123A1`/`ABC123A2`, `KOR-014-A2`/`KOR-014-A3` and
   `GHE-F015-1-A`/`GHE-F015-1-B` share a tree and a core id (`core_id`),
   differing only in `section_number`. Both come out of `split_token`, which
   takes either token shape -- letter then section number, or core number then
   section letter -- so a numbered core with lettered sections needs no
   separate grouping path. It returns a core id for a purely numeric token too
   (`"1"` -> `("1", 0)`), which looks like it would group `GHE-Q003-1` with
   `-2`: it does not, because those are different ids and `section_group`
   returns `[stem]` for a group of one.
   `App.section_group()` finds all of them; case 3 sums their `accum_rw_mm` by
   default (`_save_result`'s `sum_sections`, default `True`) because measuring
   only the opened section understates ΣRW and silently inflates the computed
   distance to the pith. Do not "simplify" this back to a single file's value
   -- that is the bug this was written to fix.

9. **Switching folders builds a new `App` and rebinds `Handler.app`.**
   Everything folder-derived — the scan, `PreviewCache`, `ResultStore`, the
   species table — is constructed in `App.__init__`, so `POST /api/folder` is
   one `App(path)` and one attribute assignment; the old app stays bound if the
   new one raises, and one `.xlsx` per folder still holds. The Tk folder dialog
   (`pick_folder_dialog`) runs in a **subprocess**: tkinter insists on the main
   thread and the request arrives on a `ThreadingHTTPServer` worker. Exit code 3
   from that subprocess means "no usable dialog here", which the client turns
   into a typed-path prompt — do not conflate it with a cancelled dialog, which
   is a clean exit with empty output.

10. **The species CSV is versioned.** `pith_species_shrinkage.csv` in a data
   folder overrides the shipped table, so it carries a version marker; a file
   from an older build is moved to `.old.csv` rather than read or deleted. Bump
   `SPECIES_TABLE_VERSION` whenever the shipped values change, or users keep
   silently measuring against superseded numbers.

11. **A colour or grayscale core's image is `<stem>.tif`, probed before it is
   trusted.** RingIndicator writes no `_Tv.tif`/`_Rd.tif` for a flat (colour or
   single-page) image — those only come from averaging slices out of a real
   volume — so `find_core_image` falls back to `<stem>.tif`/`.tiff` when there
   is no `_Tv.tif`. `probe_tiff` decides "flat" the same way RingIndicator's
   `flat_by_depth` does (one page, or 3+ samples per pixel), but from real TIFF
   tags rather than a `FileSize/StripByteCounts` guess, and **fails closed**: a
   multi-page volume that happens to be named `<stem>.tif` is never shown as a
   preview, because its page 0 is an edge slice, not the mid-core plane
   RingIndicator averages. A rejected file's reason travels as `image_reject`
   all the way to the UI, so a folder that visibly contains a TIFF doesn't just
   say "no image" with no explanation. `render_png` never applies the CT
   density window to a flat image (`kind != "ct_tv"`): it applies an
   `AUTO_PCT_LO`/`AUTO_PCT_HI` (0.5/99.5) percentile stretch — always for
   colour, and for grayscale wider than 8-bit — and otherwise scales by dtype
   range (`_pick_scale`). The percentiles are pooled over **all channels**, not
   taken from the luminance: a brown core is roughly `150/115/85`, channels
   further apart than any one channel's own spread, so a luminance window is
   narrower than the gap between them and would saturate red while crushing
   blue. The CT window controls are hidden client-side (`supports_window`) and
   the `#pctRow` percentile boxes take their place for a stretched image
   (client-side, on `X-Image-Scale == "auto"`, so they never appear over an
   8-bit grayscale scan they could not affect); the operator's percentiles
   persist across cores like the CT window does (`S.pctSeeded`). `kind` and the
   percentiles are part of the preview cache key so a reclassified file can never
   serve a stale render.

12. **A flat core's pixel size never comes from `_ringwidth.txt` column 3.**
   `readTiffTags.m` converts a TIFF's `XResolution` tag to µm/px only for
   `ResolutionUnit` 1 and 3; for unit 2 (inch — the default for a scanner or
   camera) it leaves the raw tag value unconverted, and that value is what ends
   up in column 3. A CT core has no such gap to fall into by itself, so it keeps
   using column 3, with a warning attached when the number looks like a raw DPI
   or an unconverted tag rather than a pixel size — never a silent
   substitution. A flat core has no such fallback, so for it the only sources
   are an operator-entered value and `<stem>_resolution.txt` (RingIndicator's
   own sidecar, read ahead of column 3 because its Resolution menu writes that
   file when a number there is corrected). Column 1 of `_ringwidth.txt` — the
   width itself — is in pixels, so none of this touches ring geometry or ΣRW in
   pixels; only the µm/px multiplier that turns them into millimetres can be
   wrong.

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
`.chip.active`, `#btnSave` disabled state, plus `#climRow`'s hidden class (should
follow `supports_window`, i.e. only visible for `ct_tv`) and `#pxRow`'s (should
follow `res_editable`). Always check for zero console errors — two real bugs in
this codebase were caught only by `pageerror`.

Test folders are built from the RingIndicator fixtures in
`tests/fixtures/CAM633-3_*`: take the outermost *n* boundaries to make a core
that starts at a younger year, and rename the stems to exercise the grouping
rules (`TREE-A`/`TREE-B`, `TREE-A2`/`TREE-A3` with equal and with differing
oldest years, the no-separator convention, a name with no separator that
should NOT split, and one core with no `_Tv.tif`). Build each grouping scenario
in its **own** folder rather than piling every stem into one: the no-separator
convention is confirmed per-folder once any stem in it splits, so combining
scenarios that are supposed to be unconfirmed (e.g. a lone `SHP856A2N1`) with
one that confirms the same general shape elsewhere in the folder changes the
outcome out from under the test — this is pre-existing behaviour, not a bug,
but it will misdirect you if the fixtures are combined carelessly.

For a colour or grayscale core, colourise the same fixture rather than
synthesising a new one: window the CT plane to 8-bit and stack it into three
channels with a **different tint per channel** (e.g. `[g//8, g, g]`) so a
red-channel collapse (rendering only channel 0) is visually and numerically
distinct (near-black, low mean) from a correct colour render. Useful variants:
a plain uint8 RGB `<stem>.tif` with a `<stem>_resolution.txt`; the same with
`ResolutionUnit=2` and a `_ringwidth.txt` column 3 equal to the raw `XResolution`
value, to exercise the inch-fallthrough warning; a uint16 RGB version (`<<8`)
to check the dtype-range scale does not clip to white; and a multi-page volume
saved under the bare `<stem>.tif` name with **no** `_Tv.tif`, to confirm it is
rejected rather than shown as page 0. The real fixtures `NF531B-1_ringwidth.txt`
(column 3 = `667`, a raw pixels-per-cm tag) and
`mask_NG-15-1_CROP2_TEST_ringwidth.txt` (column 3 = `72`, a bare DPI) are
ready-made bad-resolution cases with no image needed.

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
| `GHE-F015-1-A` + `GHE-F015-1-B` + `GHE-F015-2` | one tree `GHE-F015`; `-1-A`/`-1-B` are sections of core 1 and their ΣRW sums; `-2` is its own core |
| `GHE-F015-1-A` + `GHE-F015-1-B`, no `-2` | tree `GHE-F015-1`, cores `A`/`B` -- unconfirmed, so no peel |
| `KOR-014-A` + `KOR-014-B` | tree `KOR-014` -- same shape, but nothing confirms a tree `KOR` |
| `TREE1-A` with a colour `.tif`, `TREE1-B` with no image at all | `TREE1-A` opens even though it is younger -- `has_image` outranks year, and a colour core counts |
| a real multi-page TIFF saved as `<stem>.tif`, no `_Tv.tif` | `has_image` false, `image_reject` names the page count, `/api/image` 404s |

The folder switch is testable headless apart from the dialog itself: drive
`POST /api/folder` (and `openFolder(path)` in the page) between two folders and
assert the tree list, `#folderPath`, progress and per-folder `pith_offsets.*`.
`POST /api/folder/pick` answers `{"unavailable": true}` in the sandbox, since
there is no tkinter for the running interpreter; the dialog itself was checked
by running `_PICKER` under `xvfb-run` with a Python that has tkinter and seeing
it stay open.

`run.bat` cannot be executed in the sandbox. Its winget branch is the one path
that has never run on real Windows; review it by reading, and be careful with
quoting and with delayed expansion inside parenthesised blocks.
