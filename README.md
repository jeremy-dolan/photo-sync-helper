# photo-sync-helper

Copy your iPhone's **favorites** and **album** classifications into Photos on
the Mac after a USB import.

When you import photos from an iPhone into Photos.app over a USB cable,
the photos come across with their Live Photo videos, edits and locations,
but some things are lost:

- **Favorites.** Hearted photos arrive as ordinary photos.
- **Albums.** Albums you made on the phone don't come across.
- **Hidden photos.** These aren't imported at all; see
  [Limitations](#limitations).

If you use iCloud Photos, none of this matters, because iCloud keeps
everything in sync. This tool is for people who keep their library on
the Mac and move photos over by cable (e.g., for cost or privacy).

`photo-sync-helper.py` reads the iPhone's own Photos database directly
over USB. It then:

1. checks that every photo and video in the phone's camera roll is in
   your Mac library, and lists any that aren't
2. writes a plan indicating which photos on Mac need favorite or album
   classification applied.
3. after you review the plan, marks those photos as favorites in Photos
   and adds them to albums of the same names

## What it changes

- **On the iPhone: nothing.** It copies the phone's Photos database to a
  temporary folder on the Mac, reads it, and deletes the copy.
- **In Photos on the Mac:** Makes changes only when you run `--apply`, and only
  two things: marking photos as favorites, and adding photos to albums
  (creating an album if one with that name doesn't exist yet). It never removes
  a favorite, takes a photo out of an album, or deletes anything.

Changes are made by Photos.app itself, through its scripting interface, just as
if you'd clicked the heart. The Photos database is never written to directly.

## Requirements

- macOS with Photos.app, and Python 3.10 or later
- An iPhone connected by USB, unlocked, and trusted by this Mac (tap
  "Trust" on the phone the first time)
- [osxphotos](https://github.com/RhetTbull/osxphotos), which reads the
  Mac library, and
  [pymobiledevice3](https://github.com/doronz88/pymobiledevice3), which
  talks to the iPhone

Tested on macOS 26.6 with an iPhone running iOS 26.7.1. The phone's
database format is undocumented and could change with a future iOS
release; the script stops with an error rather than guessing if it
can't read it.

## Setup

```
git clone <this repo> && cd <this repo>
python3 -m venv .venv
.venv/bin/pip install osxphotos pymobiledevice3
```

## Usage

1. **Import** your photos in Photos.app over USB as usual. Leave "Delete
   items after import" off, so the photos stay on the phone until you've
   checked them. Keep the phone plugged in and unlocked.

2. **Give the script access to your Photos library.** Choose one:
   - Give your terminal app **Full Disk Access** (System Settings →
     Privacy & Security → Full Disk Access), then quit and reopen it.
     The script can then read the library directly.
   - Or copy the library's database next to the script after each
     import: quit Photos, right-click *Photos Library* in ~/Pictures →
     Show Package Contents → `database`, and copy `Photos.sqlite` (and
     `Photos.sqlite-wal`, if there is one) into the script's folder,
     replacing any old copy. The script uses this copy if it's there,
     and tells you how old it is. It only reads the copy; delete it
     when you're done.

3. **Make a plan.** This step changes nothing.
   ```
   .venv/bin/python photo-sync-helper.py
   ```
   It writes `sync_plan.csv` and prints a summary, e.g.:
   ```
   phone: 249 camera-roll items (23 favorites, 12 in albums, 0 hidden)
   on the Mac: 246 matched (246 by size, 0 by date+dimensions), 0 uncertain, 3 not in the library
   favorites to carry over: 23 (0 already favorites in this copy of the Mac library)
   album 'Trip to Maine': 12 photos

   on the phone but not in the Mac library (not imported, or deleted from the Mac since):
     IMG_0243.PNG     2026-10-21 19:18  in the Mac's Recently Deleted
     ...
   ```
   Photos that are on the phone but not in your library were either never
   imported or deleted from the Mac afterwards. Photos still in the Mac's
   Recently Deleted are labeled as such. Import any that should be there.

   Besides the changes `--apply` will make, `sync_plan.csv` lists these
   missing photos and any uncertain (`review`) matches.

4. **Apply.** The first time, macOS asks whether your terminal may
   control Photos; allow it.
   ```
   .venv/bin/python photo-sync-helper.py --check   # optional: what would change
   .venv/bin/python photo-sync-helper.py --apply
   ```
   Running `--check` again afterwards should report nothing left to do.

You can then free up space by deleting the imported photos from the
phone. Back up your Mac first, since its library will now be the only
copy. The phone's Recently Deleted album keeps them for 30 days.

## How photos are matched

Photos are matched by their original filename (e.g. `IMG_0123.HEIC`)
together with:

1. **the exact original file size and capture date.** This is the normal
   case, and is effectively unique.
2. **otherwise, capture time (within 5 seconds) and pixel dimensions.**
3. **otherwise, capture time alone.** Such matches are marked `review`
   in the plan and skipped by `--apply` unless you change their status
   to `ok` in the CSV.

A filename alone isn't enough: iPhones restart their numbering after
IMG_9999, and photos received from other people keep the names their
phones gave them. If you duplicated a photo on the phone, each copy is
matched to its own copy on the Mac, with edited copies paired with
edited ones.

## If the phone can't be read

If a future iOS stops allowing the database to be read, you can
export instead. On the phone, open the Favorites album (or any other
album), select all, and AirDrop the photos to the Mac; under Options,
turn on "All Photos Data". Put each export in its own folder, then:

```
.venv/bin/python photo-sync-helper.py --favorites favorites_export \
    --album "Trip to Maine=maine_export"
```

Then use `--check` and `--apply` as above. This mode can't check whether
every photo was imported, only match the exported files.

## Limitations

- **Hidden photos aren't imported over USB at all.** The photos are
  left out, not just their hidden status. The script lists hidden photos
  it finds on the phone. To bring them over, unhide them before
  importing, or AirDrop them to the Mac, import them, and hide them
  there (⌘L).
- **Only album membership is copied.** The order of photos within an
  album, folders of albums, and cover photos aren't. Albums are created
  at the top level in Photos.
- **Only the camera roll is checked.** Photos synced to the phone from a
  computer, and iCloud Shared Albums, are skipped, because a USB import
  doesn't bring those over either.
- **Captions, people's names and other phone-only details** aren't
  carried over.

## Privacy

Everything happens locally on your Mac. The phone's database is copied
to a temporary folder, read, and deleted. Nothing is sent anywhere.

This project isn't affiliated with Apple. Back up your Photos library
before applying changes to it.
