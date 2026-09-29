"""The library in DeoVR's own format, so DeoVR can browse it without DLNA.

DeoVR reads a JSON list from any address you type into it. That is a better road than DLNA for a
VR player: the projection of each video is stated outright rather than smuggled into the title,
thumbnails and lengths come with it, there is a password step, and nothing depends on discovery
finding its way across the network.

The feed sends a short entry per video - title, length, thumbnail, and where the full details are -
and DeoVR asks for the details of whatever the viewer opens. A library of a few thousand scenes
therefore stays a small download, however many folders a scene appears in.
"""
from __future__ import annotations

import os

from blueshelf.db import Database

LAYOUT_SBS, LAYOUT_TB = 1, 2
PROJ_180, PROJ_360, PROJ_FISHEYE = 0, 1, 2

# What DeoVR calls each projection. The fisheye names are lenses, not angles: a scene shot on a
# 200° MKX lens is stretched or cropped if it is handed over as plain 180° fisheye.
def screen_type(video: dict) -> str:
    if not video.get("is_vr"):
        return "flat"
    projection = video.get("vr_proj") or 0
    if projection == PROJ_360:
        return "sphere"
    if projection == PROJ_FISHEYE:
        lens = float(video.get("vr_lens_fov") or 190)
        if lens <= 185:
            return "fisheye"
        if lens <= 195:
            return "rf52"
        if lens <= 210:
            return "mkx200"
        return "vrca220"
    return "dome"


def stereo_mode(video: dict) -> str:
    return {LAYOUT_SBS: "sbs", LAYOUT_TB: "tb"}.get(video.get("vr_layout") or 0, "off")


def is_passthrough(video: dict) -> bool:
    """An AR passthrough scene, by the category BlueShelf gave it."""
    return "passthrough" in (video.get("tag_names") or "").lower()


def title_of(video: dict) -> str:
    """The name to show. Nothing is smuggled into it: the link carries what DeoVR needs."""
    return video.get("title") or os.path.splitext(os.path.basename(video["path"]))[0]


def thumbnail(video: dict, base: str) -> str:
    return f"{base}/thumb/{video['id']}.jpg" if video.get("thumb") else ""


def video_url(video: dict, base: str) -> str:
    """The link to the file itself, ending in its real extension.

    Players built on Android's media stack work the container out from the end of the address, so
    a link like /video/12 is refused before a byte is fetched, while /video/12.mp4 plays.
    """
    extension = os.path.splitext(video.get("path") or "")[1].lower() or ".mp4"
    # DeoVR reads the alpha flag from the file name in the link, not from anything in the feed:
    # "Adding _alpha to the file name marks the video as containing a packed alpha channel." Without
    # it, a passthrough scene is only offered chroma key, which is not what it was shot for.
    alpha = "_alpha" if is_passthrough(video) else ""
    # "via" is how a play through DeoVR is told apart from one on the headset page.
    return f"{base}/video/{video['id']}{alpha}{extension}?via=deovr"


def short_entry(video: dict, base: str, prefix: str = "") -> dict:
    """What a browsing list needs: enough to show, plus where to get the rest."""
    # No "id" here: the short format does not carry one, and DeoVR appears to treat a repeated id
    # as the same scene, which left every performer tab empty because the views listed them first.
    return {
        "title": f"{prefix} - {title_of(video)}" if prefix else title_of(video),
        "videoLength": int(video.get("duration") or 0),
        "thumbnailUrl": thumbnail(video, base),
        "video_url": f"{base}/deovr/video/{video['id']}",
    }


def full_entry(video: dict, base: str) -> dict:
    """Everything DeoVR needs to play one scene the right way round."""
    codec = (video.get("codec") or video.get("vcodec") or "").lower() or "h264"
    height = int(video.get("height") or 0)
    entry = {
        "id": video["id"],
        "title": title_of(video),
        "videoLength": int(video.get("duration") or 0),
        "is3d": (video.get("vr_layout") or 0) != 0,
        "screenType": screen_type(video),
        "stereoMode": stereo_mode(video),
        "thumbnailUrl": thumbnail(video, base),
        "encodings": [{
            "name": "hevc" if codec in ("hevc", "h265") else "h264",
            "videoSources": [{"resolution": height, "url": video_url(video, base)}],
        }],
    }
    resume = int(video.get("resume") or video.get("resume_pos") or 0)
    if resume > 30:
        entry["skipIntro"] = resume  # carry on where the desktop or the headset page left off
    return entry


# DeoVR shows every folder as a tab across the top of the screen, with no folders inside folders
# and no search, so a tab per performer becomes a wall of names. Performers are gathered into a
# few alphabetical tabs instead, and each scene says who is in it, which is what the names were
# for. Someone who would rather have it one way or the other can say so in the sharing window.
LAYOUTS = ("grouped", "performers", "simple")
PERFORMER_FOLDERS = 40
PERFORMER_MINIMUM = 2
# DeoVR stops drawing a list somewhere around a hundred and fifty entries, so a tab holding more
# than that hides the rest with no way to reach it. Tabs are therefore packed to a size it will
# show whole, and named after the stretch of performers inside them.
TAB_LIMIT = 120


def views_for(db: Database) -> list[tuple[str, list[dict]]]:
    """The tabs that are not about performers. Newest first, so the cap cuts the oldest."""
    return [
        ("Recently added", db.list_videos(view="recent", sort="added", descending=True)),
        ("Unwatched", db.list_videos(view="unwatched", sort="added", descending=True)),
        ("VR", db.list_videos(view="vr", sort="added", descending=True)),
        ("Favorites", db.list_videos(view="favorites", sort="title", descending=False)),
        ("All videos", db.list_videos(sort="added", descending=True)),
    ]


def shorten(name: str) -> str:
    """A performer's name cut down to something that fits on a tab."""
    parts = (name or "?").split()
    return (parts[0][:6] + (" " + parts[1][:1] if len(parts) > 1 else "")) if parts else "?"


def tab_name(first: str, last: str) -> str:
    """The stretch of performers a tab covers - or just the one, when it holds only one."""
    return shorten(first) if first == last else f"{shorten(first)} - {shorten(last)}"


def performer_groups(db: Database) -> list[tuple[str, list[tuple[str, dict]]]]:
    """Performers in alphabetical order, packed into tabs DeoVR will show to the end."""
    performers = sorted(db.performer_counts(False), key=lambda row: row["name"].lower())
    tabs: list[tuple[str, list[tuple[str, dict]]]] = []
    batch: list[tuple[str, dict]] = []
    first = ""
    for performer in performers:
        videos = db.list_videos(performer_id=performer["id"], sort="title", descending=False)
        if not videos:
            continue
        if batch and len(batch) + len(videos) > TAB_LIMIT:
            tabs.append((tab_name(first, batch[-1][0]), batch))
            batch, first = [], ""
        first = first or performer["name"]
        batch.extend((performer["name"], video) for video in videos)
    if batch:
        tabs.append((tab_name(first, batch[-1][0]), batch))

    packed = []
    for name, entries in tabs:
        seen: set[int] = set()
        kept = []
        for who, video in entries:
            if video["id"] in seen:
                continue  # a scene with two performers in one tab belongs there once
            seen.add(video["id"])
            kept.append((who, video))
        packed.append((name, kept))
    return packed


def scenes_for(db: Database, layout: str = "grouped") -> list[tuple[str, list[tuple[str, dict]]]]:
    """The tabs DeoVR shows: (name, [(who to credit, video)])."""
    listing: list[tuple[str, list[tuple[str, dict]]]] = [
        (name, [("", video) for video in videos]) for name, videos in views_for(db)]
    if layout == "grouped":
        listing += performer_groups(db)
    elif layout == "performers":
        performers = [row for row in db.performer_counts(False) if row["n"] >= PERFORMER_MINIMUM]
        performers.sort(key=lambda row: (-row["n"], row["name"].lower()))
        for performer in performers[:PERFORMER_FOLDERS]:
            videos = db.list_videos(performer_id=performer["id"], sort="title", descending=False)
            listing.append((performer["name"], [("", video) for video in videos]))
    return [(name, entries) for name, entries in listing if entries]


def feed(db: Database, base: str, authorized: str = "1", layout: str = "grouped") -> dict:
    return {
        "scenes": [{"name": name, "list": [short_entry(video, base, prefix) for prefix, video in entries]}
                   for name, entries in scenes_for(db, layout)],
        "authorized": authorized,
    }


def locked(reason: str = "0") -> dict:
    """An empty library with the status DeoVR reads: 0 asks for a password, -1 says it was wrong."""
    return {"scenes": [], "authorized": reason}


def single(db: Database, video_id: int, base: str) -> dict | None:
    rows = db.list_videos(video_id=video_id)  # hidden and trashed videos are not listed either
    return full_entry(rows[0], base) if rows else None
