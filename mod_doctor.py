#!/usr/bin/env python3
"""mod_doctor.py -- Timberborn mod crash triage + duplicate cleanup.

Implements the diagnostic playbook in README.md (same folder). It:
  1. reads the Error-report zips (Version / Exception / Player log),
  2. classifies each crash into a known class,
  3. resolves the culprit mod against the ACTIVE mod folders,
  4. plans a safe action -- disable an error mod (-> _BUG/) or remove the
     older copy of a duplicate mod Id (-> __archives/YYYYMMDD-N/).

Default is a DRY RUN: it prints the diagnosis and the exact moves it would
make, and changes nothing. Pass --apply to perform the moves.

Safety guards (see the two "GUARD" comments below):
  * Cascade guard  -- never auto-disable a mod that other active mods list in
                      RequiredMods (foundational deps like Harmony/TimberUi).
  * Confidence gate -- auto-apply only high-confidence classes. The fragile
                      classes (MissingMethod, blueprint spec-key) are
                      report-only unless --force is given.

Nothing is ever hard-deleted; every move is recoverable and backed up.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, struct, subprocess, sys, time, zipfile
from collections import defaultdict
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _detect_mods_dir() -> Path:
    """Timberborn's user Mods dir. Override with $TIMBERBORN_MODS; else use the current
    directory when it looks like a Mods folder; else the standard Documents location."""
    env = os.environ.get("TIMBERBORN_MODS")
    if env:
        return Path(env).expanduser()
    cwd = Path.cwd()
    if cwd.name.lower() == "mods":
        return cwd
    try:
        if any((d / "manifest.json").exists() or any(d.glob("version-*"))
               for d in cwd.iterdir() if d.is_dir()):
            return cwd
    except OSError:
        pass
    return Path.home() / "Documents" / "Timberborn" / "Mods"


def _managed_from(path: Path) -> Path:
    """Accept either a Timberborn install root or its Managed dir; return Managed."""
    if path.name.lower() == "managed":
        return path
    return path / "Timberborn_Data" / "Managed"


def _mono_path_from_logs(er: Path) -> Path | None:
    """The 'Mono path[0]' the game recorded -- the exact Managed dir it ran from,
    read from the error-report zips and the live Player.log."""
    for z in (sorted(er.glob("error-report-*.zip"), reverse=True) if er.exists() else []):
        try:
            with zipfile.ZipFile(z) as zf:
                n = [x for x in zf.namelist() if x.startswith("2 Player log")]
                if not n:
                    continue
                log = zf.read(n[0]).decode("utf-8", "replace")
            m = re.search(r"Mono path\[0\] = '([^']+)'", log)
            if m and Path(m.group(1)).exists():
                return Path(m.group(1))
        except Exception:
            continue
    pl = Path.home() / "AppData" / "LocalLow" / "Mechanistry" / "Timberborn" / "Player.log"
    try:
        m = re.search(r"Mono path\[0\] = '([^']+)'",
                      pl.read_text(encoding="utf-8", errors="replace"))
        if m and Path(m.group(1)).exists():
            return Path(m.group(1))
    except Exception:
        pass
    return None


def _detect_game_dir(er: Path) -> Path:
    """Game Managed dir. Override with $TIMBERBORN_GAME; else the path the game logged;
    else the common Steam library / install locations across drives. Empty Path if not
    found -- callers guard on .exists()."""
    env = os.environ.get("TIMBERBORN_GAME")
    if env:
        return _managed_from(Path(env).expanduser())
    logged = _mono_path_from_logs(er)
    if logged:
        return logged
    roots = [Path(b) / "Steam" for b in
             filter(None, (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")))]
    for d in "CDEFGH":
        roots += [Path(f"{d}:/SteamLibrary"), Path(f"{d}:/Games"), Path(f"{d}:/Steam")]
    for r in roots:
        for cand in (r / "steamapps" / "common" / "Timberborn", r / "Timberborn"):
            m = cand / "Timberborn_Data" / "Managed"
            if m.exists():
                return m
    return Path("")


def resolve_paths(mods=None, game=None):
    """(mods_dir, error_reports_dir, game_managed_dir). Args override auto-detection."""
    mods_dir = Path(mods).expanduser() if mods else _detect_mods_dir()
    er = mods_dir.parent / "Error reports"
    game_dir = _managed_from(Path(game).expanduser()) if game else _detect_game_dir(er)
    return mods_dir, er, game_dir


MODS, ER, GAME = resolve_paths()
EXCLUDE = {"_BUG", "__archives", "tmp", ".ilspy-cache"}  # not mods: disabled, archived, scratch, cache
VERSION_DIR_RE = re.compile(r"(?i)^version-\d+\.\d+(?:\.\d+)*$")
# Steam-overlay panel-stack error is a game/Steam bug, not a mod fault.
TRANSIENT_MARKERS = ("SteamOverlayInputBlocker", "is not on top of the stack")
FOUNDATIONAL = {"Harmony", "TimberApi", "TimberUi", "eMka.ModSettings"}  # informational

# --------------------------------------------------------------------------- #
# Mod model
# --------------------------------------------------------------------------- #
def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments without touching markers inside JSON strings."""
    out = []
    i, in_string, escaped = 0, False, False
    while i < len(text):
        char = text[i]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def read_json(p: Path):
    # Timberborn's manifest parser is lenient; Python's json is strict. Match the
    # game so hand-authored manifests stay visible -- else they read as "not loaded"
    # and their dupes / culprit DLLs vanish from triage. Real case: BobCommuteBalancer,
    # whose multi-line Description (raw newlines in a string) broke strict parsing, so
    # its duplicate went undetected and the dup_id crash had no dedup action.
    #   strict=False -> allow raw newlines / control chars inside string values;
    #   comment strip -> tolerate mod.json JSONC comments;
    #   comma strip   -> tolerate a trailing comma before } or ].
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return None
    uncommented = _strip_json_comments(text)
    candidates = (text, uncommented,
                  re.sub(r",(\s*[}\]])", r"\1", text),
                  re.sub(r",(\s*[}\]])", r"\1", uncommented))
    for candidate in candidates:
        try:
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
    return None


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def _detect_game_version():
    """major.minor.build.rev of the installed game. Authoritative source is the game's
    own version.txt (always present in the install, e.g. '1.0.13.1-b769e88-sw'); the
    error reports are only a fallback for when the game path can't be found, and the
    hardcoded value a last resort. Reading it from reports alone is stale/wrong the
    moment the reports are cleared or the game is updated without a fresh crash."""
    # 1) Game's own stamp: Managed/../StreamingAssets/version.txt. Match only the
    #    leading dotted version, never the trailing build hash (its digits would
    #    corrupt the tuple via vtuple's findall).
    vfile = GAME.parent / "StreamingAssets" / "version.txt"
    try:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)",
                      vfile.read_text(encoding="utf-8", errors="replace"))
        if m:
            return vtuple(m.group(1))
    except Exception:
        pass
    # 2) Fallback: newest error report's recorded version.
    for z in sorted(ER.glob("error-report-*.zip"), reverse=True):
        try:
            with zipfile.ZipFile(z) as zf:
                for pre in ("0 Version", "2 Player log"):
                    n = [x for x in zf.namelist() if x.startswith(pre)]
                    if n:
                        m = re.search(r"(\d+\.\d+\.\d+\.\d+)",
                                      zf.read(n[0]).decode("utf-8", "replace"))
                        if m:
                            return vtuple(m.group(1))
        except Exception:
            continue
    return (1, 0, 12, 3)


GAMEV = _detect_game_version()


def loadable(mod: dict) -> bool:
    """The game loads the highest version-N dir whose version is <= the installed
    game version (proven: a version-0.7-only mod loaded on game 1.0.12.3; a
    version-1.0.13.0 / 1.1 mod did NOT). A root-manifest mod (no version dirs,
    e.g. Harmony) always loads. NOTE: this is stricter/more accurate than the
    README's "exact major.minor" shorthand -- version-1.1 stays dormant, but a
    lower version-0.x is still loadable when nothing closer exists."""
    vds = mod["version_dirs"]
    if not vds:
        return True
    return any(vtuple(n) and vtuple(n) <= GAMEV for n in vds)


def scan_mod(folder: Path) -> dict:
    dlls, jsons = [], []
    for dp, _dn, fs in os.walk(folder):
        for f in fs:
            fp = Path(dp) / f
            if f.lower().endswith(".dll"):
                dlls.append(fp)
            elif f.lower().endswith(".json"):
                jsons.append(fp)
    roots = {p.name for p in folder.iterdir() if p.is_file()}
    vdir_names = sorted(p.name for p in folder.iterdir()
                        if p.is_dir() and re.match(r"(?i)version[-.]", p.name))
    loadable_vdirs = [n for n in vdir_names if vtuple(n) and vtuple(n) <= GAMEV]
    if not vdir_names:
        loaded_root = folder                         # flat mod (manifest checked below)
    else:
        # Highest loadable version-N that actually HAS a manifest.json. The game
        # skips a higher but empty/manifest-less version dir (e.g. TimberApi's empty
        # version-1.0 -> it loads from version-0.7).
        loaded_root = None
        for n in sorted(loadable_vdirs, key=vtuple, reverse=True):
            if (folder / n / "manifest.json").exists():
                loaded_root = folder / n
                break
    # Timberborn loads a mod ONLY via manifest.json at the loaded root. It ignores
    # mod.json and manifests nested deeper -> those log "No manifest file found"
    # and never load, so they are neither culprits nor real duplicates.
    manifest = read_json(loaded_root / "manifest.json") if loaded_root else None
    is_loaded = isinstance(manifest, dict)
    mid = manifest.get("Id") if is_loaded else None
    mname = manifest.get("Name") if is_loaded else None
    mver = manifest.get("Version") if is_loaded else None
    required = set()
    if is_loaded:
        for r in manifest.get("RequiredMods", []) or []:
            rid = r.get("Id") if isinstance(r, dict) else r
            if rid:
                required.add(rid)
    if is_loaded:
        pref = str(loaded_root).lower()
        loaded_dlls = [d for d in dlls
                       if str(d).lower() == pref or str(d).lower().startswith(pref + os.sep)]
    else:
        loaded_dlls = []
    return {
        "folder": folder,
        "name": folder.name,
        "is_loaded": is_loaded,
        "ids": [mid] if mid else [],
        "names": [mname] if mname else [],
        "best_version": vtuple(mver),
        "mtime": folder.stat().st_mtime,
        "required": required,
        "steam": "workshop_data.json" in roots,
        "modio": "mod_manager_manifest.json" in roots,
        "version_dirs": vdir_names,
        "dlls": dlls,
        "loaded_dlls": loaded_dlls,
        "jsons": jsons,
    }


def active_mods() -> list[dict]:
    return [scan_mod(MODS / d) for d in sorted(os.listdir(MODS))
            if (MODS / d).is_dir() and d not in EXCLUDE]


def _looks_external(folder: Path) -> bool:
    """The game's ExternalModFinder loads BepInEx-style packs separately (a winhttp.dll
    + doorstop shim at the folder root); wrapping one into a version dir breaks it."""
    try:
        names = {p.name.lower() for p in folder.iterdir()}
    except Exception:
        return False
    return "bepinex" in names or "winhttp.dll" in names or "doorstop_config.ini" in names


def normalize_version_dirs(apply: bool) -> list[dict]:
    """Give each FLAT native mod a version-<MinimumGameVersion> dir so the game's own
    loader selects it. Per Timberborn.Modding.ModRepository: if a mod folder has any
    'version-*' subdir the root is ignored, and the game loads the HIGHEST version-*
    whose version is <= the current game version (else the mod is skipped). The version
    is READ from the mod's manifest.json MinimumGameVersion -- NEVER hardcoded to
    version-1.0. A folder whose target version can't be determined is left untouched and
    reported: no readable manifest.json (old mod.json / non-native), an external
    framework (BepInEx), or a manifest without MinimumGameVersion. Returns structured
    findings; performs the moves only when apply=True."""
    game = ".".join(map(str, GAMEV))
    items = []
    for name in sorted(os.listdir(MODS)):
        folder = MODS / name
        if not folder.is_dir() or name in EXCLUDE or name.startswith("_"):
            continue
        if any(p.is_dir() and VERSION_DIR_RE.fullmatch(p.name) for p in folder.iterdir()):
            continue  # already versioned -> the game selects the right dir
        if _looks_external(folder):
            items.append({"sev": "skip", "label": f"skip {name}",
                          "detail": "external framework (BepInEx) -> the game loads it "
                                    "separately; never wrapped into a version dir"})
            continue
        man = read_json(folder / "manifest.json")
        if not isinstance(man, dict):
            if (folder / "mod.json").exists():
                detail = ("legacy TimberAPI package -> a version directory alone cannot port "
                          "its schema or compiled code. See LEGACY COMPATIBILITY; "
                          "--repair-legacy migrates supported data-only Specifications to "
                          "native 1.x Blueprints.")
                items.append({"sev": "warn", "label": f"migration needed {name}",
                              "detail": detail})
            else:
                detail = ("no manifest.json or mod.json at root -> not a loadable mod (a stray "
                          "folder, or a mod nested one level too deep); not wrapping")
                items.append({"sev": "skip", "label": f"skip {name}", "detail": detail})
            continue
        mgv = man.get("MinimumGameVersion")
        if not (mgv and vtuple(mgv)):
            items.append({"sev": "skip", "label": f"skip {name}",
                          "detail": "manifest.json has no MinimumGameVersion -> target game "
                                    "version unknown; not wrapping"})
            continue
        suffix = str(mgv).strip()
        vdir = folder / f"version-{suffix}"
        dormant = vtuple(mgv) > GAMEV
        detail = f"MinimumGameVersion {suffix} -> version-{suffix}"
        if dormant:
            detail += (f"  (DORMANT: needs game >= {suffix}, you have {game} -> stays "
                       "unloaded until the game is updated)")
        if apply and not vdir.exists():
            vdir.mkdir()
            for child in list(folder.iterdir()):
                if child == vdir or child.name.lower() == "mod_manager_manifest.json":
                    continue
                shutil.move(str(child), str(vdir / child.name))
        items.append({"sev": "warn" if dormant else "ok",
                      "label": f"{'wrapped' if apply else 'wrap'} {name} -> version-{suffix}",
                      "detail": detail})
    return items


# --------------------------------------------------------------------------- #
# Static game-assembly compatibility (AssemblyRef scan)
# --------------------------------------------------------------------------- #
def _assembly_refs(path: Path) -> set[str]:
    """Referenced assembly NAMES from a .NET DLL's AssemblyRef table (ECMA-335 II.22).
    Authoritative -- unlike a byte-substring it lists true external dependencies and
    never mistakes a namespace/type string for an assembly. Empty set on a non-.NET
    file or any parse issue, so the caller degrades gracefully."""
    try:
        data = path.read_bytes()
        if data[:2] != b"MZ":
            return set()
        e = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e:e + 4] != b"PE\x00\x00":
            return set()
        coff = e + 4
        nsec = struct.unpack_from("<H", data, coff + 2)[0]
        optsz = struct.unpack_from("<H", data, coff + 16)[0]
        opt = coff + 20
        pe32plus = struct.unpack_from("<H", data, opt)[0] == 0x20B
        dd = opt + (112 if pe32plus else 96)
        cli_rva = struct.unpack_from("<I", data, dd + 14 * 8)[0]
        secs = []
        for i in range(nsec):
            b = opt + optsz + i * 40
            vs = struct.unpack_from("<I", data, b + 8)[0]
            va = struct.unpack_from("<I", data, b + 12)[0]
            rs = struct.unpack_from("<I", data, b + 16)[0]
            raw = struct.unpack_from("<I", data, b + 20)[0]
            secs.append((va, vs, raw, rs))

        def off(rva):
            for va, vs, raw, rs in secs:
                if va <= rva < va + max(vs, rs):
                    return raw + (rva - va)
            return None

        cli = off(cli_rva)
        md = off(struct.unpack_from("<I", data, cli + 8)[0])
        if data[md:md + 4] != b"BSJB":
            return set()
        vlen = struct.unpack_from("<I", data, md + 12)[0]
        p = md + 16 + ((vlen + 3) & ~3) + 2
        nstreams = struct.unpack_from("<H", data, p)[0]
        p += 2
        st = {}
        for _ in range(nstreams):
            o, s = struct.unpack_from("<II", data, p)
            p += 8
            a = p
            while data[p] != 0:
                p += 1
            st[data[a:p].decode("ascii", "replace")] = (md + o, s)
            p = (p + 4) & ~3
        if "#~" not in st or "#Strings" not in st:
            return set()
        to = st["#~"][0]
        so = st["#Strings"][0]
        hs = data[to + 6]
        sw = 4 if hs & 1 else 2
        gw = 4 if hs & 2 else 2
        bw = 4 if hs & 4 else 2
        valid = struct.unpack_from("<Q", data, to + 8)[0]
        present = [i for i in range(64) if (valid >> i) & 1]
        rp = to + 24
        rows = {}
        for i in present:
            rows[i] = struct.unpack_from("<I", data, rp)[0]
            rp += 4

        def Rt(t):
            return 4 if rows.get(t, 0) >= (1 << 16) else 2

        CODED = {
            "TypeDefOrRef": ([2, 1, 27], 2), "HasConstant": ([4, 8, 23], 2),
            "HasCustomAttribute": ([6, 4, 1, 2, 8, 9, 10, 0, 20, 17, 26, 27, 32, 35,
                                    38, 39, 40, 42, 43, 44], 5),
            "HasFieldMarshal": ([4, 8], 1), "HasDeclSecurity": ([2, 6, 32], 2),
            "MemberRefParent": ([2, 1, 26, 6, 27], 3), "HasSemantics": ([20, 23], 1),
            "MethodDefOrRef": ([6, 10], 1), "MemberForwarded": ([4, 6], 1),
            "Implementation": ([38, 35, 39], 2),
            "CustomAttributeType": ([-1, -1, 6, 10, -1], 3),
            "ResolutionScope": ([0, 26, 35, 1], 2), "TypeOrMethodDef": ([2, 6], 1),
        }

        def Cs(n):
            tabs, bits = CODED[n]
            mx = max((rows.get(t, 0) for t in tabs if t >= 0), default=0)
            return 4 if mx >= (1 << (16 - bits)) else 2

        def colsize(c):
            if c == "u8":
                return 1
            if c == "u16":
                return 2
            if c == "u32":
                return 4
            if c == "S":
                return sw
            if c == "G":
                return gw
            if c == "B":
                return bw
            if isinstance(c, tuple):
                return Rt(c[1])
            return Cs(c)

        SCH = {
            0: ["u16", "S", "G", "G", "G"], 1: ["ResolutionScope", "S", "S"],
            2: ["u32", "S", "S", "TypeDefOrRef", ("R", 4), ("R", 6)], 3: [("R", 4)],
            4: ["u16", "S", "B"], 5: [("R", 6)],
            6: ["u32", "u16", "u16", "S", "B", ("R", 8)], 7: [("R", 8)],
            8: ["u16", "u16", "S"], 9: [("R", 2), "TypeDefOrRef"],
            10: ["MemberRefParent", "S", "B"], 11: ["u8", "u8", "HasConstant", "B"],
            12: ["HasCustomAttribute", "CustomAttributeType", "B"],
            13: ["HasFieldMarshal", "B"], 14: ["u16", "HasDeclSecurity", "B"],
            15: ["u16", "u32", ("R", 2)], 16: ["u32", ("R", 4)], 17: ["B"],
            18: [("R", 2), ("R", 20)], 19: [("R", 20)],
            20: ["u16", "S", "TypeDefOrRef"], 21: [("R", 2), ("R", 23)], 22: [("R", 23)],
            23: ["u16", "S", "B"], 24: ["u16", ("R", 6), "HasSemantics"],
            25: [("R", 2), "MethodDefOrRef", "MethodDefOrRef"], 26: ["S"], 27: ["B"],
            28: ["u16", "MemberForwarded", "S", ("R", 26)], 29: ["u32", ("R", 4)],
            30: ["u32", "u32"], 31: ["u32"],
            32: ["u32", "u16", "u16", "u16", "u16", "u32", "B", "S", "S"],
            33: ["u32"], 34: ["u32", "u32", "u32"],
            35: ["u16", "u16", "u16", "u16", "u32", "B", "S", "S", "B"],
        }
        cur = rp
        for t in present:
            if t not in SCH:
                if t < 35:
                    return set()
                break
            rsz = sum(colsize(c) for c in SCH[t])
            if t == 35:
                out = set()
                for r in range(rows[t]):
                    base = cur + r * rsz + 8 + 4 + bw  # skip Version(8)+Flags(4)+PublicKey(blob)
                    nidx = struct.unpack_from("<I" if sw == 4 else "<H", data, base)[0]
                    a = so + nidx
                    b2 = a
                    while data[b2] != 0:
                        b2 += 1
                    out.add(data[a:b2].decode("utf-8", "replace"))
                return out
            cur += rows[t] * rsz
        return set()
    except Exception:
        return set()


def compat_report(mods) -> list[dict]:
    """Proactive counterpart to crash triage: WITHOUT a crash report, scan each loaded
    mod's DLLs (AssemblyRef table) for assemblies nothing installed provides.
      * missing 'Timberborn.*' GAME assembly -> HARD: its types can't load, the game
        crashes on that mod (version mismatch -- align the GAME).
      * missing MOD assembly -> SOFT: .NET binds refs lazily, so the mod still loads and
        only a feature that touches the type breaks (a likely-missing dependency mod).
    'provided' spans the game plus every dll shipped by any installed mod, so a
    dependency in a sibling/version folder isn't falsely flagged. Returns items."""
    if not GAME.exists():
        return [{"sev": "info", "label": "compat scan skipped",
                 "detail": "game Managed dir not found"}]
    provided = {d.stem for d in GAME.glob("*.dll")}
    for m in mods:
        for d in m["dlls"]:
            provided.add(d.stem)
    game_gaps, dep_gaps = defaultdict(list), defaultdict(list)
    for m in mods:
        if not m["is_loaded"]:
            continue
        miss = set()
        for d in m["loaded_dlls"]:
            miss |= {r for r in _assembly_refs(d) if r not in provided}
        for a in sorted(miss):
            (game_gaps if a.startswith("Timberborn.") else dep_gaps)[a].append(m["name"])
    gv = ".".join(map(str, GAMEV))
    items = []
    if game_gaps:
        for m in sorted({m for c in game_gaps.values() for m in c}):
            need = sorted(a for a, c in game_gaps.items() if m in c)
            items.append({"sev": "crash", "label": f"{m} needs a missing GAME assembly",
                          "detail": "unresolved: " + ", ".join(need)
                                    + "\ntypes won't load -> the game crashes on this mod; "
                                      "align the GAME, don't disable it"})
        for a in sorted(x for x in game_gaps if len(game_gaps[x]) >= 4):
            items.append({"sev": "crash",
                          "label": f"VERSION MISMATCH: '{a}' absent, needed by {len(game_gaps[a])} mods",
                          "detail": "align the GAME (update/branch); disabling would strip "
                                    "foundational mods and their trees"})
    else:
        items.append({"sev": "ok", "label": f"no missing GAME assembly on game {gv}",
                      "detail": "every loaded mod binds against the installed game assemblies "
                                "-> no load crash"})
    for a in sorted(dep_gaps):
        items.append({"sev": "warn", "label": f"soft: {a} missing",
                      "detail": "referenced by " + ", ".join(sorted(set(dep_gaps[a])))
                                + "\nlazy-bound: the mod loads, only a feature that uses it "
                                  "breaks -> install that dependency mod (NOT a crash)"})
    return items


def known_assemblies(mods) -> set[str]:
    names = {dll.stem for dll in GAME.glob("*.dll")} if GAME.exists() else set()
    for m in mods:
        for dll in m["dlls"]:
            names.add(dll.stem)
    return names


def source_note(m: dict) -> str:
    if m["steam"] and m["modio"]:
        return "SYNCED (Steam+mod.io): unsubscribe on BOTH or it re-downloads"
    if m["steam"]:
        return "SYNCED (Steam Workshop): unsubscribe in-game/Workshop or it re-downloads"
    if m["modio"]:
        return "SYNCED (mod.io): disable in the in-game Mods manager or it re-downloads"
    return "manual install: move is durable"


def fmt_ver(m):
    return ".".join(map(str, m["best_version"])) or "?"


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------- #
# Culprit resolution
# --------------------------------------------------------------------------- #
def owners_of_token(token: str, mods, in_dll=True, in_json=False, discrete=False) -> list[dict]:
    # discrete=True matches the token only as a standalone, null-delimited .NET
    # #Strings entry ("\x00<tok>\x00") -- i.e. a real type/member name -- so a short
    # leaf like 'ModVersion' can't false-match a substring such as the property
    # getter 'get_ModVersion'. Trade-off: a genuine but suffix-merged reference is
    # missed, which errs to report-only (safe), never a wrong auto-disable.
    tb = (b"\x00" + token.encode("utf-8", "replace") + b"\x00") if discrete \
        else token.encode("utf-8", "replace")
    hit = []
    for m in mods:
        files = list(m["loaded_dlls"]) if in_dll else []
        if in_json:
            files += m["jsons"]
        for fp in files:
            try:
                if tb in fp.read_bytes():
                    hit.append(m)
                    break
            except Exception:
                pass
    return hit


# --------------------------------------------------------------------------- #
# Report loading + classification
# --------------------------------------------------------------------------- #
def load_report(zp: Path):
    with zipfile.ZipFile(zp) as zf:
        def rd(prefix):
            n = [x for x in zf.namelist() if x.startswith(prefix)]
            return zf.read(n[0]).decode("utf-8", "replace") if n else ""
        return rd("0 Version"), rd("1 Exception"), rd("2 Player log")


PLAYER_LOG = Path.home() / "AppData" / "LocalLow" / "Mechanistry" / "Timberborn" / "Player.log"
_EXC_HEAD_RE = re.compile(r"^[A-Za-z][\w.]*(?:Exception|Error):")


def scan_player_log():
    """Fallback crash source for when the error-report zips are gone (the game writes
    every session to Player.log regardless). Return (name, exc, log) for the LAST
    uncaught exception whose stack aborts mod loading (ModCodeStarter /
    LoadModsAndStartGame / GetModStarters), or None if the log is absent or booted
    clean. classify() also scans the full log, so an approximate exc block suffices."""
    try:
        text = PLAYER_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()
    heads = [i for i, l in enumerate(lines) if _EXC_HEAD_RE.match(l.strip())]
    fatal = None
    for i in heads:
        if any(k in "\n".join(lines[i:i + 60])
               for k in ("ModCodeStarter", "LoadModsAndStartGame", "GetModStarters")):
            fatal = i  # keep the most recent
    if fatal is None:
        return None
    end = fatal + 1
    while end < len(lines) and end < fatal + 80 and not _EXC_HEAD_RE.match(lines[end].strip()):
        end += 1
    return ("Player.log", "\n".join(lines[fatal:end]), text)


def _report_stamp(name: str) -> str:
    """Short label for a report source: the timestamp of an error-report zip, else the
    name as-is (e.g. 'Player.log')."""
    return name[13:-4] if name.startswith("error-report-") and name.endswith(".zip") else name


def classify(exc: str, log: str) -> dict:
    head = exc.strip().splitlines()[0] if exc.strip() else ""
    text = exc + "\n" + log

    if any(t in head for t in TRANSIENT_MARKERS):
        return {"cls": "transient", "conf": "n/a",
                "summary": "Steam overlay panel-stack bug (not a mod)"}

    m = re.search(r"same key has already been added\. Key:\s*(\S+)", head)
    if m and ("ToDictionary" in exc or "ModUpdateService" in exc):
        return {"cls": "dup_id", "conf": "high", "key": m.group(1),
                "summary": f"Duplicate enabled mod Id '{m.group(1)}'"}

    m = re.search(r"No type found for key\s+(\w+Spec)", head)
    if m:
        return {"cls": "spec_key", "conf": "low", "key": m.group(1),
                "summary": f"Removed/renamed blueprint spec key '{m.group(1)}'"}

    m = re.search(r"Could not load type of field '([\w.]+):", exc)
    if m:
        asm = re.search(r"Could not load file or assembly '([^,']+)", exc)
        return {"cls": "typeload", "conf": "high", "type": m.group(1),
                "asm": asm.group(1) if asm else None,
                "summary": f"TypeLoadException on {m.group(1)}"
                           + (f" (needs missing '{asm.group(1)}')" if asm else "")}

    # Only when the resolve-type-token message IS the head (a pure Mono
    # TypeLoadException). A ReflectionTypeLoadException carries the same phrasing on
    # LATER lines but is better served by the missing_asm branch below (which adds
    # assembly-consumer fallback), so match head, not the whole exception.
    m = re.search(r"Could not resolve type with token \w+ from typeref "
                  r"\(expected class '([^']+)' in assembly '([^,']+)", head)
    if m and "ReflectionTypeLoadException" not in head:
        typ, asm = m.group(1), m.group(2).strip()
        return {"cls": "typeref_missing", "conf": "high", "type": typ, "asm": asm,
                "summary": f"TypeLoadException: type '{typ}' no longer exists in "
                           f"assembly '{asm}' (a loaded mod was built against a "
                           f"different game API version)"}

    if "transpiler" in head or "Patching exception" in text or "HarmonyLib" in text:
        m = (re.search(r"Failed to apply ([\w.]+) transpiler", head)
             or re.search(r"Patching exception in method [\w.]+ ([\w.]+)::", text)
             or re.search(r"\bin ([\w.]+Patches?)", text))
        return {"cls": "harmony", "conf": "high", "target": m.group(1) if m else None,
                "summary": f"Harmony patch/transpiler failure ({m.group(1) if m else '?'})"}

    m = re.search(r"Method not found:\s*(.+)", head)
    if m:
        return {"cls": "missing_method", "conf": "low", "sig": m.group(1).strip(),
                "summary": f"MissingMethodException: {m.group(1).strip()[:70]}"}

    if "ReflectionTypeLoadException" in head or "Could not load file or assembly" in text:
        asm = re.search(r"Could not load file or assembly '([^,']+)", text)
        tref = re.search(r"expected class '([^']+)'", text)
        asmn = asm.group(1) if asm else None
        trefn = tref.group(1) if tref else None
        summ = "ReflectionTypeLoadException"
        if trefn:
            summ += f": unresolved type '{trefn}'"
        if asmn:
            summ += f" / missing assembly '{asmn}'"
        return {"cls": "missing_asm", "conf": "med", "asm": asmn, "typeref": trefn,
                "summary": summ}

    return {"cls": "unknown", "conf": "n/a", "summary": head[:90] or "(empty)"}


def owners_of_dotted(dotted: str, mods) -> list:
    """Resolve a dotted type/class name (e.g. 'NS.Sub.Type', 'PatchClass.Method')
    to owning mods. .NET metadata stores the namespace and each type name as
    SEPARATE #Strings-heap entries, so the full dotted string is never contiguous
    in the DLL -- searching it verbatim always misses. The ROOT segment (a mod's
    top namespace or the patch class) IS contiguous and globally distinctive, so
    match on that."""
    root = dotted.split(".")[0]
    return owners_of_token(root, mods) if len(root) > 5 else []


def provides_assembly(m: dict, asm: str) -> bool:
    """True if this mod SHIPS <asm>.dll -> it PROVIDES the assembly, so a bind
    failure against it points at a consumer, not this provider."""
    tgt = (asm + ".dll").lower()
    return any(d.name.lower() == tgt for d in m["loaded_dlls"])


def consumers_of_assembly(asm: str, mods) -> list:
    """Loaded mods that reference <asm> but don't provide it -> bind-failure suspects."""
    return [m for m in owners_of_token(asm, mods) if not provides_assembly(m, asm)]


# --------------------------------------------------------------------------- #
# Optional IL precision (ILSpy) -- disambiguate a break by reading the mod's
# actual call sites, not just a name-substring. Absent ilspycmd -> degrade to
# name matching (callers guard on ILSPYCMD).
# --------------------------------------------------------------------------- #
ILSPYCMD = shutil.which("ilspycmd")
_DECOMPILE_CACHE: dict[str, str] = {}


def decompile(dll: Path) -> str:
    """Decompiled C# of a DLL via ilspycmd. Cached in-process AND on disk (keyed by
    path+size+mtime) so repeat --apply runs are instant. '' on absence/failure/timeout."""
    if not ILSPYCMD:
        return ""
    key = str(dll)
    if key in _DECOMPILE_CACHE:
        return _DECOMPILE_CACHE[key]
    cf = None
    try:
        st = dll.stat()
        cdir = MODS / ".ilspy-cache"
        cdir.mkdir(exist_ok=True)
        cf = cdir / f"{dll.stem}__{st.st_size}__{st.st_mtime_ns}.cs"
        if cf.exists():
            src = cf.read_text(errors="replace")
            _DECOMPILE_CACHE[key] = src
            return src
    except Exception:
        cf = None
    try:
        r = subprocess.run([ILSPYCMD, key], capture_output=True, text=True,
                           errors="replace", timeout=240)
        src = r.stdout or ""
    except Exception:
        src = ""
    _DECOMPILE_CACHE[key] = src
    if cf is not None and src:
        try:
            cf.write_text(src, errors="replace")
        except Exception:
            pass
    return src


def calls_member(m: dict, type_short: str, method: str) -> bool:
    """True if a loaded DLL of this mod decompiles to a '<type_short>.<method>' call
    (e.g. 'UnitFormatter.FormatHours'). Substring matches short or fully-qualified
    rendering (it's a suffix), and won't cross-match a different type like 'UnitFormats'."""
    needle = f"{type_short}.{method}"
    return any(needle in decompile(d) for d in m["loaded_dlls"])


def _method_parts(sig: str):
    """From "RetType NS.Type.Method<!0>(args)" -> (declaring_type_short, method)."""
    full = sig.split("(")[0].split()[-1].split("<")[0]   # NS.Type.Method
    parts = full.split(".")
    return (parts[-2] if len(parts) >= 2 else None), parts[-1]


def resolve_culprit(diag: dict, mods, known: set[str]) -> dict:
    """Attach 'culprits' (active LOADED mods), 'notes', and 'token' (primary
    search string, reused for the already-removed check)."""
    diag["culprits"] = []
    diag["notes"] = []
    diag["token"] = None
    diag["token_json"] = False
    cls = diag["cls"]

    if cls == "typeload":
        diag["token"] = diag["type"].split(".")[0]
        diag["culprits"] = owners_of_dotted(diag["type"], mods)

    elif cls == "typeref_missing":
        # A loaded mod references <type>, but the game assembly that should define
        # it no longer does (removed/renamed across a game update). Match the type's
        # LEAF as a DISCRETE #Strings entry ("\x00leaf\x00") -> the referencing
        # mod(s). Discrete matching avoids a getter-substring false positive (e.g.
        # 'get_ModVersion') fingering an unrelated, possibly foundational mod, which
        # a loose substring search would -- risking a cascade auto-disable.
        leaf = diag["type"].split(".")[-1]
        diag["culprits"] = owners_of_token(leaf, mods, discrete=True)
        if diag["culprits"]:
            diag["token"] = leaf
        else:
            diag["conf"] = "n/a"
            diag["notes"].append(
                f"no currently-loaded mod carries a discrete '{leaf}' type "
                "reference -> the mod that did was removed/updated/disabled since "
                "the crash; relaunch to confirm it's cleared")

    elif cls == "harmony" and diag.get("target"):
        diag["token"] = diag["target"].split(".")[0]
        diag["culprits"] = owners_of_dotted(diag["target"], mods)

    elif cls == "missing_asm":
        asm = diag["asm"].split(",")[0].strip() if diag.get("asm") else None
        tref = diag.get("typeref")
        leaf = tref.split(".")[-1] if tref else None
        # 1) Precise: a distinctive unresolved TYPE name in a loaded DLL (e.g. the
        #    removed generic 'IObjectSerializer`1'). Short/common names like 'Tool'
        #    are useless as substrings -> skip and use the assembly instead.
        if leaf and len(leaf) > 6:
            # Distinctive type named -> resolve by it. A loaded hit is the culprit; NO
            # loaded hit leaves culprits empty (token=leaf) so build_plan's disposed
            # check marks the report STALE -- rather than the broad assembly-consumer
            # net (branch 2) fingering innocent mods that merely share the removed game
            # assembly after the real culprit is already in _BUG. The culprit MUST
            # reference this failing type, so the leaf owner is authoritative.
            diag["token"] = leaf
            hits = owners_of_token(leaf, mods)
            if hits:
                diag["culprits"] = hits
                diag["conf"] = "high"
        # 2) No distinctive type -> the loaded CONSUMER of the missing/version-mismatched
        #    assembly, minus its provider. A 'present' assembly is NOT safe: a mod built
        #    against a different version still fails to bind, so don't suppress on presence.
        elif asm:
            diag["token"] = asm
            consumers = consumers_of_assembly(asm, mods)
            game_dll = GAME / (asm + ".dll")
            if (asm.startswith("Timberborn.") and GAME.exists() and not game_dll.exists()
                    and len(consumers) >= 4):
                # A GAME assembly this build lacks, needed by MANY mods = a game/mod VERSION
                # mismatch, not per-mod culprits. Disabling would gut the modpack (often
                # foundational mods). Report the real cause; propose no disable (conf n/a).
                diag["culprits"] = consumers
                diag["conf"] = "n/a"
                diag["notes"].insert(0,
                    f"VERSION MISMATCH: game assembly '{asm}' is absent from your install yet "
                    f"required by {len(consumers)} loaded mods -> your Timberborn build is out "
                    "of step with these mods. Fix by aligning the GAME (Steam > Timberborn > "
                    "Properties > Betas > the branch/version your mods target), NOT by disabling "
                    "them. Whack-a-mole disabling will strip foundational mods and their trees.")
            elif consumers:
                diag["culprits"] = consumers
                diag["conf"] = "high" if ("." in asm and len(consumers) <= 3) else "med"
                if asm in known:
                    diag["notes"].append(f"'{asm}' is installed but a loaded mod needs a "
                                         "different version -> that consumer is the culprit")

    elif cls == "spec_key":
        diag["token"] = f'"{diag["key"]}"'
        diag["token_json"] = True
        diag["culprits"] = owners_of_token(diag["token"], mods, in_dll=False, in_json=True)

    elif cls == "missing_method":
        # sig e.g. "RetType NS.Type.Method<!0>(args)". Method name drives a first-pass
        # name match (strip the generic-arg suffix the runtime prints -- .NET metadata
        # stores only the bare name, so 'FormatHours<!0>' never matches as-is).
        decl, method = _method_parts(diag["sig"])
        diag["token"] = method
        cands = owners_of_token(method, mods)
        # PRECISION: a method name alone is ambiguous (many mods call FormatHours). With
        # ILSpy + a declaring type, decompile the candidate(s) and keep only those that
        # actually call <DeclaringType>.<method> -- the game 'UnitFormatter.FormatHours'
        # caller, not mods calling their own 'UnitFormats'. Confirms a lone candidate,
        # disambiguates several, and empties an all-innocent set (-> disposed/stale check).
        if cands and decl and ILSPYCMD:
            precise = [m for m in cands if calls_member(m, decl, method)]
            diag["notes"].append(f"ILSpy: {len(precise)} of {len(cands)} name-match "
                                 f"candidate(s) actually call {decl}.{method}")
            cands = precise
            if precise:
                diag["conf"] = "high" if len(precise) == 1 else "med"
        diag["culprits"] = cands
        if len(diag["culprits"]) > 1 and diag["conf"] == "low":
            diag["notes"].append("multiple loaded DLLs reference this method name -> "
                                 "ambiguous; needs manual CLI-metadata check (README class C)")
    return diag


def disposed_roots() -> list:
    """Top-level mod folders already disposed to _BUG/ or __archives/YYYYMMDD-N/."""
    out = []
    bug = MODS / "_BUG"
    if bug.exists():
        out += [p for p in bug.iterdir() if p.is_dir() and p.name != "_edits-backup"]
    arch = MODS / "__archives"
    if arch.exists():
        for batch in arch.iterdir():
            if batch.is_dir():
                out += [p for p in batch.iterdir() if p.is_dir()]
    return out


def find_disposed(token: str, in_json=False) -> list:
    """Disposed folders whose DLL/JSON references token -> 'already removed' proof.
    Skips folders that merely PROVIDE the token as an assembly (<token>.dll) so an
    archived provider (e.g. an old TimberApi holding TimberApi.Tools.dll) isn't
    mistaken for the removed consumer."""
    if not token:
        return []
    tb = token.encode("utf-8", "replace")
    provider_dll = (token + ".dll").lower()
    out = []
    for r in disposed_roots():
        files = [Path(dp) / f for dp, _dn, fs in os.walk(r) for f in fs]
        if any(f.name.lower() == provider_dll for f in files):
            continue  # provider, not the removed consumer
        found = False
        for f in files:
            fl = f.name.lower()
            if fl.endswith(".dll") or (in_json and fl.endswith(".json")):
                try:
                    if tb in f.read_bytes():
                        found = True
                        break
                except Exception:
                    pass
        if found:
            out.append(r.relative_to(MODS))
    return out


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #
def find_duplicate_groups(mods):
    by_id = defaultdict(dict)      # id -> {folder: mod}
    for m in mods:
        if not m["is_loaded"]:
            continue          # a mod the game can't load can't collide in ToDictionary
        for i in m["ids"]:
            by_id[i][m["folder"]] = m
    groups = []
    for i, fmap in by_id.items():
        if len(fmap) > 1:
            groups.append((i, list(fmap.values())))
    return groups


def pick_keeper(group):
    # Prefer copies loadable on THIS game version; among those the highest
    # manifest Version wins (no silent downgrade), newest mtime breaks ties.
    pool = [m for m in group if loadable(m)] or group
    return max(pool, key=lambda m: (m["best_version"], m["mtime"]))


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def dated_archive_dir():
    today = time.strftime("%Y%m%d")
    n = 1
    while (MODS / "__archives" / f"{today}-{n}").exists():
        n += 1
    return MODS / "__archives" / f"{today}-{n}"


def build_plan(mods, reports, force: bool):
    known = known_assemblies(mods)
    by_id = defaultdict(set)                       # Id -> set(active folders)
    dependents_of = defaultdict(list)             # Id -> [mod names requiring it]
    for m in mods:
        for i in m["ids"]:
            by_id[i].add(m["folder"])
        for r in m["required"]:
            dependents_of[r].append(m["name"])

    actions = []          # {kind, folder(mod), dest_parent, reason, source, warn}
    planned_folders = set()
    diagnoses = []

    # ---- crash triage ----
    for name, exc, log in reports:
        diag = resolve_culprit(classify(exc, log), mods, known)
        diag["report"] = name
        diagnoses.append(diag)
        cls = diag["cls"]
        if cls in ("transient", "unknown", "dup_id"):
            continue  # dup_id handled by dedup below; transient/unknown -> no action
        if not diag["culprits"]:
            disp = find_disposed(diag.get("token"), in_json=diag.get("token_json", False))
            if disp:
                diag["stale"] = True
                diag["notes"].append(f"ALREADY REMOVED -> {disp[0]} "
                                     "(stale report from before removal; relaunch to clear it)")
            else:
                diag["notes"].append("no loaded mod matches, none in _BUG/__archives "
                                     "-> not installed or already updated")
            continue
        # Confidence gate (GUARD 2): fragile classes are report-only unless --force
        auto_ok = diag["conf"] == "high" or (diag["conf"] == "med" and len(diag["culprits"]) == 1)
        for c in diag["culprits"]:
            # Cascade guard (GUARD 1): never auto-disable a depended-upon mod
            deps = [d for i in c["ids"] for d in dependents_of.get(i, []) if d != c["name"]]
            if deps:
                diag["notes"].append(
                    f"CASCADE RISK: {c['name']} is required by {len(deps)} active mod(s) "
                    f"({', '.join(sorted(set(deps))[:4])}...) -> NOT auto-disabling; needs an update")
                continue
            if c["folder"] in planned_folders:
                continue
            if not (auto_ok or force):
                diag["notes"].append(f"low-confidence -> report-only (use --force): {c['name']}")
                continue
            planned_folders.add(c["folder"])
            actions.append({
                "kind": "disable", "mod": c, "dest_parent": MODS / "_BUG",
                "reason": f"{diag['summary']} [{_report_stamp(name)}]",
                "warn": None if not deps else "depended-upon",
            })

    # ---- duplicate cleanup ----
    archive_parent = None
    for mod_id, group in sorted(find_duplicate_groups(mods)):
        keeper = pick_keeper(group)
        for m in group:
            if m["folder"] == keeper["folder"] or m["folder"] in planned_folders:
                continue
            planned_folders.add(m["folder"])
            if archive_parent is None:
                archive_parent = dated_archive_dir()
            actions.append({
                "kind": "dedup", "mod": m, "dest_parent": archive_parent,
                "reason": (f"duplicate Id '{mod_id}': keep {keeper['name']} "
                           f"(v{fmt_ver(keeper)}, {fmt_time(keeper['mtime'])}); "
                           f"remove older v{fmt_ver(m)}, {fmt_time(m['mtime'])}"),
                "warn": None,
            })
    return diagnoses, actions


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def unique_dest(parent: Path, name: str) -> Path:
    dest = parent / name
    i = 2
    while dest.exists():
        dest = parent / f"{name}__{i}"
        i += 1
    return dest


def apply_actions(actions):
    done = []
    for action in actions:
        if action["kind"] == "repair_legacy":
            destination = _apply_legacy_repair(action)
        else:
            source = action["mod"]["folder"]
            action["dest_parent"].mkdir(parents=True, exist_ok=True)
            destination = unique_dest(action["dest_parent"], source.name)
            shutil.move(str(source), str(destination))
        done.append((action, destination))
    return done


# --------------------------------------------------------------------------- #
# Findings -> items  (label = title shown by default; detail = hidden 'debug')
# --------------------------------------------------------------------------- #
def diag_items(diagnoses, player_log_note=False):
    items = []
    if player_log_note:
        items.append({"sev": "info",
                      "label": "no error-report zips -> triaged the live Player.log",
                      "detail": str(PLAYER_LOG)})
    for d in diagnoses:
        sev = "info" if d["cls"] in ("transient", "unknown") else "crash"
        if d.get("stale"):
            sev = "skip"
        det = []
        if d.get("culprits"):
            det.append("culprit(s): " + ", ".join(c["name"] for c in d["culprits"]))
        det += [f"note: {n}" for n in d.get("notes", [])]
        det.append(f"source: {_report_stamp(d['report'])}")
        items.append({"sev": sev,
                      "label": f"{d['cls']}/{d['conf']} - {d['summary']}",
                      "detail": "\n".join(det)})
    if not diagnoses:
        items.append({"sev": "ok", "label": "no crash reports to triage", "detail": ""})
    return items


def action_items(actions, applied):
    items = []
    for action in actions:
        mod = action["mod"]
        if action["kind"] == "repair_legacy":
            verb = "migrated" if applied else "will migrate"
            target = action.get("repair_path", mod["folder"].with_name(
                mod["folder"].name + "__mod_doctor_1.0"))
            label = f"{verb} {mod['name']} -> native {target.name}"
            detail = (f"why: {action['reason']}\noriginal -> __archives\n"
                      f"generated target: {target}\n{source_note(mod)}")
        else:
            tag = "-> _BUG" if action["kind"] == "disable" else "-> __archives"
            label = f"{'moved' if applied else 'will move'} {mod['name']} {tag}"
            detail = f"why: {action['reason']}\n{source_note(mod)}"
        items.append({"sev": "action", "label": label, "detail": detail})
    if not actions:
        items.append({"sev": "ok", "label": "nothing to do", "detail": ""})
    return items


def summary_items(diagnoses, actions, applied):
    disable = [a for a in actions if a["kind"] == "disable"]
    dedup = [a for a in actions if a["kind"] == "dedup"]
    repaired = [a for a in actions if a["kind"] == "repair_legacy"]
    stale = [d for d in diagnoses if d.get("stale")]
    transient = [d for d in diagnoses if d["cls"] == "transient"]
    acted = {a["mod"]["folder"] for a in actions}
    manual = [d for d in diagnoses if d.get("culprits")
              and not any(c["folder"] in acted for c in d["culprits"])]
    items = [{"sev": "info",
              "label": f"disable {len(disable)} | dedup {len(dedup)} | "
                       f"migrate {len(repaired)} | stale {len(stale)} | "
                       f"manual {len(manual)} | transient {len(transient)}",
              "detail": ""}]
    synced = [a for a in actions if a["mod"]["steam"] or a["mod"]["modio"]]
    if synced:
        items.append({"sev": "warn",
                      "label": f"{len(synced)} synced mod(s) -> re-download unless unsubscribed",
                      "detail": "\n".join(f"{a['mod']['name']}: {source_note(a['mod'])}"
                                          for a in synced)})
    if actions and applied:
        v, s = "Applied. Relaunch the game to verify.", "ok"
    elif actions:
        v, s = "Re-run with --apply to perform these.", "warn"
    elif stale and not manual:
        v, s = "Nothing to do: crashes are from already-removed mods.", "ok"
    elif manual:
        v, s = "No safe auto-fix; see the manual-review items.", "warn"
    else:
        v, s = "No known crasher. Relaunch to verify.", "ok"
    items.append({"sev": s, "label": v, "detail": ""})
    return items


def verify_items():
    dups = find_duplicate_groups(active_mods())
    if dups:
        return [{"sev": "crash", "label": f"DUPLICATE Id '{i}'",
                 "detail": ", ".join(m["name"] for m in g)} for i, g in dups]
    return [{"sev": "ok", "label": "every active mod Id is unique (no ToDictionary collision)",
             "detail": ""}]


# --------------------------------------------------------------------------- #
# Rendering: colored plain (titles only) or clickable Textual TUI
# --------------------------------------------------------------------------- #
_SEV_STYLE = {"ok": "green", "info": "cyan", "skip": "grey58",
              "warn": "yellow", "crash": "bold red", "action": "magenta"}


def _use_tui(args) -> bool:
    if args.plain:
        return False
    if args.tui:
        return True
    if not sys.stdout.isatty():
        return False
    try:
        import textual  # noqa: F401
        return True
    except Exception:
        return False


def render_plain(meta, sections, details):
    try:
        from rich.console import Console
        from rich.markup import escape
        pr = Console().print
    except Exception:
        def escape(s):
            return str(s)

        def pr(s):
            print(re.sub(r"\[/?[^\]]*\]", "", s))
    for line in meta:
        pr(f"[dim]{escape(line)}[/dim]")
    n = 0
    for title, items in sections:
        pr(f"\n[bold underline]{escape(title)}[/]")
        if not items:
            pr("   [grey58](none)[/]")
            continue
        for it in items:
            n += 1
            st = _SEV_STYLE.get(it["sev"], "white")
            pr(f"  [{st}]{n:>2}. {escape(it['label'])}[/]")
            if details and it.get("detail"):
                for dl in it["detail"].splitlines():
                    pr(f"       [grey58]{escape(dl)}[/]")
    if not details:
        pr("\n[dim]details hidden -- rerun with --details, or --tui for a clickable view[/dim]")


_TUI_CSS = """
.meta { color: #7f848e; }
.sechead { text-style: bold; margin: 1 0 0 0; }
.detail { color: #9aa0a6; padding: 0 0 1 3; }
Collapsible { border: none; padding: 0 0 0 1; }
.ok { color: #98c379; }
.info { color: #56b6c2; }
.skip { color: #7f848e; }
.warn { color: #e5c07b; }
.crash { color: #e06c75; text-style: bold; }
.action { color: #c678dd; }
"""


def _build_tui_app(meta, sections):
    """Construct (but don't run) the Textual review app -- factory-style so it can be
    exercised headlessly via App.run_test()."""
    from textual.app import App
    from textual.containers import VerticalScroll
    from textual.widgets import Header, Footer, Static, Collapsible

    class DoctorTUI(App):
        TITLE = "mod_doctor"
        SUB_TITLE = "click a row to expand -- e/c expand/collapse all, q quit"
        CSS = _TUI_CSS
        BINDINGS = [("q", "quit", "Quit"), ("e", "expand_all", "Expand all"),
                    ("c", "collapse_all", "Collapse all")]

        def compose(self):
            yield Header()
            with VerticalScroll():
                for line in meta:
                    yield Static(line, markup=False, classes="meta")
                n = 0
                for title, items in sections:
                    yield Static(title, markup=False, classes="sechead")
                    if not items:
                        yield Static("  (none)", markup=False, classes="skip")
                        continue
                    for it in items:
                        n += 1
                        yield Collapsible(
                            Static(it["detail"] or "(no further detail)",
                                   markup=False, classes="detail"),
                            title=f"{n:>2}. {it['label']}",
                            collapsed=True, classes=it["sev"])
            yield Footer()

        def action_expand_all(self):
            for cw in self.query(Collapsible):
                cw.collapsed = False

        def action_collapse_all(self):
            for cw in self.query(Collapsible):
                cw.collapsed = True

    return DoctorTUI()


def render_tui(meta, sections):
    _build_tui_app(meta, sections).run()


# --------------------------------------------------------------------------- #
# Main
def legacy_mods(mods):
    """TimberAPI mod.json folders skipped by Timberborn's native loader."""
    out = []
    for m in mods:
        folder = m["folder"]
        if m["is_loaded"] or _looks_external(folder):
            continue
        if (folder / "mod.json").exists() and not (folder / "manifest.json").exists():
            out.append(m)
    return out


def _legacy_specs_dir(folder: Path) -> Path | None:
    try:
        return next((p for p in folder.iterdir()
                     if p.is_dir() and p.name.lower() == "specifications"), None)
    except OSError:
        return None


def _legacy_profile(mod):
    """Classify a TimberAPI package by what can be migrated without inventing code.

    Data-only Specification packages map deterministically to Timberborn 1.x
    Blueprints. Compiled TimberAPI DLLs and bundles containing serialized GameObjects
    need source-level rebuilds; renaming their manifest would only expose incompatible
    binaries to the current loader.
    """
    folder = mod["folder"]
    metadata = read_json(folder / "mod.json")
    if not isinstance(metadata, dict):
        return {"mod": mod, "repairable": False, "reason": "unreadable mod.json"}
    entry_dll = metadata.get("EntryDll")
    if entry_dll:
        dll = folder / str(entry_dll)
        refs = sorted(_assembly_refs(dll)) if dll.exists() else []
        obsolete = [ref for ref in refs if ref == "TimberApi" or
                    (ref.startswith("Timberborn.") and not (GAME / f"{ref}.dll").exists())]
        evidence = ", ".join(obsolete[:8]) or str(entry_dll)
        return {
            "mod": mod, "metadata": metadata, "repairable": False,
            "reason": f"compiled EntryDll requires a source rebuild; obsolete references: {evidence}",
        }
    specs_dir = _legacy_specs_dir(folder)
    if not specs_dir:
        return {
            "mod": mod, "metadata": metadata, "repairable": False,
            "reason": "no Specifications directory to translate",
        }
    supported = ("NeedSpecification.", "GoodSpecification.", "RecipeSpecification.",
                 "FactionSpecification.", "BuildingSpecification.")
    specs = sorted(p for p in specs_dir.glob("*.json") if p.is_file())
    unsupported = [p.name for p in specs if not p.name.startswith(supported)]
    if not specs or unsupported:
        detail = "no supported specification files" if not specs else (
            "unsupported specification types: " + ", ".join(unsupported))
        return {"mod": mod, "metadata": metadata, "repairable": False, "reason": detail}
    assets = next((p for p in folder.iterdir()
                   if p.is_dir() and p.name.lower() == "assets"), None)
    if assets:
        for manifest in assets.glob("*.manifest"):
            text = manifest.read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?m)^- Class: (?:1|114)$", text):
                return {
                    "mod": mod, "metadata": metadata, "repairable": False,
                    "reason": "asset bundle contains serialized GameObjects/scripts and needs "
                              "a current Unity/source rebuild",
                }
    obsolete_targets = []
    for spec in specs:
        if spec.name.startswith("BuildingSpecification."):
            tail = spec.name[len("BuildingSpecification."):].removesuffix(".json")
            filename = f"{tail}.blueprint.json"
            if not _built_in_blueprint_path(filename):
                obsolete_targets.append(filename)
    reason = "data-only TimberAPI Specifications can be translated to native Blueprints"
    if obsolete_targets:
        reason += "; obsolete building targets omitted: " + ", ".join(obsolete_targets)
    return {
        "mod": mod, "metadata": metadata, "specs_dir": specs_dir, "specs": specs,
        "assets_dir": assets, "repairable": True, "obsolete_targets": obsolete_targets,
        "reason": reason,
    }


def _coordinate_legacy_profiles(profiles):
    """Assign duplicate legacy definitions to one package and deduplicate appends."""
    definitions = defaultdict(list)
    for profile in profiles:
        profile["omit_specs"] = set()
        profile["omit_needs"] = set()
        profile["omit_goods"] = set()
        profile["omit_recipes"] = set()
        if not profile["repairable"]:
            continue
        for source in profile["specs"]:
            kind = next((prefix for prefix in ("Need", "Good", "Recipe")
                         if source.name.startswith(prefix + "Specification.")), None)
            if not kind:
                continue
            spec = read_json(source)
            identity = spec.get("Id") if isinstance(spec, dict) else None
            if identity:
                definitions[(kind, str(identity))].append((profile, source))
    for (kind, identity), owners in definitions.items():
        if len(owners) < 2:
            continue
        def ownership(entry):
            metadata = entry[0]["metadata"]
            name = f"{metadata.get('Name', '')} {metadata.get('UniqueId', '')}".lower()
            return (identity.lower() in name, -len(name))
        winner, _ = max(owners, key=ownership)
        for profile, source in owners:
            if profile is winner:
                continue
            profile["omit_specs"].add(source.name)
            profile[f"omit_{kind.lower()}s"].add(identity)
            profile["reason"] += f"; duplicate {kind} {identity} supplied by " \
                                 f"{winner['mod']['name']}"
    return profiles


def _hex_color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6,8}", value):
        return value
    raw = value[1:]
    channels = [int(raw[i:i + 2], 16) / 255 for i in range(0, len(raw), 2)]
    if len(channels) == 3:
        channels.append(1.0)
    return dict(zip(("r", "g", "b", "a"), channels))


def _native_asset_path(value, metadata):
    if not isinstance(value, str):
        return value
    for asset in metadata.get("Assets", []) or []:
        prefix = asset.get("Prefix") if isinstance(asset, dict) else None
        if prefix and value.startswith(prefix + "/"):
            return value[len(prefix) + 1:]
    return value


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _convert_need(spec):
    value = dict(spec)
    for key in ("CriticalNeedType", "CriticalSpriteName", "CriticalDescriptionLocKey",
                "DeathOnMinValue", "DeathMessageLocKey", "RequiredFeatureToggle"):
        value.pop(key, None)
    value.setdefault("HoursWarningThreshold", 0.0)
    value.setdefault("UnfavorableWellbeing", 0)
    return {"NeedSpec": value}


def _convert_good(spec, metadata):
    value = dict(spec)
    visible = value.get("VisibleContainer")
    if isinstance(visible, dict):
        value["VisibleContainer"] = visible.get("Value", "")
    value["ContainerColor"] = _hex_color(value.get("ContainerColor"))
    value["Icon"] = _native_asset_path(value.get("Icon", ""), metadata)
    value.pop("RequiredFeatureToggle", None)
    value.setdefault("ContainerMaterial", "")
    value.setdefault("ForceImport", True)
    return {"GoodSpec": value}


def _convert_recipe(spec, metadata):
    value = dict(spec)
    value.setdefault("BackwardCompatibleIds", [])
    for key in ("Ingredients", "Products"):
        converted = []
        for item in value.get(key, []) or []:
            item = dict(item)
            good = item.pop("Good", None)
            if isinstance(good, dict):
                item["Id"] = good.get("Id")
            converted.append(item)
        value[key] = converted
    fuel = value.get("Fuel")
    if isinstance(fuel, dict):
        value["Fuel"] = fuel.get("Id", "")
    value["Icon"] = _native_asset_path(value.get("Icon", ""), metadata)
    return {"RecipeSpec": value}


def _built_in_blueprint_path(filename: str) -> Path | None:
    archive = GAME.parent / "StreamingAssets" / "Modding" / "Blueprints.zip"
    if not archive.exists():
        return None
    with zipfile.ZipFile(archive) as blueprints:
        matches = [Path(name) for name in blueprints.namelist()
                   if Path(name).name == filename]
    return matches[0] if len(matches) == 1 else None


def _convert_legacy_package(profile, destination: Path):
    """Write a native, current-build package from one validated data-only profile."""
    metadata = profile["metadata"]
    current = ".".join(map(str, GAMEV))
    version_root = destination / f"version-{current}"
    version_root.mkdir(parents=True)
    manifest = {
        "Name": metadata.get("Name") or profile["mod"]["name"],
        "Version": metadata.get("Version") or "1.0.0",
        "Id": metadata.get("UniqueId") or metadata.get("Id") or profile["mod"]["name"],
        "MinimumGameVersion": current,
        "Description": "Locally migrated from a data-only TimberAPI package by mod_doctor.",
    }
    _write_json(version_root / "manifest.json", manifest)
    for source in profile["specs"]:
        if source.name in profile.get("omit_specs", set()):
            continue
        spec = read_json(source)
        if not isinstance(spec, dict):
            raise ValueError(f"unreadable specification: {source.name}")
        name = source.name
        if name.startswith("NeedSpecification."):
            tail = name[len("NeedSpecification."):]
            tail = tail.removesuffix(".original.json").removesuffix(".json")
            _write_json(version_root / "Needs" / f"Need.{tail}.blueprint.json",
                        _convert_need(spec))
        elif name.startswith("GoodSpecification."):
            tail = name[len("GoodSpecification."):]
            tail = tail.removesuffix(".original.json").removesuffix(".json")
            _write_json(version_root / "Goods" / f"Good.{tail}.blueprint.json",
                        _convert_good(spec, metadata))
        elif name.startswith("RecipeSpecification."):
            tail = name[len("RecipeSpecification."):]
            tail = tail.removesuffix(".original.json").removesuffix(".json")
            _write_json(version_root / "Recipes" / f"Recipe.{tail}.blueprint.json",
                        _convert_recipe(spec, metadata))
        elif name.startswith("FactionSpecification."):
            faction = name[len("FactionSpecification."):].removesuffix(".json")
            needs = [item for item in spec.get("Needs", [])
                     if item not in profile.get("omit_needs", set())]
            if needs:
                _write_json(
                    version_root / "NeedCollection" /
                    f"NeedCollection.{faction}.blueprint.json",
                    {"NeedCollectionSpec": {"CollectionId": faction,
                                            "Needs#append": needs}})
            goods = [item for item in spec.get("Goods", [])
                     if item not in profile.get("omit_goods", set())]
            if goods:
                _write_json(
                    version_root / "GoodCollections" /
                    f"GoodCollection.{faction}.blueprint.json",
                    {"GoodCollectionSpec": {"CollectionId": faction,
                                            "Goods#append": goods}})
        elif name.startswith("BuildingSpecification."):
            tail = name[len("BuildingSpecification."):].removesuffix(".json")
            target_name = f"{tail}.blueprint.json"
            target = _built_in_blueprint_path(target_name)
            if not target:
                continue  # Building/faction no longer exists in the installed game.
            recipes = [item for item in
                       (spec.get("RecipeIds") or spec.get("ProductionRecipeIds") or [])
                       if item not in profile.get("omit_recipes", set())]
            if recipes:
                _write_json(
                    version_root / target,
                    {"ManufactorySpec": {"ProductionRecipeIds#append": recipes}})
    lang_dir = next((p for p in profile["mod"]["folder"].iterdir()
                     if p.is_dir() and p.name.lower() == "lang"), None)
    if lang_dir:
        for source in lang_dir.glob("*.txt"):
            target = version_root / "Localizations" / f"{source.stem}.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    assets = profile.get("assets_dir")
    if assets:
        target = version_root / "AssetBundles"
        target.mkdir(parents=True, exist_ok=True)
        for source in assets.iterdir():
            if source.is_file():
                shutil.copy2(source, target / source.name)
    for pattern in ("thumbnail.*", "icon.*"):
        for source in profile["mod"]["folder"].glob(pattern):
            if source.is_file():
                shutil.copy2(source, version_root / source.name)
    return version_root


def _apply_legacy_repair(action):
    source = action["mod"]["folder"]
    action["dest_parent"].mkdir(parents=True, exist_ok=True)
    archived = unique_dest(action["dest_parent"], source.name)
    repaired = unique_dest(source.parent, source.name + "__mod_doctor_1.0")
    staging = unique_dest(source.parent, "." + source.name + ".mod-doctor-staging")
    profile = action.get("profile") or _legacy_profile(action["mod"])
    try:
        if not profile["repairable"]:
            raise ValueError(profile["reason"])
        _convert_legacy_package(profile, staging)
        shutil.move(str(source), str(archived))
        shutil.move(str(staging), str(repaired))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if archived.exists() and not source.exists():
            shutil.move(str(archived), str(source))
        raise
    action["repair_path"] = repaired
    return repaired


def legacy_compat_items(profiles):
    items = []
    for profile in profiles:
        mod = profile["mod"]
        if profile["repairable"]:
            items.append({
                "sev": "action",
                "label": f"native migration available for {mod['name']}",
                "detail": profile["reason"] + "\n--repair-legacy translates Specifications "
                          "to 1.x Blueprints and preserves the original in __archives.",
            })
        else:
            items.append({
                "sev": "warn",
                "label": f"source rebuild required for {mod['name']}",
                "detail": profile["reason"] + "\nA synthetic manifest is rejected because "
                          "it would load incompatible code/assets rather than port them.",
            })
    if not items:
        items.append({"sev": "ok", "label": "no legacy TimberAPI packages", "detail": ""})
    return items


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Timberborn mod crash triage + compatibility repair")
    ap.add_argument("--apply", action="store_true",
                    help="perform planned moves/migrations (default: dry run)")
    ap.add_argument("--reports", type=int, default=0,
                    help="only the N most recent error reports (0 = all)")
    ap.add_argument("--no-dedup", action="store_true", help="skip duplicate cleanup")
    ap.add_argument("--no-crash", action="store_true", help="skip crash triage")
    ap.add_argument("--force", action="store_true",
                    help="also auto-apply low-confidence classes (spec-key, missing-method)")
    ap.add_argument("--details", action="store_true",
                    help="plain output: show every finding's detail (default: titles only)")
    ap.add_argument("--tui", action="store_true", help="force the interactive collapsible TUI")
    ap.add_argument("--plain", action="store_true", help="force plain output (no TUI)")
    ap.add_argument("--mods", help="Timberborn Mods dir (default: auto-detect / $TIMBERBORN_MODS)")
    ap.add_argument("--game",
                    help="Timberborn install or its Managed dir (default: auto-detect / $TIMBERBORN_GAME)")
    ap.add_argument("--repair-legacy", action="store_true",
                    help="translate supported data-only TimberAPI Specifications into native "
                         "Blueprints for the installed game; compiled mods remain report-only")
    args = ap.parse_args(argv)
    if args.mods or args.game:
        global MODS, ER, GAME, GAMEV
        MODS, ER, GAME = resolve_paths(args.mods, args.game)
        GAMEV = _detect_game_version()
    if not MODS.exists():
        print(f"Mods dir not found: {MODS}\n"
              "Run from your Timberborn/Mods folder, pass --mods <path>, "
              "or set the TIMBERBORN_MODS environment variable.", file=sys.stderr)
        return 2

    meta = [f"Mods dir     : {MODS}",
            f"Error reports: {ER}",
            f"Game managed : {GAME}  ({'found' if GAME.exists() else 'NOT FOUND'})",
            f"Game version : {'.'.join(map(str, GAMEV))}"]
    sections = []

    sections.append(("VERSION DIRECTORIES", normalize_version_dirs(apply=args.apply)))

    mods = active_mods()
    loaded = sum(1 for m in mods if m["is_loaded"])
    meta.append(f"Mod folders  : {len(mods)} present, {loaded} loaded by the game")

    sections.append(("GAME COMPATIBILITY (AssemblyRef scan)", compat_report(mods)))
    legacy_profiles = _coordinate_legacy_profiles(
        [_legacy_profile(mod) for mod in legacy_mods(mods)])
    sections.append(("LEGACY COMPATIBILITY", legacy_compat_items(legacy_profiles)))

    reports, player_note = [], False
    if not args.no_crash:
        zips = sorted(ER.glob("error-report-*.zip"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if args.reports > 0:
            zips = zips[:args.reports]
        reports = [(zp.name, *load_report(zp)[1:]) for zp in zips]
        if not reports:
            pl = scan_player_log()
            if pl:
                reports, player_note = [pl], True

    diagnoses, actions = build_plan(mods, reports, args.force)
    if args.no_dedup:
        actions = [a for a in actions if a["kind"] != "dedup"]
    if args.repair_legacy:
        planned = {a["mod"]["folder"] for a in actions}
        archive = dated_archive_dir()
        for profile in legacy_profiles:
            mod = profile["mod"]
            if not profile["repairable"] or mod["folder"] in planned:
                continue
            actions.append({
                "kind": "repair_legacy",
                "mod": mod,
                "dest_parent": archive,
                "reason": profile["reason"],
                "warn": None,
                "profile": profile,
            })

    if not args.no_crash:
        sections.append(("CRASH TRIAGE", diag_items(diagnoses, player_note)))

    if args.apply:
        apply_actions(actions)
    verb = "APPLIED" if args.apply else "PLANNED (dry-run; --apply to perform)"
    sections.append((f"ACTIONS {verb}", action_items(actions, args.apply)))
    sections.append(("SUMMARY", summary_items(diagnoses, actions, args.apply)))
    if args.apply:
        sections.append(("VERIFY", verify_items()))

    if _use_tui(args):
        render_tui(meta, sections)
    else:
        render_plain(meta, sections, args.details)
    return 0


if __name__ == "__main__":
    sys.exit(main())
