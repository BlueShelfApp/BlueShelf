"""BlueShelf as a DLNA media server, so TVs, consoles and VR players can browse the library.

DLNA is how a TV's "Media" app, a PlayStation, VLC, Kodi, and the Quest's own VR players - DeoVR,
Skybox, HereSphere - find video on a home network. Three pieces make it work:

  * SSDP, a multicast chatter on UDP port 1900 that announces "a media server is here".
  * A description of the server and its two services, as XML over plain HTTP.
  * ContentDirectory, answering "what is in this folder?" in SOAP with DIDL-Lite listings.

The folders are the same views as the desktop sidebar - performers, studios, categories, VR,
favourites - so a TV browses the library the way BlueShelf organised it, not the way the files
happen to sit on the share.

DLNA has no passwords. Anything on the network that speaks it can list and play what is shared,
which is why this is off until switched on, and why hidden videos are never listed.
"""
from __future__ import annotations

import os
import random
import re
import socket
import struct
import threading
import time
import uuid as uuidlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from blueshelf import __version__, paths
from blueshelf.db import Database

DEFAULT_PORT = 8200
SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
MAX_AGE = 1800
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
CHUNK = 256 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")

# The fourth field of a DLNA protocolInfo: byte seeking allowed, not transcoded, streaming mode.
DLNA_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"

MIME_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv", ".mov": "video/quicktime", ".webm": "video/webm", ".ts": "video/mp2t",
    ".m2ts": "video/mp2t", ".mts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".flv": "video/x-flv", ".3gp": "video/3gpp",
}

# The folders at the top of the tree: (id, name, which library view fills it).
TOP = [
    ("all", "All videos", "all"),
    ("performers", "Performers", None),
    ("studios", "Studios", None),
    ("tags", "Categories", None),
    ("vr", "VR", "vr"),
    ("favorites", "Favorites", "favorites"),
    ("recent", "Recently added", "recent"),
    ("unwatched", "Unwatched", "unwatched"),
]
TOP_BY_ID = {entry[0]: entry for entry in TOP}

DIDL_OPEN = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
             'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">')


class NoSuchObject(Exception):
    """UPnP error 701: the client asked for a folder or item that is not there."""


def mime_for(path: str) -> str:
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(), "video/mp4")


def clock(seconds: float) -> str:
    """DLNA's duration format, H:MM:SS.mmm."""
    seconds = max(0.0, float(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:06.3f}"


def is_passthrough(video: dict) -> bool:
    """Whether this is an AR passthrough scene, by the category BlueShelf gave it."""
    return "passthrough" in (video.get("tag_names") or "").lower()


def vr_suffix(video: dict) -> str:
    """The filename tags VR players read to pick a projection, such as _180_180x180_LR.

    DeoVR, Skybox and HereSphere only see the title a server sends, never the file, so without
    these a 180 side-by-side scene opens as a flat, double picture. The two players mostly agree -
    LR and TB for the eye layout, 360, FISHEYE190 - but not on 180: DeoVR reads "180", Skybox reads
    "180x180". Both are sent, and each player ignores the one it does not know.
    """
    if not video.get("is_vr"):
        return ""
    projection = video.get("vr_proj") or 0
    if projection == 1:
        angle = "360"
    elif projection == 2:
        # DeoVR names fisheye by the lens that shot it; Skybox only knows the 190 one.
        lens = int(video.get("vr_lens_fov") or 190)
        angle = "FISHEYE190" if lens <= 195 else "MKX200" if lens <= 210 else "VRCA220"
    else:
        angle = "180_180x180"
    layout = {1: "_LR", 2: "_TB"}.get(video.get("vr_layout") or 0, "")
    # A passthrough scene carries a cut-out mask beside the picture. Players only switch that on
    # when the name says ALPHA, and without it the mask plays as a grey strip beside the scene.
    alpha = "_ALPHA" if is_passthrough(video) else ""
    return f"_{angle}{layout}{alpha}"


# --------------------------------------------------------------------------- the content tree

class Library:
    """Turns the library into DLNA folders and items. Knows nothing about the network."""

    def __init__(self, db: Database):
        self.db = db

    def _videos(self, container: str) -> list[dict]:
        if container in TOP_BY_ID and TOP_BY_ID[container][2]:
            view = TOP_BY_ID[container][2]
            ordering = ("title", False) if view in ("all", "vr", "favorites", "unwatched") else ("added", True)
            return self.db.list_videos(view=view, sort=ordering[0], descending=ordering[1])
        kind, _, key = container.partition(":")
        if kind == "p" and key.isdigit():
            return self.db.list_videos(performer_id=int(key), sort="title", descending=False)
        if kind == "t" and key.isdigit():
            return self.db.list_videos(tag_id=int(key), sort="title", descending=False)
        if kind == "s" and key:
            return self.db.list_videos(studio=key, sort="title", descending=False)
        raise NoSuchObject(container)

    def _groups(self, container: str) -> list[dict]:
        if container == "performers":
            return [self.folder(f"p:{row['id']}", "performers", row["name"], row["n"])
                    for row in self.db.performer_counts(False)]
        if container == "studios":
            return [self.folder(f"s:{key}", "studios", name, count)
                    for name, count, key in self.db.studio_counts(False)]
        if container == "tags":
            return [self.folder(f"t:{row['id']}", "tags", row["name"], row["n"])
                    for row in self.db.tag_counts(False)]
        raise NoSuchObject(container)

    @staticmethod
    def folder(object_id: str, parent: str, title: str, count: int) -> dict:
        return {"kind": "container", "id": object_id, "parent": parent, "title": title, "count": count}

    @staticmethod
    def item(parent: str, video: dict) -> dict:
        return {"kind": "item", "id": f"{parent}/{video['id']}", "parent": parent, "video": video}

    def children(self, object_id: str) -> list[dict]:
        if object_id == "0":
            views = self.db.view_counts(False)
            counts = {"performers": len(self.db.performer_counts(False)),
                      "studios": len(self.db.studio_counts(False)),
                      "tags": len(self.db.tag_counts(False))}
            return [self.folder(key, "0", title, views.get(view, 0) if view else counts[key])
                    for key, title, view in TOP]
        if object_id in ("performers", "studios", "tags"):
            return self._groups(object_id)
        return [self.item(object_id, video) for video in self._videos(object_id)]

    def metadata(self, object_id: str) -> dict:
        if object_id == "0":
            return {"kind": "container", "id": "0", "parent": "-1", "title": "BlueShelf", "count": len(TOP)}
        if object_id in TOP_BY_ID:
            return self.folder(object_id, "0", TOP_BY_ID[object_id][1], len(self.children(object_id)))
        if "/" in object_id:
            parent, _, video_id = object_id.rpartition("/")
            if video_id.isdigit():
                rows = self.db.list_videos(video_id=int(video_id))
                if rows:
                    return self.item(parent, rows[0])
            raise NoSuchObject(object_id)
        kind, _, key = object_id.partition(":")
        parent = {"p": "performers", "s": "studios", "t": "tags"}.get(kind)
        if parent is None:
            raise NoSuchObject(object_id)
        for group in self._groups(parent):
            if group["id"] == object_id:
                return group
        raise NoSuchObject(object_id)

    # ------------------------------------------------------------ DIDL-Lite

    def didl(self, entries: list[dict], base: str) -> str:
        parts = [DIDL_OPEN]
        for entry in entries:
            if entry["kind"] == "container":
                parts.append(
                    f'<container id="{escape(entry["id"])}" parentID="{escape(entry["parent"])}" restricted="1" '
                    f'searchable="0" childCount="{entry["count"]}">'
                    f'<dc:title>{escape(entry["title"])}</dc:title>'
                    '<upnp:class>object.container.storageFolder</upnp:class></container>')
            else:
                parts.append(self._item_xml(entry, base))
        parts.append("</DIDL-Lite>")
        return "".join(parts)

    @staticmethod
    def _item_xml(entry: dict, base: str) -> str:
        video = entry["video"]
        path = video["path"]
        title = (video.get("title") or os.path.splitext(os.path.basename(path))[0]) + vr_suffix(video)
        mime = mime_for(path)
        extension = os.path.splitext(path)[1].lower() or ".mp4"
        attributes = [f'protocolInfo="http-get:*:{mime}:{DLNA_FEATURES}"', f'size="{int(video.get("size") or 0)}"']
        if video.get("duration"):
            attributes.append(f'duration="{clock(video["duration"])}"')
        if video.get("width") and video.get("height"):
            attributes.append(f'resolution="{int(video["width"])}x{int(video["height"])}"')
        performers = [name.strip() for name in (video.get("performer_names") or "").split(",") if name.strip()]
        tags = [name.strip() for name in (video.get("tag_names") or "").split(",") if name.strip()]

        xml = [f'<item id="{escape(entry["id"])}" parentID="{escape(entry["parent"])}" restricted="1">',
               f"<dc:title>{escape(title)}</dc:title>",
               "<upnp:class>object.item.videoItem</upnp:class>"]
        if performers:
            xml.append(f"<dc:creator>{escape(', '.join(performers))}</dc:creator>")
            xml.extend(f"<upnp:actor>{escape(name)}</upnp:actor>" for name in performers)
        xml.extend(f"<upnp:genre>{escape(name)}</upnp:genre>" for name in tags)
        if video.get("studio"):
            xml.append(f"<dc:publisher>{escape(video['studio'])}</dc:publisher>")
        if video.get("release_date"):
            xml.append(f"<dc:date>{escape(str(video['release_date']))}</dc:date>")
        if video.get("thumb"):
            xml.append(f'<upnp:albumArtURI dlna:profileID="JPEG_TN">{base}/thumb/{video["id"]}.jpg</upnp:albumArtURI>')
        xml.append(f'<res {" ".join(attributes)}>{base}/media/{video["id"]}{escape(extension)}</res>')
        xml.append("</item>")
        return "".join(xml)


# --------------------------------------------------------------------------- SOAP and XML

def soap_envelope(body: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f"<s:Body>{body}</s:Body></s:Envelope>").encode("utf-8")


def soap_response(service: str, action: str, values: dict) -> bytes:
    inner = "".join(f"<{key}>{escape(str(value))}</{key}>" for key, value in values.items())
    return soap_envelope(f'<u:{action}Response xmlns:u="{service}">{inner}</u:{action}Response>')


def soap_fault(code: int, description: str) -> bytes:
    return soap_envelope(
        "<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode><errorDescription>{escape(description)}</errorDescription>"
        "</UPnPError></detail></s:Fault>")


def soap_arguments(body: bytes) -> tuple[str, dict[str, str]]:
    """The action name and its arguments, whatever namespace prefixes the client chose."""
    root = ElementTree.fromstring(body)
    for element in root.iter():
        if element.tag.split("}")[-1] == "Body":
            for action in element:
                name = action.tag.split("}")[-1]
                return name, {child.tag.split("}")[-1]: (child.text or "") for child in action}
    raise ValueError("no SOAP body")


def _argument(name: str, direction: str, variable: str) -> str:
    return (f"<argument><name>{name}</name><direction>{direction}</direction>"
            f"<relatedStateVariable>{variable}</relatedStateVariable></argument>")


def _action(name: str, *arguments: str) -> str:
    return f"<action><name>{name}</name><argumentList>{''.join(arguments)}</argumentList></action>"


def _variable(name: str, kind: str, events: bool = False, allowed: tuple[str, ...] = ()) -> str:
    values = ""
    if allowed:
        values = "<allowedValueList>" + "".join(f"<allowedValue>{v}</allowedValue>" for v in allowed) + \
                 "</allowedValueList>"
    return (f'<stateVariable sendEvents="{"yes" if events else "no"}"><name>{name}</name>'
            f"<dataType>{kind}</dataType>{values}</stateVariable>")


def _scpd(actions: list[str], variables: list[str]) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<scpd xmlns="urn:schemas-upnp-org:service-1-0"><specVersion><major>1</major><minor>0</minor>'
            f"</specVersion><actionList>{''.join(actions)}</actionList>"
            f"<serviceStateTable>{''.join(variables)}</serviceStateTable></scpd>").encode("utf-8")


CDS_SCPD = _scpd(
    [_action("Browse",
             _argument("ObjectID", "in", "A_ARG_TYPE_ObjectID"),
             _argument("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
             _argument("Filter", "in", "A_ARG_TYPE_Filter"),
             _argument("StartingIndex", "in", "A_ARG_TYPE_Index"),
             _argument("RequestedCount", "in", "A_ARG_TYPE_Count"),
             _argument("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
             _argument("Result", "out", "A_ARG_TYPE_Result"),
             _argument("NumberReturned", "out", "A_ARG_TYPE_Count"),
             _argument("TotalMatches", "out", "A_ARG_TYPE_Count"),
             _argument("UpdateID", "out", "A_ARG_TYPE_UpdateID")),
     _action("GetSearchCapabilities", _argument("SearchCaps", "out", "SearchCapabilities")),
     _action("GetSortCapabilities", _argument("SortCaps", "out", "SortCapabilities")),
     _action("GetSystemUpdateID", _argument("Id", "out", "SystemUpdateID"))],
    [_variable("A_ARG_TYPE_ObjectID", "string"),
     _variable("A_ARG_TYPE_Result", "string"),
     _variable("A_ARG_TYPE_BrowseFlag", "string", allowed=("BrowseMetadata", "BrowseDirectChildren")),
     _variable("A_ARG_TYPE_Filter", "string"),
     _variable("A_ARG_TYPE_SortCriteria", "string"),
     _variable("A_ARG_TYPE_Index", "ui4"),
     _variable("A_ARG_TYPE_Count", "ui4"),
     _variable("A_ARG_TYPE_UpdateID", "ui4"),
     _variable("SearchCapabilities", "string"),
     _variable("SortCapabilities", "string"),
     _variable("SystemUpdateID", "ui4", events=True)])

CMS_SCPD = _scpd(
    [_action("GetProtocolInfo",
             _argument("Source", "out", "SourceProtocolInfo"),
             _argument("Sink", "out", "SinkProtocolInfo")),
     _action("GetCurrentConnectionIDs", _argument("ConnectionIDs", "out", "CurrentConnectionIDs")),
     _action("GetCurrentConnectionInfo",
             _argument("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"),
             _argument("RcsID", "out", "A_ARG_TYPE_RcsID"),
             _argument("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"),
             _argument("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
             _argument("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
             _argument("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"),
             _argument("Direction", "out", "A_ARG_TYPE_Direction"),
             _argument("Status", "out", "A_ARG_TYPE_ConnectionStatus"))],
    [_variable("SourceProtocolInfo", "string", events=True),
     _variable("SinkProtocolInfo", "string", events=True),
     _variable("CurrentConnectionIDs", "string", events=True),
     _variable("A_ARG_TYPE_ConnectionStatus", "string",
               allowed=("OK", "ContentFormatMismatch", "InsufficientBandwidth", "UnreliableChannel", "Unknown")),
     _variable("A_ARG_TYPE_ConnectionManager", "string"),
     _variable("A_ARG_TYPE_Direction", "string", allowed=("Input", "Output")),
     _variable("A_ARG_TYPE_ProtocolInfo", "string"),
     _variable("A_ARG_TYPE_ConnectionID", "i4"),
     _variable("A_ARG_TYPE_AVTransportID", "i4"),
     _variable("A_ARG_TYPE_RcsID", "i4")])

SOURCE_PROTOCOLS = ",".join(f"http-get:*:{mime}:*" for mime in sorted(set(MIME_TYPES.values())))


def description(name: str, udn: str) -> bytes:
    def service(kind: str, short: str) -> str:
        return (f"<service><serviceType>{kind}</serviceType><serviceId>urn:upnp-org:serviceId:{short}</serviceId>"
                f"<SCPDURL>/dlna/{short}.xml</SCPDURL><controlURL>/dlna/control/{short}</controlURL>"
                f"<eventSubURL>/dlna/event/{short}</eventSubURL></service>")

    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
            f"<deviceType>{DEVICE_TYPE}</deviceType>"
            f"<friendlyName>{escape(name)}</friendlyName>"
            "<manufacturer>BlueShelf</manufacturer>"
            "<modelDescription>BlueShelf video library</modelDescription>"
            "<modelName>BlueShelf</modelName>"
            f"<modelNumber>{__version__}</modelNumber>"
            f"<UDN>uuid:{udn}</UDN>"
            "<dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>"
            f"<serviceList>{service(CDS, 'ContentDirectory')}{service(CMS, 'ConnectionManager')}</serviceList>"
            "</device></root>").encode("utf-8")


# --------------------------------------------------------------------------- HTTP

class _Handler(BaseHTTPRequestHandler):
    server_version = f"BlueShelf/{__version__} UPnP/1.0 DLNADOC/1.50"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str = "text/xml; charset=\"utf-8\"",
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    @property
    def base(self) -> str:
        """The address the client reached us on, so every link it is given is one it can open."""
        host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"http://{host}"

    def do_GET(self) -> None:
        self._route()

    def do_HEAD(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def do_SUBSCRIBE(self) -> None:
        # Some clients refuse a server that will not take event subscriptions, even though the
        # library changes too rarely to be worth sending events for.
        self._send(HTTPStatus.OK, b"", extra={"SID": f"uuid:{uuidlib.uuid4()}", "TIMEOUT": "Second-1800"})

    def do_UNSUBSCRIBE(self) -> None:
        self._send(HTTPStatus.OK, b"")

    def _route(self) -> None:
        if not self.server.allowed(self.client_address[0]):
            self._send(HTTPStatus.FORBIDDEN, b"")
            return
        route = unquote(urlparse(self.path).path)
        try:
            if route == "/dlna/description.xml":
                self._send(HTTPStatus.OK, description(self.server.name, self.server.udn))
            elif route == "/dlna/ContentDirectory.xml":
                self._send(HTTPStatus.OK, CDS_SCPD)
            elif route == "/dlna/ConnectionManager.xml":
                self._send(HTTPStatus.OK, CMS_SCPD)
            elif route == "/dlna/control/ContentDirectory" and self.command == "POST":
                self._content_directory()
            elif route == "/dlna/control/ConnectionManager" and self.command == "POST":
                self._connection_manager()
            elif route.startswith("/media/"):
                self._stream(route)
            elif route.startswith("/thumb/"):
                self._thumb(route)
            else:
                self._send(HTTPStatus.NOT_FOUND, b"")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the TV skipped ahead or moved on

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _content_directory(self) -> None:
        try:
            action, arguments = soap_arguments(self._body())
        except (ElementTree.ParseError, ValueError):
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, soap_fault(402, "Invalid Args"))
            return
        library = Library(self.server.database_for_thread())
        if action == "Browse":
            try:
                result = self.server.browse(library, arguments, self.base)
            except NoSuchObject:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, soap_fault(701, "No such object"))
                return
            self._send(HTTPStatus.OK, soap_response(CDS, "Browse", result))
        elif action == "GetSearchCapabilities":
            self._send(HTTPStatus.OK, soap_response(CDS, action, {"SearchCaps": ""}))
        elif action == "GetSortCapabilities":
            self._send(HTTPStatus.OK, soap_response(CDS, action, {"SortCaps": "dc:title"}))
        elif action == "GetSystemUpdateID":
            self._send(HTTPStatus.OK, soap_response(CDS, action, {"Id": self.server.update_id}))
        else:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, soap_fault(401, "Invalid Action"))

    def _connection_manager(self) -> None:
        try:
            action, _arguments = soap_arguments(self._body())
        except (ElementTree.ParseError, ValueError):
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, soap_fault(402, "Invalid Args"))
            return
        if action == "GetProtocolInfo":
            self._send(HTTPStatus.OK, soap_response(CMS, action, {"Source": SOURCE_PROTOCOLS, "Sink": ""}))
        elif action == "GetCurrentConnectionIDs":
            self._send(HTTPStatus.OK, soap_response(CMS, action, {"ConnectionIDs": "0"}))
        elif action == "GetCurrentConnectionInfo":
            self._send(HTTPStatus.OK, soap_response(CMS, action, {
                "RcsID": -1, "AVTransportID": -1, "ProtocolInfo": "", "PeerConnectionManager": "",
                "PeerConnectionID": -1, "Direction": "Output", "Status": "OK"}))
        else:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, soap_fault(401, "Invalid Action"))

    def _video(self, route: str) -> dict | None:
        name = route.rsplit("/", 1)[-1]
        video_id = name.split(".", 1)[0]
        if not video_id.isdigit():
            return None
        rows = self.server.database_for_thread().list_videos(video_id=int(video_id))
        return rows[0] if rows else None  # hidden and trashed videos are not served either

    def _thumb(self, route: str) -> None:
        video = self._video(route)
        path = paths.thumbs_dir() / f"{video['id']}.jpg" if video else None
        if path is None or not path.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"")
            return
        self._send(HTTPStatus.OK, path.read_bytes(), "image/jpeg",
                   {"contentFeatures.dlna.org": "DLNA.ORG_PN=JPEG_TN;DLNA.ORG_OP=01;DLNA.ORG_CI=0"})

    def _stream(self, route: str) -> None:
        video = self._video(route)
        if video is None or not os.path.isfile(video["path"]):
            self._send(HTTPStatus.NOT_FOUND, b"")
            return
        path = video["path"]
        size = os.path.getsize(path)
        start, end = 0, size - 1
        requested = self.headers.get("Range") or ""
        if not requested or requested.startswith("bytes=0-"):
            from blueshelf.server import count_play
            count_play(self.server.database_for_thread(), video["id"], self.client_address[0], "dlna")
        match = _RANGE.match(requested)
        if match:
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            elif match.group(2):
                start = max(0, size - int(match.group(2)))
        if start >= size:
            self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", extra={"Content-Range": f"bytes */{size}"})
            return
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if match else HTTPStatus.OK)
        self.send_header("Content-Type", mime_for(path))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        # TVs check these before they will play anything, and some refuse a file without them.
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", DLNA_FEATURES)
        if match:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                data = handle.read(min(CHUNK, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, name: str, udn: str):
        super().__init__(address, _Handler)
        self.name = name
        self.udn = udn
        self.update_id = int(time.time()) % 2_000_000_000
        self._local = threading.local()

    def database_for_thread(self) -> Database:
        if getattr(self._local, "db", None) is None:
            self._local.db = Database(str(paths.db_path()))
        return self._local.db

    @staticmethod
    def allowed(address: str) -> bool:
        """Only the local network: private, link-local and loopback addresses."""
        try:
            first, second = (int(part) for part in address.split(".")[:2])
        except ValueError:
            return address in ("::1",)
        return (first == 10 or first == 127 or (first == 172 and 16 <= second <= 31)
                or (first == 192 and second == 168) or (first == 169 and second == 254))

    def browse(self, library: Library, arguments: dict[str, str], base: str) -> dict:
        object_id = arguments.get("ObjectID") or "0"
        flag = arguments.get("BrowseFlag") or "BrowseDirectChildren"
        if flag == "BrowseMetadata":
            entries, total = [library.metadata(object_id)], 1
        else:
            everything = library.children(object_id)
            total = len(everything)
            start = int(arguments.get("StartingIndex") or 0)
            count = int(arguments.get("RequestedCount") or 0)
            entries = everything[start:start + count] if count else everything[start:]
        return {"Result": library.didl(entries, base), "NumberReturned": len(entries),
                "TotalMatches": total, "UpdateID": self.update_id}


# --------------------------------------------------------------------------- SSDP

_ips_cache: tuple[float, list[str]] = (0.0, [])


def _adapter_ips() -> list[str]:
    """IPv4 addresses of every network adapter that is up and can take part in multicast.

    Asked of the adapters themselves, through Qt. Working it out from the default route and the
    PC's own name missed the real network on a PC whose main route ran through a VPN or a virtual
    adapter: BlueShelf listened on the wrong adapter, heard only itself, and a headset searching
    on the real network was never answered.
    """
    try:
        from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
    except ImportError:
        return []
    flag = QNetworkInterface.InterfaceFlag
    ipv4 = QAbstractSocket.NetworkLayerProtocol.IPv4Protocol
    found: list[str] = []
    for adapter in QNetworkInterface.allInterfaces():
        flags = adapter.flags()
        if not (flags & flag.IsUp and flags & flag.IsRunning and flags & flag.CanMulticast):
            continue
        if flags & flag.IsLoopBack:
            continue
        for entry in adapter.addressEntries():
            address = entry.ip()
            if address.protocol() != ipv4:
                continue
            ip = address.toString()
            if ip.startswith("169.254.") or ip in found:  # 169.254 means "no network here"
                continue
            found.append(ip)
    return found


def local_ips() -> list[str]:
    """This PC's IPv4 addresses on working networks, the one on the default route first."""
    global _ips_cache
    now = time.time()
    if now - _ips_cache[0] < 30 and _ips_cache[1]:
        return list(_ips_cache[1])
    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    for ip in _adapter_ips():
        if ip not in found:
            found.append(ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in found:
                found.append(ip)
    except OSError:
        pass
    _ips_cache = (now, found)
    return list(found)


def ip_facing(remote: str) -> str:
    """Which of this PC's addresses a given device would reach us on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((remote, 9))
        return probe.getsockname()[0]
    except OSError:
        return (local_ips() or ["127.0.0.1"])[0]
    finally:
        probe.close()


class Ssdp:
    """Answers "is there a media server here?" and announces BlueShelf now and then unprompted."""

    def __init__(self, udn: str, http_port: int, ssdp_port: int = SSDP_PORT):
        self.udn = udn
        self.http_port = http_port
        self.ssdp_port = ssdp_port
        self.targets = ["upnp:rootdevice", f"uuid:{udn}", DEVICE_TYPE, CDS, CMS]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._socket: socket.socket | None = None
        # Shown in the sharing window, so "is a player even asking?" has an answer on screen.
        self.heard = 0            # every search on the network, for anything
        self.asked = 0            # searches for a media server - the ones BlueShelf should answer
        self.asked_remote = 0     # ...of those, from another device rather than this PC
        self.answered = 0
        self.answered_remote = 0
        self.failed = 0
        self.errors = 0
        self.last_heard = ""
        self.last_asked = ""
        self.last_answered = ""
        self.last_failure = ""

    def usn(self, target: str) -> str:
        return f"uuid:{self.udn}" if target.startswith("uuid:") else f"uuid:{self.udn}::{target}"

    def location(self, ip: str) -> str:
        return f"http://{ip}:{self.http_port}/dlna/description.xml"

    def response(self, target: str, ip: str) -> bytes:
        return ("HTTP/1.1 200 OK\r\n"
                f"CACHE-CONTROL: max-age={MAX_AGE}\r\n"
                f"DATE: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n"
                "EXT:\r\n"
                f"LOCATION: {self.location(ip)}\r\n"
                f"SERVER: Windows/10 UPnP/1.0 BlueShelf/{__version__}\r\n"
                f"ST: {target}\r\n"
                f"USN: {self.usn(target)}\r\n"
                "Content-Length: 0\r\n\r\n").encode("ascii")

    def notify(self, target: str, ip: str, alive: bool = True) -> bytes:
        lines = ["NOTIFY * HTTP/1.1", f"HOST: {SSDP_GROUP}:{self.ssdp_port}", f"NT: {target}",
                 f"NTS: ssdp:{'alive' if alive else 'byebye'}", f"USN: {self.usn(target)}"]
        if alive:
            lines += [f"CACHE-CONTROL: max-age={MAX_AGE}", f"LOCATION: {self.location(ip)}",
                      f"SERVER: Windows/10 UPnP/1.0 BlueShelf/{__version__}"]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    def answers(self, message: bytes) -> list[str]:
        """The targets a search message is asking about, or nothing when it is not for us."""
        text = message.decode("utf-8", "replace")
        if not text.upper().startswith("M-SEARCH"):
            return []
        headers = {}
        for line in text.split("\r\n")[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().upper()] = value.strip()
        if headers.get("MAN", "").strip('"') != "ssdp:discover":
            return []
        wanted = headers.get("ST", "")
        if wanted == "ssdp:all":
            return list(self.targets)
        return [wanted] if wanted in self.targets else []

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # Windows' own SSDP service already holds port 1900, so the port has to be shared with it.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("", self.ssdp_port))
        group = socket.inet_aton(SSDP_GROUP)
        joined = False
        for ip in local_ips() + ["0.0.0.0"]:
            try:
                listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + socket.inet_aton(ip))
                joined = True
            except OSError:
                continue
        if not joined:
            listener.close()
            raise OSError("could not join the SSDP multicast group")
        listener.settimeout(1.0)
        self._socket = listener
        self._stop.clear()
        for target, name in ((self._listen, "blueshelf-ssdp"), (self._announce, "blueshelf-ssdp-notify")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._broadcast(alive=False)
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads = []
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _listen(self) -> None:
        # Nothing a device sends may stop this loop. It once returned on any socket error, and one
        # oversized announcement from a chatty device - Windows reports a datagram bigger than the
        # buffer as an error rather than cutting it short - left BlueShelf announcing itself but
        # deaf to searches: visible in Skybox, invisible in DeoVR, which only searches.
        while not self._stop.is_set():
            try:
                message, sender = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set() or self._socket is None:
                    return
                self.errors += 1
                self._stop.wait(0.05)
                continue
            try:
                targets = self.answers(message)
            except Exception:
                self.errors += 1
                continue
            if message[:8].upper() == b"M-SEARCH":
                self.heard += 1
                self.last_heard = sender[0]
            if not targets:
                continue
            self.asked += 1
            self.last_asked = sender[0]
            if sender[0] not in local_ips() and not sender[0].startswith("127."):
                self.asked_remote += 1
            ip = ip_facing(sender[0])
            # Replies are spread over a moment, as the protocol asks, so a room full of servers does
            # not all answer in the same instant.
            delay = random.uniform(0.05, 0.4)
            threading.Timer(delay, self._reply, args=(targets, ip, sender)).start()

    def _reply(self, targets: list[str], ip: str, sender) -> None:
        """Answers a search from the socket it arrived on, the way other media servers do.

        A reply from a fresh socket looked, to some PCs' security software, like a new conversation
        nobody asked for, and was dropped on the way out - BlueShelf counted it as answered while
        the headset heard nothing. Going back out the way the question came in is treated as the
        answer it is. A fresh socket is still tried if the listening one will not send.
        """
        replies = [self.response(target, ip) for target in targets]
        remote = sender[0] not in local_ips() and not sender[0].startswith("127.")
        problem = ""
        for attempt in ("listener", "fresh"):
            try:
                if attempt == "listener" and self._socket is not None:
                    for reply in replies:
                        self._socket.sendto(reply, sender)
                else:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as out:
                        for reply in replies:
                            out.sendto(reply, sender)
                self.answered += 1
                if remote:
                    self.answered_remote += 1
                    self.last_answered = sender[0]
                return
            except OSError as error:
                problem = str(error)
        self.failed += 1
        self.last_failure = f"{sender[0]}: {problem}"

    def _broadcast(self, alive: bool = True) -> None:
        for ip in local_ips():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as out:
                    out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                    out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                    for target in self.targets:
                        out.sendto(self.notify(target, ip, alive), (SSDP_GROUP, self.ssdp_port))
            except OSError:
                continue

    def _announce(self) -> None:
        # Three quick announcements, because UDP loses packets, then a refresh well inside max-age.
        for _ in range(3):
            if self._stop.is_set():
                return
            self._broadcast()
            self._stop.wait(1.0)
        while not self._stop.wait(MAX_AGE / 3):
            self._broadcast()


# --------------------------------------------------------------------------- the whole server

class DlnaServer:
    """Starts and stops both halves together. Safe to call start() twice."""

    def __init__(self, name: str = "", udn: str = "", port: int = DEFAULT_PORT, ssdp_port: int = SSDP_PORT):
        self.name = name or f"BlueShelf on {socket.gethostname()}"
        self.udn = udn or str(uuidlib.uuid4())
        self.port = port
        self.ssdp_port = ssdp_port
        self._http: _HttpServer | None = None
        self._thread: threading.Thread | None = None
        self._ssdp: Ssdp | None = None

    @property
    def running(self) -> bool:
        return self._http is not None

    def start(self, announce: bool = True) -> int:
        if self._http is not None:
            return self.port
        self._http = _HttpServer(("0.0.0.0", self.port), self.name, self.udn)
        self.port = self._http.server_address[1]
        self._thread = threading.Thread(target=self._http.serve_forever, name="blueshelf-dlna", daemon=True)
        self._thread.start()
        if announce:
            self._ssdp = Ssdp(self.udn, self.port, self.ssdp_port)
            try:
                self._ssdp.start()
            except OSError:
                self._ssdp = None  # still browsable by address, just not discovered automatically
        return self.port

    @property
    def discoverable(self) -> bool:
        return self._ssdp is not None

    def searches(self) -> dict:
        """What discovery has been doing, in the terms the sharing window shows."""
        empty = {"heard": 0, "asked": 0, "asked_remote": 0, "answered": 0, "answered_remote": 0,
                 "failed": 0, "last": "", "last_asked": "", "last_answered": "", "last_failure": ""}
        if self._ssdp is None:
            return empty
        s = self._ssdp
        return {"heard": s.heard, "asked": s.asked, "asked_remote": s.asked_remote, "answered": s.answered,
                "answered_remote": s.answered_remote, "failed": s.failed, "last": s.last_heard,
                "last_asked": s.last_asked, "last_answered": s.last_answered, "last_failure": s.last_failure}

    def stop(self) -> None:
        if self._ssdp is not None:
            self._ssdp.stop()
            self._ssdp = None
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def start_saved(server: DlnaServer, db: Database) -> str:
    """Starts on the saved port, or any free one when that is taken. Returns a problem, or ''.

    Port 8200 is what many DLNA servers use, so another one on this PC is a real possibility.
    Whatever port it ends up on is kept, so the firewall rule made for it keeps working.
    """
    try:
        server.start()
    except OSError:
        server.port = 0
        try:
            server.start()
        except OSError as error:
            return str(error.strerror or error)
    db.set_setting("dlna_port", server.port)
    return ""
