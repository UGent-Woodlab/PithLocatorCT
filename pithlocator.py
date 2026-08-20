#!/usr/bin/env python3
"""
PithLocatorCT -- estimate the distance to the pith on RingIndicator CT cores.

Local web tool. Point it at a folder of RingIndicator output, it groups the
cores into trees, picks the core of each tree that reaches furthest back in
time, and lets you estimate the distance from the innermost indicated ring to
the pith. Results accumulate in an .xlsx beside the data so the session can be
stopped and resumed.

    python3 pithlocator.py /path/to/cores

Nothing leaves the machine: the server binds to localhost only.
"""

import argparse
import datetime as _dt
import glob
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

TOOL_VERSION = "1.1"
SPECIES_TABLE_VERSION = 2

# --------------------------------------------------------------------------
# dependencies

_MISSING = []
try:
    import numpy as np
except ImportError:
    _MISSING.append("numpy")
try:
    import tifffile
except ImportError:
    _MISSING.append("tifffile")
try:
    from PIL import Image
except ImportError:
    _MISSING.append("pillow")
try:
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
except ImportError:
    _MISSING.append("openpyxl")

if sys.version_info < (3, 8):
    sys.stderr.write(
        "This tool needs Python 3.8 or newer; this interpreter is %s.\n"
        % ".".join(str(v) for v in sys.version_info[:3])
    )
    sys.exit(1)

if _MISSING:
    sys.stderr.write(
        "Missing Python packages: %s\n\nInstall them with:\n\n    pip install %s\n"
        % (", ".join(_MISSING), " ".join(_MISSING))
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# species / shrinkage table
#
# Sr is the TOTAL green -> oven-dry RADIAL shrinkage, as a fraction. It is used
# exactly as in CodeMain.qmd:  RW_green = RW_ovendry / (1 - Sr)
#
# "source" is carried through into the Excel output so that every saved row
# records which number was used and where it came from.

TROPIX = "CIRAD Tropix 7 (temperate species)"

DEFAULT_SPECIES = [
    # (latin name, Sr, source)
    ("Abies alba",             0.040, TROPIX),
    ("Acer pseudoplatanus",    0.045, TROPIX),
    ("Castanea sativa",        0.042, TROPIX),
    ("Cedrus atlantica",       0.041, TROPIX),
    ("Fagus sylvatica",        0.057, TROPIX),
    ("Fraxinus excelsior",     0.057, TROPIX),
    ("Larix decidua",          0.042, TROPIX),
    ("Picea abies",            0.039, TROPIX),
    ("Pinus pinaster",         0.045, TROPIX),
    ("Pinus radiata",          0.042, TROPIX),
    ("Pinus sylvestris",       0.052, TROPIX),
    ("Pinus uncinata",         0.041, TROPIX),
    ("Populus p.p.",           0.048, TROPIX),
    ("Pseudotsuga menziesii",  0.047, TROPIX),
    ("Quercus robur/petraea",  0.045, TROPIX),
    ("Thuja plicata",          0.022, TROPIX),
    ("Tilia x europaea",       0.050, TROPIX),
]

SPECIES_CSV = "pith_species_shrinkage.csv"


def species_csv_path(folder):
    return os.path.join(folder, SPECIES_CSV)


SPECIES_MARKER = "# pithlocator species table v%d" % SPECIES_TABLE_VERSION


def load_species(folder):
    """Species table, from the folder's CSV if the operator has edited one.

    A CSV written by an older build carries a different marker, or none at all.
    Keeping it would silently override the shipped table with superseded
    numbers, so it is set aside rather than read -- and set aside rather than
    deleted, because it may hold hand-entered values worth recovering.
    """
    path = species_csv_path(folder)
    if os.path.isfile(path):
        if not _species_csv_current(path):
            stale = os.path.join(folder, "pith_species_shrinkage.old.csv")
            try:
                os.replace(path, stale)
                sys.stderr.write(
                    "The species table in this folder was written by an older "
                    "version.\nIt has been moved to %s and the current table "
                    "loaded instead.\n" % os.path.basename(stale))
            except OSError:
                pass
            return _default_species()
        rows = []
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                for n, line in enumerate(fh):
                    line = line.strip()
                    if not line or line.startswith("#") or \
                       line.lower().startswith("species"):
                        continue
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 2:
                        continue
                    try:
                        sr = float(parts[1])
                    except ValueError:
                        continue
                    src = parts[2] if len(parts) > 2 else "user supplied"
                    rows.append({"name": parts[0], "sr": sr, "source": src})
            if rows:
                return rows
        except OSError:
            pass
    return _default_species()


def _default_species():
    return [{"name": n, "sr": v, "source": src} for n, v, src in DEFAULT_SPECIES]


def _species_csv_current(path):
    """True when the file's first line is this build's version marker."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.readline().strip() == SPECIES_MARKER
    except OSError:
        return False


def save_species(folder, rows):
    path = species_csv_path(folder)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("%s\n" % SPECIES_MARKER)
        fh.write("Species,Sr_radial_shrinkage,Source\n")
        for r in rows:
            fh.write("%s,%.4f,%s\n" % (r["name"], r["sr"], r.get("source", "")))
    return path


# --------------------------------------------------------------------------
# reading RingIndicator sidecar files

def _read_numeric_table(path):
    """Rows of comma-separated numbers; non-numeric cells become NaN."""
    out = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cells = [c.strip() for c in line.replace("\t", ",").split(",")]
            row = []
            for c in cells:
                if c == "":
                    row.append(float("nan"))
                    continue
                try:
                    row.append(float(c))
                except ValueError:
                    row.append(float("nan"))
            out.append(row)
    if not out:
        return np.zeros((0, 0))
    width = max(len(r) for r in out)
    arr = np.full((len(out), width), np.nan)
    for i, r in enumerate(out):
        arr[i, : len(r)] = r
    return arr


def read_ring_and_fibre(path):
    """zpos (px along the core), theta (transverse tilt), phi (radial tilt)."""
    arr = _read_numeric_table(path)
    if arr.size == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    zpos = arr[:, 0]
    keep = np.isfinite(zpos)
    arr = arr[keep]
    zpos = arr[:, 0]
    n = len(zpos)

    def angles(col):
        v = np.zeros(n)
        if arr.shape[1] > col:
            c = arr[:, col]
            v[: len(c)] = c[:n]
            v[~np.isfinite(v)] = 0.0
        return v

    return zpos, angles(1), angles(2)


def read_ringwidth(path):
    """
    One row per ring WIDTH, six columns:
      0 width px (tilt corrected) | 1 year | 2 microns per pixel
      3 felling date | 4 missing rings inserted | 5 fracture type
    """
    arr = _read_numeric_table(path)
    info = {
        "widths_px": [], "years": [], "res_um": None, "fell_date": None,
        "n_widths": 0, "n_missing": 0, "n_broken": 0,
    }
    if arr.size == 0 or arr.shape[1] < 1:
        return info
    w = arr[:, 0]
    keep = np.isfinite(w)
    arr = arr[keep]
    if arr.shape[0] == 0:
        return info
    info["widths_px"] = arr[:, 0].tolist()
    info["n_widths"] = int(arr.shape[0])
    if arr.shape[1] > 1:
        info["years"] = arr[:, 1].tolist()
    if arr.shape[1] > 2 and np.isfinite(arr[0, 2]) and arr[0, 2] > 0:
        info["res_um"] = float(arr[0, 2])
    if arr.shape[1] > 3 and np.isfinite(arr[0, 3]):
        info["fell_date"] = float(arr[0, 3])
    if arr.shape[1] > 4:
        c = arr[:, 4]
        info["n_missing"] = int(np.nansum(c[np.isfinite(c)]))
    if arr.shape[1] > 5:
        c = arr[:, 5]
        info["n_broken"] = int(np.sum(np.isfinite(c) & (c > 0)))
    return info


TV_PATTERNS = ("{s}_Tv.tif", "{s}_Tv.tiff", "{s}_TV.tif", "{s}_tv.tif")


def find_transverse(folder, stem):
    for pat in TV_PATTERNS:
        p = os.path.join(folder, pat.format(s=stem))
        if os.path.isfile(p):
            return p
    hits = sorted(glob.glob(os.path.join(folder, stem + "*Tv*.tif*")))
    return hits[0] if hits else None


# --------------------------------------------------------------------------
# core discovery and tree grouping
#
# A core file name ends in a token that identifies the core within its tree.
# Two conventions are recognised, tried in this order:
#
#   1. An explicit "-" or "_" separator states outright where the tree name
#      ends, so it always wins when present:
#
#          GHE-Q003-1, GHE-Q003-2      tree GHE-Q003, cores 1 and 2
#          KOR-014-A,  KOR-014-B       tree KOR-014,  cores A and B
#          KOR-014-A2, KOR-014-A3      tree KOR-014,  cores A2 and A3
#
#   2. The common dendrochronology convention with NO separator at all: a site
#      code and tree number (ending in a digit), then a one- or two-letter core
#      id, then an optional number if that core was scanned in several pieces:
#
#          ABC123A,  ABC123B           tree ABC123, cores A and B
#          ABC123A1, ABC123A2          tree ABC123, core A in two sections
#
# A2 and A3 -- or A1 and A2 -- are not two different cores: they are SECTIONS of
# one physical core that had to be scanned in pieces, sharing a tree and a core
# letter. See section_group() below, which sums their ring width for case 3.
#
# Convention 2 only applies when at least one OTHER stem in the same folder
# confirms it, i.e. some other stem structurally splits to the same tree (see
# the "confirmed" pass in scan_folder). Without that corroboration a name is
# left whole rather than guessed at: "ABC123A" alone, with no ABC123-anything
# else in the folder, might just as easily be a complete, unsuffixed name, and
# splitting it would invent a tree "ABC123" that does not otherwise exist. This
# also protects a lone id that only coincidentally ends in "letter(s)" -- a
# site code ending in a letter, for instance -- from being carved up on no
# evidence. The separator rule needs no such corroboration: an explicit
# separator is a statement, not a guess.
#
# A stem matching neither convention is treated as its own tree. That is the
# safe direction to fail: it shows the core on its own rather than silently
# filing it under a tree it does not belong to.
SEPARATOR_TOKEN_RE = re.compile(r"^[A-Za-z]{0,2}[0-9]{0,3}$")
STRUCTURAL_RE = re.compile(r"^(?P<tree>.*[0-9])(?P<letter>[A-Za-z]{1,2})(?P<section>[0-9]{0,3})$")
TOKEN_SPLIT_RE = re.compile(r"^(?P<letter>[A-Za-z]{0,2})(?P<section>[0-9]{0,3})$")


def parse_stem_separator(stem):
    """Try the explicit "-"/"_" rule. (tree, token) if it applies, else None."""
    cut = max(stem.rfind("-"), stem.rfind("_"))
    if cut <= 0:
        return None
    tail = stem[cut + 1:]
    if not tail or not SEPARATOR_TOKEN_RE.match(tail):
        return None
    return stem[:cut], tail


def parse_stem_structural(stem):
    """Try the no-separator convention. (tree, token) if it applies, else None.

    token is the core letter plus its section number run together (e.g. "A2"),
    matching the shape a separator-rule token already has, so both feed the
    same core_letter()/section_number() below.
    """
    m = STRUCTURAL_RE.match(stem)
    if not m:
        return None
    return m.group("tree"), m.group("letter") + m.group("section")


def core_letter(token):
    """Letter part of a core token: "A2" -> "A", "B" -> "B", "1" -> ""."""
    m = TOKEN_SPLIT_RE.match(token or "")
    return m.group("letter") if m else ""


def section_number(token):
    """Section number within a core: "A2" -> 2, "A" -> 0, "1" -> 0.

    A core broken up during scanning is split into sections that share a tree
    and a core letter, e.g. A1 and A2 are two pieces of core A -- see
    section_group() below, which groups them for summing case 3's ring width.
    Zero for a purely numeric token, because there the number identifies the
    core itself: GHE-Q003-1 and GHE-Q003-2 are two different cores, not two
    sections of one, so "2" is not a section number.
    """
    m = TOKEN_SPLIT_RE.match(token or "")
    if not m or not m.group("letter") or not m.group("section"):
        return 0
    return int(m.group("section"))


def scan_folder(folder):
    """Discover cores, read their ring data, group into trees."""
    cores = {}
    for path in sorted(glob.glob(os.path.join(folder, "*_ring_and_fibre.txt"))):
        stem = os.path.basename(path)[: -len("_ring_and_fibre.txt")]
        zpos, theta, phi = read_ring_and_fibre(path)
        if len(zpos) == 0:
            continue
        order = np.argsort(zpos)
        zpos, theta, phi = zpos[order], theta[order], phi[order]

        rw_path = os.path.join(folder, stem + "_ringwidth.txt")
        rw = read_ringwidth(rw_path) if os.path.isfile(rw_path) else read_ringwidth_empty()

        years = [y for y in rw["years"] if np.isfinite(y)]
        oldest = min(years) if years else None
        newest = max(years) if years else None
        res_um = rw["res_um"]
        accum_px = float(np.nansum(np.array(rw["widths_px"]))) if rw["widths_px"] else 0.0

        tv = find_transverse(folder, stem)

        cores[stem] = {
            "stem": stem,
            "n_boundaries": int(len(zpos)),
            "n_widths": rw["n_widths"],
            "oldest_year": oldest,
            "newest_year": newest,
            "res_um": res_um,
            "fell_date": rw["fell_date"],
            "accum_rw_px": accum_px,
            "accum_rw_mm": (accum_px * res_um / 1000.0) if res_um else None,
            "n_missing": rw["n_missing"],
            "n_broken": rw["n_broken"],
            "has_image": tv is not None,
            "image_path": tv,
            "zpos": zpos, "theta": theta, "phi": phi,
            "years": rw["years"],
            "widths_px": rw["widths_px"],
        }

    # Tree/core assignment needs every stem in the folder at once: the
    # structural convention is only accepted when some OTHER stem confirms it
    # (see resolve_stem_names), which cannot be decided one file at a time.
    for stem, (tree, token) in resolve_stem_names(list(cores)).items():
        cores[stem]["tree"] = tree
        cores[stem]["core_token"] = token

    trees = {}
    for stem, c in cores.items():
        trees.setdefault(c["tree"], []).append(stem)

    out = []
    for tree in sorted(trees):
        stems = sorted(trees[tree])
        out.append({"tree": tree, "cores": stems, "selected": select_core(cores, stems)})
    return cores, out


def resolve_stem_names(stems):
    """(tree, core token) for every stem, deciding the structural convention
    once for the whole folder. See the module comment above for why."""
    tentative = {}
    for stem in stems:
        sep = parse_stem_separator(stem)
        if sep is not None:
            tentative[stem] = ("sep",) + sep
            continue
        struct = parse_stem_structural(stem)
        if struct is not None:
            tentative[stem] = ("struct",) + struct
            continue
        tentative[stem] = ("self", stem, "")

    struct_trees = {}
    for stem, (kind, tree, _token) in tentative.items():
        if kind == "struct":
            struct_trees.setdefault(tree, []).append(stem)
    confirmed = any(len(v) >= 2 for v in struct_trees.values())

    resolved = {}
    for stem, (kind, tree, token) in tentative.items():
        resolved[stem] = (stem, "") if (kind == "struct" and not confirmed) \
            else (tree, token)
    return resolved


def read_ringwidth_empty():
    return {"widths_px": [], "years": [], "res_um": None, "fell_date": None,
            "n_widths": 0, "n_missing": 0, "n_broken": 0}


def select_core(cores, stems):
    """
    The core of a tree that reaches furthest back: lowest oldest indicated year.
    Cores with no image cannot be worked on, so they lose to ones that have one.

    On an equal oldest year the higher section number wins -- e.g. between two
    sections of one broken core, A3 opens rather than A2. There is no real
    reason to prefer one section over another this way, but the choice needs to
    be *something* and it needs to be stable, and the previous behaviour (before
    sections were understood to be pieces of one core rather than repeat scans)
    already picked this way. It is only a tie-break: a core that genuinely
    reaches further back wins whatever its number. Every core of the tree stays
    listed either way, so a wrong pick is one click to correct and nothing is
    ever hidden.
    """
    def key(stem):
        c = cores[stem]
        oy = c["oldest_year"]
        return (
            0 if c["has_image"] else 1,
            oy if oy is not None else float("inf"),
            -section_number(c["core_token"]),
            -c["n_boundaries"],
            -(c["accum_rw_px"] or 0.0),
            stem,
        )
    return sorted(stems, key=key)[0] if stems else None


# --------------------------------------------------------------------------
# preview rendering

def render_png(image_path, lo, hi, max_dim=16000):
    """Transverse preview as 8-bit grayscale PNG, density window [lo, hi]."""
    with tifffile.TiffFile(image_path) as tf:
        arr = tf.pages[0].asarray()
    if arr.ndim == 3:
        arr = arr[..., 0] if arr.shape[-1] <= 4 else arr[0]
    arr = arr.astype(np.float32)

    step = 1
    h, w = arr.shape
    if max(h, w) > max_dim:
        step = int(math.ceil(max(h, w) / float(max_dim)))
        arr = arr[::step, ::step]

    if hi <= lo:
        hi = lo + 1.0
    arr = (arr - lo) * (255.0 / (hi - lo))
    np.clip(arr, 0, 255, out=arr)
    img = Image.fromarray(arr.astype(np.uint8), mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=3)
    return buf.getvalue(), (h, w), step


class PreviewCache(object):
    """Rendered PNGs on disk, keyed by file identity plus the density window."""

    def __init__(self, folder):
        self.dir = os.path.join(folder, ".pith_cache")
        self.lock = threading.Lock()
        self.meta = {}
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError:
            self.dir = None

    def get(self, image_path, lo, hi):
        st = os.stat(image_path)
        key = hashlib.sha1(
            ("%s|%d|%d|%s|%s" % (image_path, st.st_mtime_ns, st.st_size, lo, hi)).encode()
        ).hexdigest()[:20]
        with self.lock:
            if key in self.meta and (self.dir is None or os.path.isfile(self._p(key))):
                return self._read(key), self.meta[key]
            data, shape, step = render_png(image_path, lo, hi)
            info = {"height": shape[0], "width": shape[1], "step": step}
            self.meta[key] = info
            self._write(key, data)
            return data, info

    def _p(self, key):
        return os.path.join(self.dir, key + ".png")

    def _read(self, key):
        if self.dir is None:
            return self._mem[key]
        with open(self._p(key), "rb") as fh:
            return fh.read()

    def _write(self, key, data):
        if self.dir is None:
            if not hasattr(self, "_mem"):
                self._mem = {}
            self._mem[key] = data
            return
        try:
            with open(self._p(key), "wb") as fh:
                fh.write(data)
        except OSError:
            self.dir = None
            self._mem = {key: data}


# --------------------------------------------------------------------------
# results store

RESULT_JSON = "pith_offsets.json"
RESULT_XLSX = "pith_offsets.xlsx"

COLUMNS = [
    ("TreeID", "tree"),
    ("Core", "core"),
    ("OtherCores", "other_cores"),
    ("Species", "species"),
    ("Method", "method"),
    ("Distance_to_Pith_mm", "distance_mm"),
    ("Perpendicular_mm", "perp_mm"),
    ("Euclidean_mm", "euclid_mm"),
    ("Bark_Thickness_mm", "bark_mm"),
    ("Diameter_over_Bark_mm", "diameter_mm"),
    ("Diameter_Entered_As", "diameter_input"),
    ("Barkless_Radius_mm", "barkless_radius_mm"),
    ("AccumRW_ovendry_mm", "accum_ovendry_mm"),
    ("AccumRW_Files", "accum_files"),
    ("Sr_radial_shrinkage", "sr"),
    ("AccumRW_green_mm", "accum_green_mm"),
    ("Outer_Gap_mm", "outer_gap_mm"),
    ("n_Rings", "n_rings"),
    ("Oldest_Year", "oldest_year"),
    ("Newest_Year", "newest_year"),
    ("PixelSize_um", "res_um"),
    ("Pith_X_px", "pith_x_px"),
    ("Pith_Y_px", "pith_y_px"),
    ("InnerRing_zpos_px", "inner_zpos_px"),
    ("InnerRing_theta_rad", "inner_theta"),
    ("Sr_Source", "sr_source"),
    ("Flag", "flag"),
    ("Notes", "notes"),
    ("Timestamp", "timestamp"),
    ("Tool_Version", "tool_version"),
]

METHOD_LABEL = {
    "indicated": "pith indicated on core (offset 0)",
    "concentric": "concentric circles",
    "geometric": "diameter + bark (shrinkage corrected)",
}


class ResultStore(object):
    def __init__(self, folder):
        self.folder = folder
        self.json_path = os.path.join(folder, RESULT_JSON)
        self.xlsx_path = os.path.join(folder, RESULT_XLSX)
        self.lock = threading.Lock()
        self.rows = {}
        self._load()

    def _load(self):
        if not os.path.isfile(self.json_path):
            return
        try:
            with open(self.json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for r in data.get("results", []):
                if r.get("tree"):
                    self.rows[r["tree"]] = r
        except (OSError, ValueError) as exc:
            sys.stderr.write("Could not read %s (%s); starting a new result set.\n"
                             % (self.json_path, exc))

    def save(self, row):
        with self.lock:
            self.rows[row["tree"]] = row
            self._write_json()
            err = self._write_xlsx()
        return err

    def delete(self, tree):
        with self.lock:
            self.rows.pop(tree, None)
            self._write_json()
            self._write_xlsx()

    def _write_json(self):
        tmp = self.json_path + ".tmp"
        payload = {"tool_version": TOOL_VERSION,
                   "written": _dt.datetime.now().isoformat(timespec="seconds"),
                   "results": [self.rows[k] for k in sorted(self.rows)]}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        os.replace(tmp, self.json_path)

    def _write_xlsx(self):
        """Rewritten from scratch on every save; returns a message on failure."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "pith offsets"
        head_font = Font(bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", fgColor="2F4858")
        for j, (title, _) in enumerate(COLUMNS, start=1):
            c = ws.cell(row=1, column=j, value=title)
            c.font = head_font
            c.fill = head_fill
            c.alignment = Alignment(horizontal="left")
            ws.column_dimensions[c.column_letter].width = max(12, min(26, len(title) + 3))
        for i, key in enumerate(sorted(self.rows), start=2):
            row = self.rows[key]
            for j, (_, field) in enumerate(COLUMNS, start=1):
                v = row.get(field)
                if isinstance(v, float) and not math.isfinite(v):
                    v = None
                ws.cell(row=i, column=j, value=v)
        ws.freeze_panes = "A2"
        try:
            wb.save(self.xlsx_path)
            return None
        except OSError as exc:
            return ("Could not write %s (%s). The results are safe in %s."
                    % (RESULT_XLSX, exc, RESULT_JSON))


# --------------------------------------------------------------------------
# state

class App(object):
    def __init__(self, folder):
        self.folder = os.path.abspath(folder)
        self.cache = PreviewCache(self.folder)
        self.store = ResultStore(self.folder)
        self.species = load_species(self.folder)
        if not os.path.isfile(species_csv_path(self.folder)):
            try:
                save_species(self.folder, self.species)
            except OSError:
                pass  # a read-only folder is no reason not to run
        self.rescan()

    def rescan(self):
        self.cores, self.trees = scan_folder(self.folder)

    def species_names(self):
        return [s["name"] for s in self.species]

    def section_group(self, stem):
        """Every core sharing this one's tree and core letter, sorted by
        section number -- the pieces of one physical core that was scanned in
        parts. [stem] alone when its token has no letter, or has no siblings.
        """
        c = self.cores.get(stem)
        if c is None:
            return [stem]
        letter = core_letter(c["core_token"])
        if not letter:
            return [stem]
        group = [s for s, sc in self.cores.items()
                 if sc["tree"] == c["tree"] and core_letter(sc["core_token"]) == letter]
        if len(group) <= 1:
            return [stem]
        return sorted(group, key=lambda s: section_number(self.cores[s]["core_token"]))

    def session(self):
        trees = []
        for t in self.trees:
            sel = t["selected"]
            done = self.store.rows.get(t["tree"])
            trees.append({
                "tree": t["tree"],
                "cores": t["cores"],
                "selected": sel,
                "has_image": bool(sel and self.cores[sel]["has_image"]),
                "n_rings": self.cores[sel]["n_boundaries"] if sel else 0,
                "oldest_year": self.cores[sel]["oldest_year"] if sel else None,
                "done": done is not None,
                "result": {"method": done["method"], "distance_mm": done["distance_mm"]}
                          if done else None,
            })
        first = next((i for i, t in enumerate(trees) if not t["done"]), 0)
        return {
            "folder": self.folder,
            "xlsx": self.store.xlsx_path,
            "trees": trees,
            "species": self.species,
            "start_index": first,
            "n_done": sum(1 for t in trees if t["done"]),
            "tool_version": TOOL_VERSION,
        }

    def core_detail(self, tree_id, stem=None):
        entry = next((t for t in self.trees if t["tree"] == tree_id), None)
        if entry is None:
            return None
        stem = stem or entry["selected"]
        if stem not in self.cores:
            return None
        c = self.cores[stem]
        res_um = c["res_um"]
        mm_per_px = (res_um / 1000.0) if res_um else None
        years = c["years"]
        rings = []
        for i, (z, t) in enumerate(zip(c["zpos"], c["theta"])):
            yr = years[i] if i < len(years) else None
            if yr is not None and not math.isfinite(yr):
                yr = None
            rings.append({"i": i, "zpos": float(z), "theta": float(t),
                          "year": int(yr) if yr is not None else None})
        siblings = []
        for s in entry["cores"]:
            sc = self.cores[s]
            siblings.append({
                "stem": s, "oldest_year": sc["oldest_year"],
                "n_rings": sc["n_boundaries"], "has_image": sc["has_image"],
                "accum_rw_mm": sc["accum_rw_mm"], "selected": s == stem,
            })

        # A core scanned in pieces has its ring width spread across several
        # files; case 3 needs the sum of all of them, not just the one opened.
        group = self.section_group(stem)
        sections = None
        accum_rw_mm_sum = None
        if len(group) > 1:
            sections = [{
                "stem": s,
                "accum_rw_mm": self.cores[s]["accum_rw_mm"],
                "oldest_year": self.cores[s]["oldest_year"],
            } for s in group]
            vals = [s["accum_rw_mm"] for s in sections]
            if any(v is not None for v in vals):
                accum_rw_mm_sum = round(sum(v for v in vals if v is not None), 3)

        return {
            "tree": tree_id,
            "stem": stem,
            "rings": rings,
            "res_um": res_um,
            "mm_per_px": mm_per_px,
            "n_boundaries": c["n_boundaries"],
            "n_widths": c["n_widths"],
            "oldest_year": c["oldest_year"],
            "newest_year": c["newest_year"],
            "fell_date": c["fell_date"],
            "accum_rw_px": c["accum_rw_px"],
            "accum_rw_mm": c["accum_rw_mm"],
            "sections": sections,
            "accum_rw_mm_sum": accum_rw_mm_sum,
            "n_missing": c["n_missing"],
            "n_broken": c["n_broken"],
            "has_image": c["has_image"],
            "siblings": siblings,
            "saved": self.store.rows.get(tree_id),
        }


# --------------------------------------------------------------------------
# HTTP

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "PithLocatorCT/" + TOOL_VERSION
    app = None

    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        body = json.dumps(_deep_clean(obj), allow_nan=False)
        self._send(code, body, "application/json; charset=utf-8")

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _static(self, rel):
        rel = rel.lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            return self._err(404, "not found")
        with open(path, "rb") as fh:
            body = fh.read()
        ext = os.path.splitext(path)[1].lower()
        self._send(200, body, MIME.get(ext, "application/octet-stream"))

    # -- routing

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._static("index.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if p == "/api/session":
                if q.get("rescan"):
                    self.app.rescan()
                return self._json(self.app.session())
            if p == "/api/core":
                tree = (q.get("tree") or [""])[0]
                stem = (q.get("stem") or [None])[0]
                d = self.app.core_detail(tree, stem)
                return self._json(d) if d else self._err(404, "unknown tree/core")
            if p == "/api/image":
                return self._image(q)
            if p == "/api/xlsx":
                path = self.app.store.xlsx_path
                if not os.path.isfile(path):
                    return self._err(404, "nothing saved yet")
                with open(path, "rb") as fh:
                    body = fh.read()
                return self._send(
                    200, body,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    {"Content-Disposition": 'attachment; filename="%s"' % RESULT_XLSX})
            return self._err(404, "no such endpoint")
        except Exception as exc:  # a crash here must not kill the session
            import traceback
            traceback.print_exc()
            return self._err(500, "%s: %s" % (type(exc).__name__, exc))

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._err(400, "malformed JSON body")
        try:
            if u.path == "/api/result":
                return self._save_result(payload)
            if u.path == "/api/result/delete":
                tree = payload.get("tree")
                if not tree:
                    return self._err(400, "no tree given")
                self.app.store.delete(tree)
                return self._json({"ok": True})
            if u.path == "/api/species":
                rows = payload.get("species") or []
                clean = []
                for r in rows:
                    try:
                        clean.append({"name": str(r["name"]).strip(),
                                      "sr": float(r["sr"]),
                                      "source": str(r.get("source", "user supplied"))})
                    except (KeyError, TypeError, ValueError):
                        continue
                if not clean:
                    return self._err(400, "no usable species rows")
                self.app.species = clean
                save_species(self.app.folder, clean)
                return self._json({"ok": True, "species": clean})
            return self._err(404, "no such endpoint")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return self._err(500, "%s: %s" % (type(exc).__name__, exc))

    # -- endpoints

    def _image(self, q):
        tree = (q.get("tree") or [""])[0]
        stem = (q.get("stem") or [None])[0]
        lo = float((q.get("lo") or ["200"])[0])
        hi = float((q.get("hi") or ["1200"])[0])
        d = self.app.core_detail(tree, stem)
        if d is None:
            return self._err(404, "unknown tree/core")
        core = self.app.cores[d["stem"]]
        if not core["has_image"]:
            return self._err(404, "no transverse preview (_Tv.tif) for %s" % d["stem"])
        data, info = self.app.cache.get(core["image_path"], lo, hi)
        self._send(200, data, "image/png", {
            "X-Image-Width": str(info["width"]),
            "X-Image-Height": str(info["height"]),
            "X-Image-Step": str(info["step"]),
        })

    def _save_result(self, payload):
        tree = payload.get("tree")
        method = payload.get("method")
        if not tree or method not in METHOD_LABEL:
            return self._err(400, "a tree and a valid method are required")
        d = self.app.core_detail(tree, payload.get("stem"))
        if d is None:
            return self._err(404, "unknown tree")
        core = self.app.cores[d["stem"]]

        row = {
            "tree": tree,
            "core": d["stem"],
            "other_cores": ", ".join(s for s in
                                     next(t["cores"] for t in self.app.trees
                                          if t["tree"] == tree) if s != d["stem"]),
            "method": METHOD_LABEL[method],
            "n_rings": core["n_boundaries"],
            "oldest_year": core["oldest_year"],
            "newest_year": core["newest_year"],
            "res_um": core["res_um"],
            "accum_ovendry_mm": _round(core["accum_rw_mm"], 3),
            "notes": (payload.get("notes") or "").strip(),
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "tool_version": TOOL_VERSION,
            "flag": "",
        }
        if core["zpos"] is not None and len(core["zpos"]):
            row["inner_zpos_px"] = _round(float(core["zpos"][0]), 3)
            row["inner_theta"] = _round(float(core["theta"][0]), 6)

        flags = []
        if method == "indicated":
            row["distance_mm"] = 0.0

        elif method == "concentric":
            for k in ("perp_mm", "euclid_mm", "pith_x_px", "pith_y_px"):
                row[k] = _round(_num(payload.get(k)), 4)
            if row["perp_mm"] is None:
                return self._err(400, "no measured distance in the payload")
            row["distance_mm"] = row["perp_mm"]
            if row["perp_mm"] < 0:
                flags.append("pith placed on the bark side of the innermost ring")

        else:  # geometric
            sr = _num(payload.get("sr"))
            bark = _num(payload.get("bark_mm"))
            diam = _num(payload.get("diameter_mm"))
            gap = _num(payload.get("outer_gap_mm")) or 0.0
            if sr is None or not (0.0 <= sr < 0.5):
                return self._err(400, "radial shrinkage must be between 0 and 0.5")
            if bark is None or bark < 0:
                return self._err(400, "bark thickness is required")
            if diam is None or diam <= 0:
                return self._err(400, "tree diameter is required")

            # A core scanned in sections has its ring width spread across
            # several files; sum them unless the operator asked to use only
            # the opened one. Falls back to the single file when there is only
            # one, so existing single-file data behaves exactly as before.
            sum_sections = payload.get("sum_sections")
            if sum_sections is None:
                sum_sections = True
            group = self.app.section_group(d["stem"]) if sum_sections else [d["stem"]]
            if len(group) <= 1:
                group = [d["stem"]]
            accum_vals = [self.app.cores[s]["accum_rw_mm"] for s in group]
            if not any(v is not None for v in accum_vals):
                return self._err(
                    400, "no pixel size in %s_ringwidth.txt, so the accumulated "
                         "ring width cannot be converted to mm" % d["stem"])
            accum = sum(v for v in accum_vals if v is not None)

            green = accum / (1.0 - sr)
            barkless_r = diam / 2.0 - bark
            dist = barkless_r - green - gap
            row.update({
                "species": payload.get("species") or "",
                "sr": sr, "sr_source": payload.get("sr_source") or "",
                "bark_mm": bark, "diameter_mm": diam,
                "diameter_input": payload.get("diameter_input") or "diameter",
                "barkless_radius_mm": _round(barkless_r, 3),
                "accum_ovendry_mm": _round(accum, 3),
                "accum_files": " + ".join(group),
                "accum_green_mm": _round(green, 3),
                "outer_gap_mm": gap,
                "distance_mm": _round(dist, 3),
            })
            if dist < 0:
                flags.append("negative offset: the rings already exceed the "
                             "barkless radius, check diameter and bark")
            elif dist > barkless_r * 0.9:
                flags.append("offset is over 90% of the barkless radius")

        row["distance_mm"] = _round(row.get("distance_mm"), 3)
        row["flag"] = "; ".join(flags)
        err = self.app.store.save(row)
        return self._json({"ok": True, "row": row, "warning": err,
                           "n_done": len(self.app.store.rows)})


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _round(v, n):
    return None if v is None else round(float(v), n)


def _deep_clean(o):
    """JSON-safe copy: numpy scalars unwrapped, NaN and Inf turned into null.

    json.dumps writes bare NaN by default, which JSON.parse rejects, so a single
    border-fracture year (deliberately NaN in _ringwidth.txt) would otherwise
    take the whole response down.
    """
    if isinstance(o, dict):
        return {k: _deep_clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_deep_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _deep_clean(o.tolist())
    if isinstance(o, (np.floating, np.integer, np.bool_)):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", default=".",
                    help="folder of RingIndicator output (default: current folder)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        sys.exit("Not a folder: %s" % folder)

    app = App(folder)
    n_trees = len(app.trees)
    if n_trees == 0:
        sys.stderr.write(
            "No cores found in %s.\nThe tool looks for RingIndicator sidecar files "
            "named '<core>_ring_and_fibre.txt'.\n" % folder)
    Handler.app = app

    # A tool that is quick to open is a tool that gets left running, so a busy
    # port should shift up rather than abort the session.
    httpd = None
    port = args.port
    for attempt in range(12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as exc:
            if exc.errno not in (98, 48, 10048):
                raise
            port += 1
    if httpd is None:
        sys.exit("Ports %d-%d are all busy. Pass --port to choose another."
                 % (args.port, port))
    if port != args.port:
        print("  note     port %d was busy, using %d" % (args.port, port))
    url = "http://127.0.0.1:%d/" % port
    print("PithLocatorCT %s" % TOOL_VERSION)
    print("  folder   %s" % folder)
    print("  trees    %d  (%d already done)" % (n_trees, len(app.store.rows)))
    print("  results  %s" % app.store.xlsx_path)
    print("  serving  %s   (Ctrl-C to stop)" % url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
