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
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

TOOL_VERSION = "1.2"
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


# --------------------------------------------------------------------------
# pixel size (microns per pixel)
#
# _ringwidth.txt column 3 is the usual source, but it is only as good as
# RingIndicator's own conversion of the TIFF's XResolution tag, and that
# conversion has a hole: readTiffTags.m handles ResolutionUnit 1 (none) and
# 3 (cm), but NOT 2 (inch) -- the ordinary default for a scanner or camera.
# For unit 2 the raw tag value is used unconverted, so a 1200 dpi scan is
# recorded as "1200 microns per pixel" in every row. Column 1 (the width
# itself) is in PIXELS, so this only ever corrupts the millimetre
# conversion, never the ring geometry.
#
# For a flat (rgb/gray) core there is no volume-derived escape hatch the way
# there is for CT, so column 3 is not trusted there at all -- see
# resolve_resolution. For CT it is still used, since no image is not the
# same thing as no resolution: the column 3 value stands, with a warning
# attached when it looks like a raw tag value rather than a pixel size.

# Default percentile window for the contrast stretch of a flat scan. A
# colour core scan almost never fills the full 0..255 range -- wood is a
# narrow band of browns -- so shown at its raw dtype range it looks washed
# out. Clipping half a percent off each tail restores the contrast without
# throwing away anything a person is looking at; the operator can change
# both ends from the HUD.
AUTO_PCT_LO = 0.5
AUTO_PCT_HI = 99.5

PLAUSIBLE_UM_PX = (0.5, 250.0)
COMMON_DPI = (72, 96, 150, 200, 240, 300, 400, 600, 720, 1200, 2400, 4800)


def read_resolution_txt(folder, stem):
    """<stem>_resolution.txt, written by RingIndicator's own Resolution menu
    (interSetTilt.m's set_resolution) or by us -- one number, microns per
    pixel. Also tried as "<stem>._resolution.txt", the name readTiffTags.m
    actually produces for a ".tiff" core (it builds the name by chopping the
    last 4 characters off the filename, not by fileparts)."""
    for suffix in ("_resolution.txt", "._resolution.txt"):
        path = os.path.join(folder, stem + suffix)
        if not os.path.isfile(path):
            continue
        arr = _read_numeric_table(path)
        if arr.size and np.isfinite(arr[0, 0]) and arr[0, 0] > 0:
            return float(arr[0, 0])
    return None


def write_resolution_txt(folder, stem, res_um):
    """Write <stem>_resolution.txt in the shape MATLAB's writematrix (and
    thus RingIndicator's own readmatrix) produces: one number, one line."""
    path = os.path.join(folder, stem + "_resolution.txt")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("%.6g\n" % float(res_um))
    os.replace(tmp, path)


def resolve_resolution(folder, stem, image_kind, rw_res_um, probe, override):
    """Pick the pixel size for one core, and explain the choice.

    Precedence:
      operator-entered override
      -> <stem>_resolution.txt
      -> <stem>_ringwidth.txt column 3 -- NOT for a flat (rgb/gray) core,
         which has no volume to fall back on if that column is wrong
      -> none

    Column 3 is never silently replaced by a heuristic guess: a value that
    looks like a raw DPI or an unconverted cm/inch tag is used exactly as
    written (when it is the resolved source) and flagged with `warning`,
    with `suggestions` offered for the operator to confirm, never applied
    on their own.
    """
    sidecar = read_resolution_txt(folder, stem)

    warning = ""
    suggestions = []

    def add_suggestion(value, why):
        if value and math.isfinite(value) and value > 0:
            suggestions.append({"res_um": round(value, 3), "why": why})

    xres = probe.get("xres") if probe else None
    res_unit = probe.get("res_unit") if probe else None

    if rw_res_um is not None and rw_res_um > 0:
        if res_unit == 2 and xres and abs(rw_res_um - xres) < 0.01:
            warning = ("%s_ringwidth.txt says %.4g \u00b5m/px, which is exactly this "
                       "TIFF's XResolution tag with ResolutionUnit=inch -- "
                       "RingIndicator left the raw tag value unconverted."
                       % (stem, rw_res_um))
            add_suggestion(25400.0 / xres, "25400 / %.4g dpi" % xres)
        elif not (PLAUSIBLE_UM_PX[0] <= rw_res_um <= PLAUSIBLE_UM_PX[1]):
            warning = ("%s_ringwidth.txt says %.4g \u00b5m/px, which is outside the "
                       "plausible range for a core scan." % (stem, rw_res_um))
            add_suggestion(1.0e4 / rw_res_um, "raw pixels-per-cm tag, unconverted")
        elif (abs(rw_res_um - round(rw_res_um)) < 1e-6
                and int(round(rw_res_um)) in COMMON_DPI):
            warning = ("%s_ringwidth.txt says %.4g \u00b5m/px, which is a common "
                       "scanner DPI value rather than a pixel size." % (stem, rw_res_um))
            add_suggestion(25400.0 / rw_res_um, "common scanner DPI")

    if sidecar is not None and rw_res_um is not None and rw_res_um > 0:
        if abs(sidecar - rw_res_um) / rw_res_um > 0.01:
            note = ("%s_resolution.txt says %.4g \u00b5m/px but %s_ringwidth.txt "
                    "says %.4g \u00b5m/px; using the sidecar."
                    % (stem, sidecar, stem, rw_res_um))
            warning = (warning + " " + note).strip() if warning else note

    if override is not None:
        res_um, source = override, "operator entered"
    elif sidecar is not None:
        res_um, source = sidecar, "%s_resolution.txt" % stem
    elif image_kind in ("rgb", "gray"):
        res_um, source = None, ""
    elif rw_res_um is not None and rw_res_um > 0:
        res_um, source = rw_res_um, "%s_ringwidth.txt" % stem
    else:
        res_um, source = None, ""

    return {"res_um": res_um, "source": source, "warning": warning,
            "suggestions": suggestions}


# --------------------------------------------------------------------------
# core image lookup

TV_PATTERNS = ("{s}_Tv.tif", "{s}_Tv.tiff", "{s}_TV.tif", "{s}_tv.tif")


def find_transverse(folder, stem):
    for pat in TV_PATTERNS:
        p = os.path.join(folder, pat.format(s=stem))
        if os.path.isfile(p):
            return p
    hits = sorted(glob.glob(os.path.join(folder, stem + "*Tv*.tif*")))
    return hits[0] if hits else None


# A colour or plain grayscale core scan carries no _Tv.tif -- RingIndicator
# never writes pre-saved planes for a flat image, only for a real volume it
# had to average slices out of (see CLAUDE.md). Its image is <stem>.tif
# itself, but only when that file is actually flat: a multi-page CT volume
# happening to be named <stem>.tif must never be mistaken for a preview, so
# every candidate is probed (page count, samples-per-pixel) before it is
# trusted. No glob here, deliberately: FLAT_PATTERNS matches the stem
# exactly, or CORE1.tif would match a glob meant for CORE12.tif.
FLAT_PATTERNS = ("{s}.tif", "{s}.tiff", "{s}.TIF")

_PROBE_CACHE = {}
_PROBE_CACHE_LOCK = threading.Lock()
_PROBE_CACHE_MAX = 4096

# Scanner/camera TIFFs commonly carry ResolutionUnit 1 (none) or 3 (cm),
# which readTiffTags.m converts to microns-per-pixel. Unit 2 (inch) is the
# one it does NOT convert -- neither branch fires and the raw DPI-ish tag
# value is used as if it already were microns-per-pixel. We mirror the
# conversion it DOES do, and deliberately do nothing for unit 2 (see
# resolve_resolution): guessing 25400/xres there would be exactly the silent
# substitution this feature exists to avoid.
def _tiff_resolution_um(tags, path):
    """(xres, resolution_unit, res_um_or_None) from TIFF tags, mirroring
    RingIndicator's readTiffTags.m. Never raises."""
    try:
        xres_tag = tags.get("XResolution")
        unit_tag = tags.get("ResolutionUnit")
        if xres_tag is None or unit_tag is None:
            return None, None, None
        xres = xres_tag.value
        if isinstance(xres, tuple):
            num, den = xres
            xres = float(num) / float(den) if den else 0.0
        else:
            xres = float(xres)
        unit = int(unit_tag.value)
        if not xres:
            return xres, unit, None
        if unit == 3:  # cm
            return xres, unit, round(1.0e4 / xres, 2)
        if unit == 1:  # "none" -- ImageJ writes real units this way
            descr = tags.get("ImageDescription")
            descr = descr.value if descr is not None else ""
            if "unit=micron" in (descr or ""):
                return xres, unit, round(1.0 / xres, 2)
            return xres, unit, round(1.0e4 / xres, 2)
        # unit == 2 (inch), or anything else: no safe conversion.
        return xres, unit, None
    except Exception:
        return None, None, None


def probe_tiff(path):
    """Inspect a TIFF's shape from its tags alone -- never reads pixel data.

    Returns a dict describing whether the file is FLAT (a single plane, safe
    to show as a preview) or a volume. Order matters: samples-per-pixel is
    checked before any second page is touched, and a single-page file is
    proven single-page by indexing page 1 and catching IndexError rather
    than by len(tf.pages) or tf.series, both of which walk every IFD -- on a
    multi-thousand-slice CT volume that is thousands of seeks per probe.
    Fails closed: any error leaves flat=False, never a false preview.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"flat": False, "why": "could not be read (%s)" % exc}

    key = (path, st.st_mtime_ns, st.st_size)
    with _PROBE_CACHE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached

    info = {"flat": False, "why": "could not be read", "spp": None,
            "dtype": None, "bits": None, "channels_first": False,
            "palette": False, "h": None, "w": None,
            "xres": None, "res_unit": None, "res_um_tag": None}
    try:
        with tifffile.TiffFile(path) as tf:
            page = tf.pages[0]
            spp = int(getattr(page, "samplesperpixel", 1) or 1)
            info["spp"] = spp
            info["dtype"] = str(page.dtype)
            info["bits"] = int(getattr(page, "bitspersample", 8) or 8)
            info["palette"] = (int(page.photometric) == 3)

            if spp < 3 and not info["palette"]:
                # Must prove there is only one plane. An ImageJ contiguous
                # stack can present as a single IFD with many images baked
                # into one strip, so check that before indexing page 1.
                if tf.is_imagej:
                    n_images = (tf.imagej_metadata or {}).get("images", 1)
                    if n_images and int(n_images) > 1:
                        info["why"] = "an ImageJ stack of %d slices" % int(n_images)
                        _probe_cache_put(key, info)
                        return info
                try:
                    tf.pages[1]
                except IndexError:
                    pass
                else:
                    info["why"] = "2 or more pages, so this is a volume"
                    _probe_cache_put(key, info)
                    return info

            axes = getattr(page, "axes", "") or ""
            info["channels_first"] = axes.startswith("S")
            shape = page.shape
            if info["channels_first"] and len(shape) >= 3:
                info["h"], info["w"] = int(shape[1]), int(shape[2])
            else:
                info["h"], info["w"] = int(shape[0]), int(shape[1])

            xres, unit, res_um = _tiff_resolution_um(page.tags, path)
            info["xres"], info["res_unit"], info["res_um_tag"] = xres, unit, res_um

            info["flat"] = True
            info["why"] = ""
    except Exception as exc:
        info = {"flat": False, "why": "could not be read (%s)" % exc,
                 "spp": None, "dtype": None, "bits": None,
                 "channels_first": False, "palette": False,
                 "h": None, "w": None,
                 "xres": None, "res_unit": None, "res_um_tag": None}

    _probe_cache_put(key, info)
    return info


def _probe_cache_put(key, info):
    with _PROBE_CACHE_LOCK:
        if len(_PROBE_CACHE) >= _PROBE_CACHE_MAX:
            _PROBE_CACHE.pop(next(iter(_PROBE_CACHE)))
        _PROBE_CACHE[key] = info


def find_core_image(folder, stem):
    """Bind a core to the image that previews it.

    Returns a dict {path, kind, file, reject, probe} -- "kind" is one of
    "ct_tv" (an existing _Tv.tif, unchanged behaviour), "rgb", "gray", or
    None. "path"/"file" are None when nothing usable was found; "reject"
    then explains why a candidate file was seen but not used (e.g. it is a
    volume), so the UI can say something better than "no image found" when
    the folder plainly contains a TIFF.
    """
    tv = find_transverse(folder, stem)
    if tv is not None:
        return {"path": tv, "kind": "ct_tv", "file": os.path.basename(tv),
                "reject": None, "probe": None}

    for pat in FLAT_PATTERNS:
        p = os.path.join(folder, pat.format(s=stem))
        if not os.path.isfile(p):
            continue
        probe = probe_tiff(p)
        if not probe["flat"]:
            return {"path": None, "kind": None, "file": None,
                    "reject": "%s is not a flat image (%s)"
                              % (os.path.basename(p), probe["why"]),
                    "probe": probe}
        spp = probe["spp"] or 1
        kind = "rgb" if (spp >= 3 or probe["palette"]) else "gray"
        return {"path": p, "kind": kind, "file": os.path.basename(p),
                "reject": None, "probe": probe}

    return {"path": None, "kind": None, "file": None, "reject": None, "probe": None}


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
#      A tree whose cores are NUMBERED can name the sections of one core with a
#      trailing letter instead, which is the same three parts in the other
#      order:
#
#          GHE-F015-1-A, GHE-F015-1-B  tree GHE-F015, core 1 in two sections
#          GHE-F015-2                  tree GHE-F015, core 2
#
#      That shape -- letters, digits, letter -- is indistinguishable from
#      KOR-014-A on its own, so like convention 2 below it is only accepted
#      when the folder corroborates it: some other stem must split, by the
#      plain separator rule, to the same tree with a purely NUMERIC core token
#      (GHE-F015-2 above). That is what says the numbers in this tree are core
#      ids rather than part of the tree name. Without it the name is left
#      whole, so KOR-014-A and KOR-014-B stay cores A and B of KOR-014.
#
#   2. The common dendrochronology convention with NO separator at all: a site
#      code and tree number (ending in a digit), then a one- or two-letter core
#      id, then an optional number if that core was scanned in several pieces:
#
#          ABC123A,  ABC123B           tree ABC123, cores A and B
#          ABC123A1, ABC123A2          tree ABC123, core A in two sections
#
# A2 and A3 -- or A1 and A2, or 1-A and 1-B -- are not two different cores: they
# are SECTIONS of one physical core that had to be scanned in pieces, sharing a
# tree and a core id. See section_group() below, which sums their ring width for
# case 3.
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
NUMBERED_TOKEN_RE = re.compile(r"^(?P<core>[0-9]{1,3})(?P<section>[A-Za-z]{1,2})$")


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
    same split_token() below.
    """
    m = STRUCTURAL_RE.match(stem)
    if not m:
        return None
    return m.group("tree"), m.group("letter") + m.group("section")


def parse_stem_numbered_section(stem):
    """Try the numbered-core, lettered-section rule: GHE-F015-1-A is section A
    of core 1 of tree GHE-F015. (tree, token) if the SHAPE fits, else None.

    Shape alone is not enough -- KOR-014-A has the same one -- so the caller
    only uses this when another stem in the folder corroborates it. See
    resolve_stem_names, and the module comment above.

    token is the core number with its section letter run together ("1A"), the
    same one-string shape the other two rules produce, so split_token() below
    can take all three.
    """
    sep = parse_stem_separator(stem)
    if sep is None:
        return None
    head, tail = sep
    if not tail.isalpha():
        return None
    inner = parse_stem_separator(head)
    if inner is None:
        return None
    tree, number = inner
    if not number.isdigit():
        return None
    return tree, number + tail


def split_token(token):
    """(core id, section number) for a core token, in either shape:

        "A"  -> ("A", 0)    core A, scanned whole
        "A2" -> ("A", 2)    core A, section 2
        "1"  -> ("1", 0)    core 1, scanned whole
        "1A" -> ("1", 1)    core 1, section A     (GHE-F015-1-A)

    A folder either letters its cores and numbers their sections or numbers its
    cores and letters their sections. Either way the first part is what the
    sections of one physical core share, and the second orders them -- see
    section_group(), which sums their ring width for case 3.

    So the number in a purely numeric token is a core id, not a section:
    GHE-Q003-1 and GHE-Q003-2 are two different cores, and each is its own
    group of one.
    """
    m = TOKEN_SPLIT_RE.match(token or "")
    if m:
        letter, section = m.group("letter"), m.group("section")
        return (letter or section), (int(section) if letter and section else 0)
    m = NUMBERED_TOKEN_RE.match(token or "")
    if m:
        n = 0
        for ch in m.group("section").upper():
            n = n * 26 + (ord(ch) - ord("A") + 1)
        return m.group("core"), n
    return "", 0


def core_id(token):
    """What the sections of one physical core share: "A2" -> "A", "1A" -> "1"."""
    return split_token(token)[0]


def section_number(token):
    """Which section of its core this is, 0 when the core is in one piece."""
    return split_token(token)[1]


def scan_folder(folder, res_overrides=None):
    """Discover cores, read their ring data, group into trees.

    res_overrides: {stem: res_um}, an operator-entered pixel size (see
    resolve_resolution / POST /api/resolution). Applying it here, in the one
    place accum_rw_mm is computed, means there is only ever one place that
    turns pixels into millimetres.
    """
    res_overrides = res_overrides or {}
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
        accum_px = float(np.nansum(np.array(rw["widths_px"]))) if rw["widths_px"] else 0.0

        img = find_core_image(folder, stem)
        resolved = resolve_resolution(folder, stem, img["kind"], rw["res_um"],
                                       img["probe"], res_overrides.get(stem))
        res_um = resolved["res_um"]

        cores[stem] = {
            "stem": stem,
            "n_boundaries": int(len(zpos)),
            "n_widths": rw["n_widths"],
            "oldest_year": oldest,
            "newest_year": newest,
            "res_um": res_um,
            "res_source": resolved["source"],
            "res_warning": resolved["warning"],
            "res_suggestions": resolved["suggestions"],
            "fell_date": rw["fell_date"],
            "accum_rw_px": accum_px,
            "accum_rw_mm": (accum_px * res_um / 1000.0) if res_um else None,
            "n_missing": rw["n_missing"],
            "n_broken": rw["n_broken"],
            "has_image": img["path"] is not None,
            "image_path": img["path"],
            "image_kind": img["kind"],
            "image_file": img["file"],
            "image_reject": img["reject"],
            "image_probe": img["probe"],
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

    # Trees whose cores the separator rule numbers: GHE-F015-2 makes GHE-F015
    # one of them. Only such a stem corroborates the numbered-core split, and
    # only for its own tree -- two candidates must not confirm each other, or
    # KOR-014-A and KOR-014-B would "confirm" a tree KOR.
    numbered_trees = set(tree for kind, tree, token in tentative.values()
                         if kind == "sep" and token.isdigit())

    resolved = {}
    for stem, (kind, tree, token) in tentative.items():
        if kind == "struct" and not confirmed:
            resolved[stem] = (stem, "")
            continue
        if kind == "sep":
            peel = parse_stem_numbered_section(stem)
            if peel is not None and peel[0] in numbered_trees:
                resolved[stem] = peel
                continue
        resolved[stem] = (tree, token)
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

def _pick_scale(arr):
    """"auto" for every colour image, whatever its dtype: an RGB core scan
    occupies a narrow slice of the available range, so the raw dtype range
    is a low-contrast render of a picture the operator has to read ring
    boundaries off. Grayscale keeps the older rule -- "dtype" for 8-bit/bool
    data (matching RingIndicator's im2double + a [0,1] display range for a
    flat image exactly), "auto" for anything wider, since a 16-bit scan
    windowed by raw dtype range renders as a uniform grey."""
    dt = arr.dtype
    if arr.ndim == 3:
        return "auto"
    if dt == np.bool_ or dt == np.uint8 or dt == np.int8:
        return "dtype"
    if np.issubdtype(dt, np.floating):
        finite = arr[np.isfinite(arr)]
        if finite.size and (finite.min() < -1e-6 or finite.max() > 1.0 + 1e-6):
            return "auto"
        return "dtype"
    return "auto"


def _scale_dtype_range(arr):
    """Scale to 0..255 by the dtype's own range -- MATLAB im2double's rule."""
    dt = arr.dtype
    if dt == np.bool_:
        return (arr.astype(np.uint8) * 255)
    if dt == np.uint8:
        return arr
    if dt == np.uint16:
        return (arr >> 8).astype(np.uint8)
    if dt == np.int8:
        return (arr.astype(np.int16) + 128).astype(np.uint8)
    if dt == np.int16:
        return ((arr.astype(np.int32) + 32768) >> 8).astype(np.uint8)
    if np.issubdtype(dt, np.floating):
        return (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
    if np.issubdtype(dt, np.integer):
        info = np.iinfo(dt)
        out = (arr.astype(np.float64) - info.min) * (255.0 / (info.max - info.min))
        np.clip(out, 0, 255, out=out)
        return out.astype(np.uint8)
    return arr.astype(np.uint8)


def _scale_auto(arr, plo=AUTO_PCT_LO, phi=AUTO_PCT_HI):
    """Percentile stretch, one window applied identically to every channel so
    colour balance is preserved -- the same one-window-for-all-channels idea
    as the CT density window, just derived from the data instead of typed in.
    [plo, phi] are percentiles, defaulting to AUTO_PCT_LO/AUTO_PCT_HI.

    The percentiles are taken over ALL channel values pooled, never over the
    luminance: on wood the channels sit far apart (a brown core is ~150/115/
    85) while each channel's own spread is a fraction of that, so a window
    cut from the luminance is narrower than the gap between channels and
    saturates red to white while crushing blue to black -- a stretch that
    destroys the hue it is meant to preserve. Pooled, the window spans what
    the image actually contains, and every channel keeps its offset inside
    it.

    Every colour preview comes through here, including the 8-bit scans that
    used to take the dtype-range path, so it stays cheap on a 100-megapixel
    flatbed scan: the percentiles come from a sample of rows (a full-width
    row every few rows describes a core's value distribution as well as all
    of them do) and the scaling runs in float32, which is ample for an
    8-bit result."""
    src = arr
    if src.shape[0] > 2048:
        src = src[:: src.shape[0] // 1024]
    if np.issubdtype(src.dtype, np.floating):
        src = src[np.isfinite(src)]
    if src.size:
        lo, hi = np.percentile(src, [plo, phi])
    else:
        lo, hi = 0.0, 1.0
    if hi <= lo:
        hi = lo + 1.0
    out = arr.astype(np.float32)
    out -= np.float32(lo)
    out *= np.float32(255.0 / (hi - lo))
    np.clip(out, 0, 255, out=out)
    return out.astype(np.uint8)


def render_png(image_path, kind, lo, hi, max_dim=16000, probe=None,
               plo=AUTO_PCT_LO, phi=AUTO_PCT_HI):
    """Preview PNG. "ct_tv" is the CT transverse plane, 8-bit grayscale,
    windowed to the density range [lo, hi] in kg/m3 -- unchanged from before
    RGB support. "rgb"/"gray" is a flat colour or grayscale core scan with no
    volume behind it; lo/hi are ignored there, and the pixels are windowed by
    a [plo, phi] percentile stretch -- always for colour, and for grayscale
    wider than 8-bit (see _pick_scale / _scale_auto) -- or otherwise by
    dtype range. Returns (png_bytes, (h, w) BEFORE downsampling, step,
    scale), where scale is "window" | "dtype" | "auto".
    """
    with tifffile.TiffFile(image_path) as tf:
        page = tf.pages[0]
        arr = page.asrgb() if (probe and probe.get("palette")) else page.asarray()

    if kind == "ct_tv":
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
        scale = "window"
    else:
        if arr.ndim == 3:
            if (probe and probe.get("channels_first")
                    and arr.shape[0] in (3, 4) and arr.shape[-1] not in (3, 4)):
                arr = np.moveaxis(arr, 0, -1)
            if arr.shape[-1] >= 4:
                arr = arr[..., :3]  # drop alpha
        h, w = arr.shape[0], arr.shape[1]
        step = 1
        if max(h, w) > max_dim:
            step = int(math.ceil(max(h, w) / float(max_dim)))
            arr = arr[::step, ::step]
        scale = _pick_scale(arr)
        out = (_scale_dtype_range(arr) if scale == "dtype"
               else _scale_auto(arr, plo, phi))
        img = Image.fromarray(out, mode=("RGB" if out.ndim == 3 else "L"))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=3)
    return buf.getvalue(), (h, w), step, scale


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

    def get(self, image_path, kind, lo, hi, probe=None,
            plo=AUTO_PCT_LO, phi=AUTO_PCT_HI):
        st = os.stat(image_path)
        # lo/hi are a CT density window and plo/phi a stretch of a flat
        # scan; each is meaningless for the other kind, so neither must
        # fragment the other's cache entries.
        ct = (kind == "ct_tv")
        lo_k, hi_k = (lo, hi) if ct else ("-", "-")
        plo_k, phi_k = ("-", "-") if ct else (plo, phi)
        key = hashlib.sha1(
            ("%s|%d|%d|%s|%s|%s|%s|%s"
             % (image_path, st.st_mtime_ns, st.st_size, kind,
                lo_k, hi_k, plo_k, phi_k)).encode()
        ).hexdigest()[:20]
        with self.lock:
            if key in self.meta and (self.dir is None or os.path.isfile(self._p(key))):
                return self._read(key), self.meta[key]
            data, shape, step, scale = render_png(image_path, kind, lo, hi,
                                                  probe=probe, plo=plo, phi=phi)
            info = {"height": shape[0], "width": shape[1], "step": step, "scale": scale}
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
    ("PixelSize_Source", "res_source"),
    ("Pith_X_px", "pith_x_px"),
    ("Pith_Y_px", "pith_y_px"),
    ("InnerRing_zpos_px", "inner_zpos_px"),
    ("InnerRing_theta_rad", "inner_theta"),
    ("Sr_Source", "sr_source"),
    ("Image_Kind", "image_kind"),
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
        # An operator-entered pixel size is normally persisted by writing
        # <stem>_resolution.txt, which the next rescan reads straight back --
        # RingIndicator's own sidecar, so both tools can see it. This dict
        # only holds a value when that write FAILED (a read-only folder),
        # so the session keeps working with it in memory. "a read-only
        # folder is no reason not to run" applies here too.
        self.res_overrides = {}
        self.rescan()

    def rescan(self):
        self.cores, self.trees = scan_folder(self.folder, self.res_overrides)

    def species_names(self):
        return [s["name"] for s in self.species]

    def section_group(self, stem):
        """Every core sharing this one's tree and core id, sorted by section
        number -- the pieces of one physical core that was scanned in parts.
        [stem] alone when its token names no core, or when it has no siblings.
        """
        c = self.cores.get(stem)
        if c is None:
            return [stem]
        cid = core_id(c["core_token"])
        if not cid:
            return [stem]
        group = [s for s, sc in self.cores.items()
                 if sc["tree"] == c["tree"] and core_id(sc["core_token"]) == cid]
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
                "image_kind": sc["image_kind"],
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

        image_expected = "%s_Tv.tif or %s.tif" % (stem, stem)
        return {
            "tree": tree_id,
            "stem": stem,
            "rings": rings,
            "res_um": res_um,
            "mm_per_px": mm_per_px,
            "res_source": c.get("res_source") or "",
            "res_warning": c.get("res_warning") or "",
            "res_suggestions": c.get("res_suggestions") or [],
            "res_editable": c["image_kind"] in ("rgb", "gray") or res_um is None,
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
            "image_kind": c["image_kind"],
            "image_file": c["image_file"],
            "image_reject": c["image_reject"],
            "image_expected": image_expected,
            "supports_window": c["image_kind"] == "ct_tv",
            "stretch_pct": [AUTO_PCT_LO, AUTO_PCT_HI],
            "siblings": siblings,
            "saved": self.store.rows.get(tree_id),
        }


# --------------------------------------------------------------------------
# HTTP

# --------------------------------------------------------------------------
# choosing another folder while the tool is running
#
# The dialog has to open on the machine running the server, which is the same
# machine as the browser -- this is a localhost tool -- so a native folder
# dialog is the honest picker. Tk is run in a SHORT-LIVED SUBPROCESS rather
# than in this process: tkinter insists on the main thread, and the request
# arrives on one of the ThreadingHTTPServer's worker threads, so an in-process
# dialog would either crash or need a main-thread queue for no gain. A
# subprocess also cannot take the server down with it, and leaves no Tk state
# behind between pickings.
#
# Exit code 3 means "no usable Tk here" (a Python built without tkinter, a
# headless Linux box); empty output means the dialog was cancelled.

_PICKER = r"""
import sys
try:
    import tkinter
    from tkinter import filedialog
except Exception:
    sys.exit(3)
try:
    root = tkinter.Tk()
except Exception:
    sys.exit(3)
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
kw = {"title": "Choose a folder of RingIndicator output", "mustexist": True}
if len(sys.argv) > 1 and sys.argv[1]:
    kw["initialdir"] = sys.argv[1]
try:
    path = filedialog.askdirectory(**kw)
finally:
    try:
        root.destroy()
    except Exception:
        pass
sys.stdout.write(path or "")
"""

PICK_TIMEOUT_S = 600  # the dialog waits for a person; only a hang is an error


def pick_folder_dialog(initialdir=None):
    """Open the OS folder dialog. Path chosen, "" if cancelled, None if there
    is no usable dialog on this machine (the caller then asks for a typed
    path)."""
    cmd = [sys.executable, "-c", _PICKER, initialdir or ""]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=PICK_TIMEOUT_S, **kw)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace").strip()


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "PithLocatorCT/" + TOOL_VERSION
    app = None
    # The app is one class attribute, so switching folders is a single atomic
    # rebind; the lock only keeps two switches from building an App each.
    switch_lock = threading.Lock()
    pick_lock = threading.Lock()

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
            if u.path == "/api/folder":
                return self._switch_folder(payload.get("path"))
            if u.path == "/api/folder/pick":
                return self._pick_folder()
            if u.path == "/api/resolution":
                return self._set_resolution(payload)
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

    def _pick_folder(self):
        """Ask the machine running the tool for a folder, with its own dialog."""
        if not self.pick_lock.acquire(False):
            return self._json({"busy": True})  # a dialog is already open
        try:
            path = pick_folder_dialog(self.app.folder)
        finally:
            self.pick_lock.release()
        if path is None:
            return self._json({"unavailable": True})
        if not path:
            return self._json({"cancelled": True})
        return self._json({"path": os.path.abspath(path)})

    def _switch_folder(self, path):
        """Serve a different folder from now on. Everything folder-derived --
        the scan, the preview cache, the species table, the results file --
        lives on the App, so a switch is one new App and one rebind. The old
        app stays bound if building the new one fails."""
        path = str(path or "").strip()
        if not path:
            return self._err(400, "no folder given")
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            return self._err(400, "Not a folder: %s" % path)
        with Handler.switch_lock:
            if os.path.normcase(path) == os.path.normcase(self.app.folder):
                self.app.rescan()
            else:
                app = App(path)
                Handler.app = app
                print("  folder   %s  (%d trees)" % (path, len(app.trees)))
        return self._json(self.app.session())

    def _image(self, q):
        tree = (q.get("tree") or [""])[0]
        stem = (q.get("stem") or [None])[0]
        d = self.app.core_detail(tree, stem)
        if d is None:
            return self._err(404, "unknown tree/core")
        core = self.app.cores[d["stem"]]
        kind = core["image_kind"]
        # lo/hi are a CT density window and meaningless for a flat colour or
        # grayscale scan; plo/phi are the percentile stretch of a flat scan
        # and meaningless for CT. A stale bookmarked URL with junk in any of
        # them must not 500, so parse defensively rather than a bare
        # float(), and fall back to the default whenever the pair does not
        # describe a usable window.
        lo = _num((q.get("lo") or [None])[0])
        hi = _num((q.get("hi") or [None])[0])
        if lo is None:
            lo = 200.0
        if hi is None:
            hi = 1200.0
        plo = _num((q.get("plo") or [None])[0])
        phi = _num((q.get("phi") or [None])[0])
        if (plo is None or phi is None or not 0.0 <= plo < phi <= 100.0):
            plo, phi = AUTO_PCT_LO, AUTO_PCT_HI
        if not core["has_image"]:
            reason = core.get("image_reject") or (
                "expected %s" % d.get("image_expected", "%s_Tv.tif or %s.tif" % (d["stem"], d["stem"])))
            return self._err(404, "no preview image for %s: %s" % (d["stem"], reason))
        data, info = self.app.cache.get(core["image_path"], kind, lo, hi,
                                        probe=core.get("image_probe"),
                                        plo=plo, phi=phi)
        mode = "RGB" if kind in ("rgb",) else "L"
        self._send(200, data, "image/png", {
            "X-Image-Width": str(info["width"]),
            "X-Image-Height": str(info["height"]),
            "X-Image-Step": str(info["step"]),
            "X-Image-Kind": kind or "",
            "X-Image-Mode": mode,
            "X-Image-Scale": info.get("scale") or "",
            "X-Image-Stretch": ("%g,%g" % (plo, phi)
                                if info.get("scale") == "auto" else ""),
        })

    def _set_resolution(self, payload):
        """POST /api/resolution -- an operator-entered pixel size (microns
        per pixel) for a core. Persisted by writing <stem>_resolution.txt,
        the same sidecar RingIndicator's own Resolution menu writes, so a
        rescan (and RingIndicator, for a TIFF with no resolution tag of its
        own) picks it straight back up. Applies to every section of the same
        physical core (see App.section_group) and, with scope="folder", to
        every other core in the folder that has no trustworthy size of its
        own yet -- typing the same number for every core of one scanning
        session is how this feature stops being used.
        """
        stem = payload.get("stem")
        tree = payload.get("tree")
        if not stem or stem not in self.app.cores:
            return self._err(404, "unknown core")

        raw = payload.get("res_um")
        clearing = "res_um" in payload and raw is None
        res_um = None if clearing else _num(raw)
        if not clearing and (res_um is None or not (0 < res_um < 10000)):
            return self._err(400, "pixel size must be a number between 0 and 10000 microns/px")

        stems = set(self.app.section_group(stem))
        if (payload.get("scope") or "core") == "folder":
            # Leave a core alone once it has a trustworthy size. A sidecar
            # or an earlier override is trustworthy even if a leftover
            # disagreement note against the (already-ignored, or -- for CT
            # -- superseded) _ringwidth.txt value still shows in res_warning
            # -- that note is for the operator to see, not a reason to
            # silently overwrite a value someone already fixed. Only "no
            # value at all" or "the _ringwidth.txt value itself looks wrong"
            # (the inch/DPI/magnitude checks, which only ever fire on the
            # source actually named "..._ringwidth.txt") count as suspect.
            for s, c in self.app.cores.items():
                sourced_from_ringwidth = (c["res_source"] or "").endswith("_ringwidth.txt")
                if c["res_um"] is None or (sourced_from_ringwidth and c["res_warning"]):
                    stems.add(s)

        warnings = []
        for s in stems:
            path = os.path.join(self.app.folder, s + "_resolution.txt")
            try:
                if clearing:
                    if os.path.isfile(path):
                        os.remove(path)
                else:
                    write_resolution_txt(self.app.folder, s, res_um)
                self.app.res_overrides.pop(s, None)
            except OSError as exc:
                if clearing:
                    warnings.append("could not remove %s (%s)" % (path, exc))
                else:
                    self.app.res_overrides[s] = res_um
                    warnings.append(
                        "could not write %s_resolution.txt (%s); kept for this session only"
                        % (s, exc))

        self.app.rescan()
        tree = tree or self.app.cores[stem]["tree"]
        d = self.app.core_detail(tree, stem)
        return self._json({
            "ok": True, "stems": sorted(stems), "core": d,
            "warning": "; ".join(warnings) if warnings else None,
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
            "res_source": core.get("res_source") or "",
            "image_kind": core.get("image_kind"),
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
        if core.get("res_warning"):
            flags.append(core["res_warning"])
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
                    400, "no pixel size for %s (none in _resolution.txt%s, and "
                         "none entered), so the accumulated ring width cannot "
                         "be converted to mm"
                         % (d["stem"], "" if core["image_kind"] in ("rgb", "gray")
                            else " or _ringwidth.txt"))
            accum = sum(v for v in accum_vals if v is not None)

            # accum_rw_mm_sum adds one value per section, each computed with
            # THAT section's own resolved pixel size -- if they were scanned
            # at different resolutions (or one was overridden and another
            # was not) the sum silently mixes scales. Worth a flag; it would
            # otherwise be invisible in the saved distance.
            group_res = [self.app.cores[s]["res_um"] for s in group
                         if self.app.cores[s]["res_um"]]
            if len(group_res) > 1 and (max(group_res) - min(group_res)) / min(group_res) > 0.01:
                flags.append("sections of this core have different pixel sizes (%s)"
                             % ", ".join("%s=%.3g" % (s, self.app.cores[s]["res_um"])
                                         for s in group if self.app.cores[s]["res_um"]))

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
