"""Supplement a USB import with favorites and album classifications.

Run after importing the phone's photos into Photos.app over USB:

  photo-sync-helper.py
      Copy the phone's Photos database over USB (read-only), match every
      camera-roll item on the phone to a photo in the Mac library, and
      write sync_plan.csv. Reports phone items that aren't in the Mac
      library, and the favorites and album memberships to carry over.
      Changes nothing.

  photo-sync-helper.py --apply
      Apply sync_plan.csv through Photos.app: mark photos as favorites and
      add them to albums (creating albums as needed). Those are the only
      changes. Nothing is removed, and nothing is written to the phone.

Backup method: if reading the phone automatically over USB can't be
authorized or is unavailable, you can export favorites and albums from
the phone into folders (e.g. by AirDrop) and pass --favorites DIR /
--album NAME=DIR instead.

See README.md for the full routine.
"""
import argparse
import csv
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DEFAULT_MAC_DB = ROOT / "Photos.sqlite"
DEFAULT_PLAN = ROOT / "sync_plan.csv"
# The -shm file is left behind on purpose: SQLite rebuilds it from the WAL.
PHONE_DB_FILES = ["Photos.sqlite", "Photos.sqlite-wal"]
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
MEDIA = {".HEIC", ".HEIF", ".JPG", ".JPEG", ".PNG", ".GIF", ".TIF", ".TIFF",
         ".DNG", ".MOV", ".MP4", ".M4V"}
DATE_TOLERANCE = timedelta(seconds=5)
GROUP_TOLERANCE = timedelta(seconds=10)  # Live Photo still vs. its video
SIZE_MATCH_WINDOW = timedelta(days=1)  # a video's file date can drift by minutes
MAX_LISTED = 25
FIELDS = ["action", "status", "match", "source", "item", "capture_date", "uuid",
          "library_filename", "library_date", "added", "original_size", "notes"]


def stem_of(name):
    # "IMG_8797 2.heic" -> "IMG_8797"; "IMG_E6858.heic" -> "IMG_6858"
    stem = re.sub(r" \d+$", "", Path(name).stem).upper()
    return re.sub(r"^IMG_E(\d+)$", r"IMG_\1", stem)


def library_fields(p):
    return {
        "uuid": p.uuid,
        "library_filename": p.original_filename,
        "library_date": p.date.isoformat(timespec="seconds"),
        "added": p.date_added.date().isoformat(),
        "original_size": p.original_filesize,
    }


def age_text(path):
    age = datetime.now() - datetime.fromtimestamp(Path(path).stat().st_mtime)
    minutes = round(age.total_seconds() / 60)
    if minutes < 1:
        return "less than a minute"
    n, unit = ((minutes, "minute") if minutes < 60 else
               (round(minutes / 60), "hour") if minutes < 48 * 60 else
               (age.days, "day"))
    return f"{n} {unit}{'' if n == 1 else 's'}"


# ---------- Mac library ----------

def load_mac_library(path):
    """Return ({stem: [PhotoInfo]}, {stem: [PhotoInfo in Recently Deleted]})."""
    import osxphotos

    logging.getLogger("osxphotos").setLevel(logging.ERROR)  # "could not find search db"
    if path is None and DEFAULT_MAC_DB.exists():
        path = DEFAULT_MAC_DB
    if path:
        print(f"Mac library: copy of the database at {path} (made {age_text(path)} ago)")
        db = osxphotos.PhotosDB(dbfile=str(path))
    else:
        print("Mac library: reading the Photos library directly")
        try:
            db = osxphotos.PhotosDB()
        except OSError as e:
            sys.exit(f"couldn't read the Photos library ({e}).\nEither give this terminal "
                     "Full Disk Access, or copy Photos.sqlite here (see README).")
    by_stem, trashed = defaultdict(list), defaultdict(list)
    for p in db.photos(intrash=False):
        by_stem[Path(p.original_filename).stem.upper()].append(p)
    for p in db.photos(intrash=True):
        trashed[Path(p.original_filename).stem.upper()].append(p)
    return by_stem, trashed


# ---------- phone ----------

NO_PHONE_HELP = ("Plug the iPhone in with a USB cable, unlock it, and tap Trust if it "
                 "asks, then run again.\n(To use an already-copied phone database "
                 "instead, pass --phone-db DIR.)")


def phone_error(stderr):
    """Turn pymobiledevice3's log output into a short explanation."""
    known = [
        ("not connected", "The iPhone was disconnected."),
        ("PasswordProtected", "The iPhone is locked. Unlock it and run again."),
        ("PairingDialogResponsePending", "Tap Trust on the iPhone, then run again."),
        ("UserDeniedPairing", "The iPhone was told not to trust this Mac. Unplug it, "
                              "plug it back in, tap Trust, then run again."),
        ("NotPaired", "This Mac isn't trusted by the iPhone yet. Unlock it, tap Trust "
                      "when asked, then run again."),
    ]
    for needle, message in known:
        if needle.lower() in stderr.lower():
            return message
    lines = [l for l in stderr.splitlines() if " ERROR " in l] or stderr.splitlines() or [""]
    detail = lines[-1].split(" ERROR ", 1)[-1].strip()
    return f"Couldn't read the iPhone ({detail}).\n{NO_PHONE_HELP}"


def pull_phone_db(dest):
    """Copy the phone's Photos database into dest. Only reads from the phone."""
    cli = Path(sys.executable).with_name("pymobiledevice3")
    cli = str(cli) if cli.exists() else shutil.which("pymobiledevice3")
    if not cli:
        sys.exit("pymobiledevice3 not found; install it into the venv (see README)")
    r = subprocess.run([cli, "usbmux", "list"], capture_output=True, text=True)
    try:
        devices = [d for d in json.loads(r.stdout) if d.get("ConnectionType") == "USB"]
    except (json.JSONDecodeError, AttributeError):
        sys.exit(phone_error(r.stderr))
    if not devices:
        sys.exit(f"No iPhone found over USB.\n{NO_PHONE_HELP}")
    if len(devices) > 1:
        names = ", ".join(d.get("DeviceName", "?") for d in devices)
        sys.exit(f"More than one iPhone is connected ({names}); unplug all but one.")
    name = devices[0].get("DeviceName", "iPhone")
    print(f"phone: copying the Photos database from {name!r} over USB (read-only)...")
    for _ in range(2):
        for f in PHONE_DB_FILES:
            r = subprocess.run([cli, "afc", "pull", f"/PhotoData/{f}", str(dest / f)],
                               capture_output=True, text=True)
            if r.returncode and f == "Photos.sqlite":
                sys.exit(phone_error(r.stderr))
        c = sqlite3.connect(dest / "Photos.sqlite")
        ok = c.execute("pragma quick_check").fetchone()[0] == "ok"
        c.close()
        if ok:
            return
        # The phone wrote to its database mid-copy; copy again.
        for f in PHONE_DB_FILES:
            (dest / f).unlink(missing_ok=True)
    sys.exit("the copy of the phone's database was inconsistent twice; try again")


def read_phone(db_file):
    """Return the phone's camera-roll items: what a USB import can bring over.

    Items synced from the Mac via Finder and Shared Album items live
    outside DCIM/ and are skipped.
    """
    c = sqlite3.connect(db_file)
    ent = dict(c.execute("select Z_NAME, Z_ENT from Z_PRIMARYKEY"))
    asset_ent, album_ent = ent.get("Asset") or ent["GenericAsset"], ent["Album"]
    albums = defaultdict(list)
    for title, pk in c.execute(f"""
            select g.ZTITLE, j.Z_{asset_ent}ASSETS from ZGENERICALBUM g
            join Z_{album_ent}ASSETS j on j.Z_{album_ent}ALBUMS = g.Z_PK
            where g.ZKIND = 2 and g.ZTRASHEDSTATE = 0"""):
        albums[pk].append(title)
    items = []
    for row in c.execute("""
            select a.Z_PK, coalesce(x.ZORIGINALFILENAME, a.ZFILENAME), x.ZORIGINALFILESIZE,
                   a.ZDATECREATED, x.ZORIGINALWIDTH, x.ZORIGINALHEIGHT, a.ZFAVORITE,
                   a.ZHIDDEN, a.ZADJUSTMENTTIMESTAMP is not null, a.ZAVALANCHEUUID
            from ZASSET a left join ZADDITIONALASSETATTRIBUTES x on x.ZASSET = a.Z_PK
            where a.ZTRASHEDSTATE = 0 and a.ZDIRECTORY like 'DCIM/%'
            order by a.ZDATECREATED"""):
        pk, name, size, created, w, h, fav, hidden, edited, burst = row
        items.append({
            "filename": name,
            "stem": Path(name).stem.upper(),
            "size": size,
            "date": CORE_DATA_EPOCH + timedelta(seconds=created),
            "dims": tuple(sorted((w, h))) if w and h else None,
            "favorite": bool(fav),
            "hidden": bool(hidden),
            "edited": bool(edited),
            "burst": burst is not None,
            "albums": albums.get(pk, []),
        })
    c.close()
    return items


def match_phone(items, by_stem):
    """Pair each phone item with at most one Mac photo: {index: (photo, how)}.

    Strongest evidence first, so a photo claimed by an exact size match
    isn't taken by a weaker one. Two copies of a photo duplicated on the
    phone share name, date and size; edited is paired with edited.
    """
    def near(it, p, tol):
        return abs(p.date - it["date"]) <= tol

    passes = [
        ("size", lambda it, p: it["size"] == p.original_filesize
                               and near(it, p, SIZE_MATCH_WINDOW)),
        ("date+dimensions", lambda it, p: near(it, p, DATE_TOLERANCE) and it["dims"]
                                          == tuple(sorted((p.original_width, p.original_height)))),
        ("date only", lambda it, p: near(it, p, DATE_TOLERANCE)),
    ]
    used, found = set(), {}
    for how, test in passes:
        for i, it in enumerate(items):
            if i in found:
                continue
            cands = [p for p in by_stem.get(it["stem"], []) if p.uuid not in used and test(it, p)]
            if cands:
                cands.sort(key=lambda p: (bool(p.hasadjustments) != it["edited"],
                                          abs(p.date - it["date"])))
                found[i] = (cands[0], how)
                used.add(cands[0].uuid)
    return found


def in_mac_trash(it, trashed):
    return any(it["size"] == p.original_filesize or abs(p.date - it["date"]) <= DATE_TOLERANCE
               for p in trashed.get(it["stem"], []))


def plan_from_phone(items, found, trashed):
    rows = []
    for i, it in enumerate(items):
        notes = [n for n, on in (("favorite", it["favorite"]), ("hidden", it["hidden"]),
                                 ("edited", it["edited"]), ("burst", it["burst"])) if on]
        notes += [f"album: {a}" for a in it["albums"]]
        base = {"source": "phone", "item": it["filename"],
                "capture_date": it["date"].astimezone().isoformat(timespec="seconds")}
        if i not in found:
            if in_mac_trash(it, trashed):
                notes.insert(0, "in the Mac's Recently Deleted")
            rows.append(base | {"action": "not_on_mac", "status": "report",
                                "notes": ", ".join(notes)})
            continue
        base["notes"] = ", ".join(notes)
        p, how = found[i]
        status = "review" if how == "date only" else "ok"
        lib = library_fields(p) | {"match": how, "status": status}
        actions = (["favorite"] if it["favorite"] else []) + [f"album:{a}" for a in it["albums"]]
        if status == "review" and not actions:
            actions = ["verify"]  # probably imported, but only the capture time matches
        rows += [base | lib | {"action": a} for a in actions]
    return rows


def report_phone(items, found, rows):
    n_fav = sum(it["favorite"] for it in items)
    n_hidden = sum(it["hidden"] for it in items)
    n_album = sum(bool(it["albums"]) for it in items)
    print(f"phone: {len(items)} camera-roll items ({n_fav} favorites, "
          f"{n_album} in albums, {n_hidden} hidden)")
    hows = defaultdict(int)
    for p, how in found.values():
        hows[how] += 1
    missing = [r for r in rows if r["action"] == "not_on_mac"]
    print(f"on the Mac: {len(found) - hows['date only']} matched "
          f"({hows['size']} by size, {hows['date+dimensions']} by date+dimensions), "
          f"{hows['date only']} uncertain, {len(missing)} not in the library")
    fav = [r for r in rows if r["action"] == "favorite" and r["status"] == "ok"]
    print(f"favorites to carry over: {len(fav)} "
          f"({sum(found[i][0].favorite for i, it in enumerate(items) if it['favorite'] and i in found)}"
          " already favorites in this copy of the Mac library)")
    for album in sorted({r["action"] for r in rows if r["action"].startswith("album:")}):
        n = sum(r["action"] == album and r["status"] == "ok" for r in rows)
        print(f"album {album.removeprefix('album:')!r}: {n} photo{'' if n == 1 else 's'}")
    if missing:
        print("\non the phone but not in the Mac library "
              "(not imported, or deleted from the Mac since):")
    for r in missing[:MAX_LISTED]:
        when = datetime.fromisoformat(r["capture_date"]).strftime("%Y-%m-%d %H:%M")
        print(f"  {r['item']:16} {when}  {r['notes']}".rstrip())
    if len(missing) > MAX_LISTED:
        print(f"  ... and {len(missing) - MAX_LISTED} more (see the plan CSV)")
    for r in rows:
        if r["status"] == "review":
            print(f"  REVIEW ({r['action']}): {r['item']} ~ {r['library_filename']} "
                  f"added {r['added']} (only the capture time matches)")
    if len(missing) > len(items) / 2:
        print("MOST of the phone isn't in the Mac library: was the Mac database copied "
              "before the import? If so, copy it again and rerun.")
    elif missing and all(r["notes"].startswith("in the Mac's Recently Deleted") for r in missing):
        print("All of these were imported and then deleted on the Mac.")
    elif missing:
        print("If any of these should be on the Mac, import them (or, if the Mac "
              "database was copied before the import, copy it again).")
    if n_hidden:
        print(f"{n_hidden} hidden photos: USB import skips them; AirDrop them over (see README)")


# ---------- export folders (fallback when the phone can't be read) ----------

def file_meta(path):
    keys = ["kMDItemContentCreationDate", "kMDItemPixelWidth", "kMDItemPixelHeight"]
    args = ["mdls"] + [a for k in keys for a in ("-name", k)] + [str(path)]
    out = subprocess.run(args, capture_output=True, text=True).stdout
    meta = {}
    for line in out.splitlines():
        if " = " in line:
            k, v = (s.strip() for s in line.split(" = ", 1))
            meta[k] = None if v == "(null)" else v
    date, w, h = (meta.get(k) for k in keys)
    return {
        "path": path,
        "size": path.stat().st_size,
        "date": datetime.strptime(date, "%Y-%m-%d %H:%M:%S %z") if date else None,
        "dims": tuple(sorted((int(w), int(h)))) if w and h else None,
    }


def collect_items(source_dir):
    """Group a folder's files (recursively) into items, one per photo.

    Files sharing a filename stem and a capture time are one item: a Live
    Photo's still and video, an "IMG_E" edited render next to its
    original, or "IMG_1 2.heic" next to "IMG_1.HEIC". Same-named files
    taken at different times stay separate (numbering wraps at IMG_9999).
    """
    by_stem = defaultdict(list)
    for f in sorted(Path(source_dir).rglob("*")):
        if f.is_file() and f.suffix.upper() in MEDIA:
            by_stem[stem_of(f.name)].append(file_meta(f))
    items = []
    for stem, files in by_stem.items():
        dated = sorted((f for f in files if f["date"]), key=lambda f: f["date"])
        clusters = []
        for f in dated:
            if clusters and f["date"] - clusters[-1][-1]["date"] <= GROUP_TOLERANCE:
                clusters[-1].append(f)
            else:
                clusters.append([f])
        for f in files:
            if not f["date"]:
                if len(clusters) == 1:
                    clusters[0].append(f)
                else:
                    clusters.append([f])
        items += [{"stem": stem, "files": c} for c in clusters]
    return items


def match_item(item, by_stem):
    """Return (library photos, status, how) for one exported item."""
    files = item["files"]
    cands = by_stem.get(item["stem"], [])
    sizes = {f["size"] for f in files}
    dates = [f["date"] for f in files if f["date"]]
    dims = {f["dims"] for f in files if f["dims"]}

    def near(p, tol):
        return any(abs(p.date - d) <= tol for d in dates)

    # An exported file can't say which copy of a phone duplicate it came
    # from, so every library copy with that exact size is selected.
    by_size = [p for p in cands
               if p.original_filesize in sizes and (not dates or near(p, SIZE_MATCH_WINDOW))]
    if by_size:
        return by_size, "ok", "size"
    # Otherwise the export is probably an edited render: match on capture
    # time plus pixel dimensions (Photos reports the edited dimensions).
    by_date = [p for p in cands if near(p, DATE_TOLERANCE)]
    by_dims = [p for p in by_date if tuple(sorted((p.width, p.height))) in dims]
    if by_dims and len({p.original_filesize for p in by_dims}) == 1:
        return by_dims, "ok", "date+dimensions"
    if by_date:
        return by_date, "review", "date+dimensions, several copies" if by_dims else "date only"
    return [], "unmatched", ""


def plan_from_folders(sources, by_stem):
    rows = []
    for action, source in sources:
        for item in collect_items(source):
            photos, status, how = match_item(item, by_stem)
            first_date = min((f["date"] for f in item["files"] if f["date"]), default=None)
            base = {
                "action": action, "status": status, "match": how, "source": source,
                "item": " | ".join(str(f["path"].relative_to(source)) for f in item["files"]),
                "capture_date": first_date.isoformat() if first_date else "",
            }
            rows += [base | library_fields(p) for p in photos] or [base]
    return rows


def report_folders(rows):
    for action in dict.fromkeys(r["action"] for r in rows):
        rs = [r for r in rows if r["action"] == action]
        items = defaultdict(set)
        for r in rs:
            items[r["status"]].add((r["source"], r["item"]))
        print(f"{action}: {sum(r['status'] == 'ok' for r in rs)} library photos ok, "
              f"{len(items['review'])} items to review, {len(items['unmatched'])} unmatched")
    for r in rows:
        if r["status"] != "ok":
            print(f"  {r['status'].upper()}: {r['action']} {r['item']} {r['match']}")


# ---------- plan ----------

def cmd_plan(args):
    sources = [("favorite", d) for d in args.favorites]
    for spec in args.album:
        name, sep, d = spec.partition("=")
        if not sep:
            sys.exit(f"--album expects NAME=DIR, got {spec!r}")
        sources.append((f"album:{name}", d))
    for _, d in sources:
        if not Path(d).is_dir():
            sys.exit(f"not a folder: {d}")

    if sources:
        by_stem, trashed = load_mac_library(args.mac_db)
        rows = plan_from_folders(sources, by_stem)
        report_folders(rows)
    else:
        # Read the phone first: it's the step most likely to fail, and
        # loading the Mac library takes a while.
        with tempfile.TemporaryDirectory() as tmp:
            phone_dir = Path(args.phone_db) if args.phone_db else Path(tmp)
            if not args.phone_db:
                pull_phone_db(phone_dir)
            elif not (phone_dir / "Photos.sqlite").exists():
                sys.exit(f"no Photos.sqlite in {phone_dir}")
            items = read_phone(phone_dir / "Photos.sqlite")
        by_stem, trashed = load_mac_library(args.mac_db)
        found = match_phone(items, by_stem)
        rows = plan_from_phone(items, found, trashed)
        report_phone(items, found, rows)

    with open(args.plan, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {args.plan}")
    print("(Nothing has been changed.)")
    print(f"Review {Path(args.plan).name}, then run with --apply")
    if any(r["status"] == "review" for r in rows):
        print("review rows are skipped by --apply; change their status to 'ok' in the "
              "CSV to include them")


# ---------- apply ----------

def cmd_apply(args):
    import photoscript

    rows = list(csv.DictReader(open(args.plan)))
    todo_rows = [r for r in rows if r["status"] == "ok"
                 and (r["action"] == "favorite" or r["action"].startswith("album:"))]
    print(f"{args.plan} (written {age_text(args.plan)} ago): {len(todo_rows)} changes to "
          f"check; {len(rows) - len(todo_rows)} report/review rows skipped")

    lib = photoscript.PhotosLibrary()
    photos, problems = {}, []
    for r in todo_rows:
        if r["uuid"] in photos:
            continue
        try:
            photo = photoscript.Photo(r["uuid"])
            filename = photo.filename
        except Exception as e:
            problems.append(f"{r['library_filename']} {r['uuid']}: not found ({e})")
            continue
        if stem_of(filename) != stem_of(r["library_filename"]):
            problems.append(f"{r['library_filename']} {r['uuid']}: filename is now {filename}")
            continue
        photos[r["uuid"]] = photo

    by_action = defaultdict(dict)
    for r in todo_rows:
        if r["uuid"] in photos:
            by_action[r["action"]][r["uuid"]] = photos[r["uuid"]]

    for action, targets in by_action.items():
        if action == "favorite":
            todo = [p for p in targets.values() if not p.favorite]
            print(f"favorite: {len(targets)} planned, {len(todo)} need flagging")
            if args.apply:
                for p in todo:
                    p.favorite = True
                print(f"  flagged {len(todo)}")
            continue
        album_name = action.removeprefix("album:")
        album = lib.album(album_name)
        present = {p.uuid for p in album.photos()} if album else set()
        todo = [p for u, p in targets.items() if u not in present]
        print(f"album {album_name!r}: {len(targets)} planned, {len(todo)} to add"
              + ("" if album else " (album will be created)"))
        if args.apply and todo:
            album = album or lib.create_album(album_name)
            album.add(todo)
            print(f"  added {len(todo)}")

    for p in problems:
        print("PROBLEM:", p)
    if not args.apply:
        print("check only: nothing changed")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="apply sync_plan.csv through Photos.app")
    ap.add_argument("--check", action="store_true",
                    help="like --apply, but only report what it would change")
    ap.add_argument("--mac-db", help="copy of the Mac's Photos.sqlite (default: "
                    "./Photos.sqlite if present, else read the library directly)")
    ap.add_argument("--phone-db", metavar="DIR", help="folder with an already-copied "
                    "phone Photos.sqlite (default: copy it from the connected phone)")
    ap.add_argument("--favorites", action="append", default=[], metavar="DIR",
                    help="fallback: folder of favorites exported from the phone")
    ap.add_argument("--album", action="append", default=[], metavar="NAME=DIR",
                    help="fallback: folder exported from a phone album")
    ap.add_argument("--plan", default=DEFAULT_PLAN)
    args = ap.parse_args()
    if args.apply or args.check:
        cmd_apply(args)
    else:
        cmd_plan(args)


if __name__ == "__main__":
    main()
