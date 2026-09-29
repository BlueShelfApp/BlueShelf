"""The DLNA server: what a TV or VR player is shown, what it may play, and how it finds us."""
from __future__ import annotations

import http.client
import socket
import time
from xml.etree import ElementTree

import pytest

from blueshelf import dlna, paths
from blueshelf.db import Database
from blueshelf.parser import parse_path

NS = {
    "d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
    "dev": "urn:schemas-upnp-org:device-1-0",
    "scpd": "urn:schemas-upnp-org:service-1-0",
}
FLAT = "Site.24.03.05.Anna.Bell.Pool.Day.XXX.mp4"
VR = "VRStudio.Anna.Bell.Sunset.180_LR.mp4"
SECRET = "Site.24.03.06.Nobody.Hidden.Scene.XXX.mp4"


@pytest.fixture()
def served(tmp_path, monkeypatch):
    data, library = tmp_path / "data", tmp_path / "library"
    (data / "thumbs").mkdir(parents=True)
    library.mkdir()
    monkeypatch.setenv("BLUESHELF_DATA_DIR", str(data))
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "thumbs_dir", lambda: data / "thumbs")
    monkeypatch.setattr(paths, "db_path", lambda: data / "library.db")

    db = Database(str(data / "library.db"))
    lib_id = db.add_library(str(library))
    ids = {}
    for name in (FLAT, VR, SECRET):
        path = library / name
        path.write_bytes(bytes(range(256)) * 40)
        ids[name] = db.insert_video(lib_id, str(path), name, path.stat().st_size, 1.0, parse_path(name), True)
    db.set_fields(ids[FLAT], duration=3725.5, width=1920, height=1080, thumb=f"{ids[FLAT]}.jpg")
    db.set_fields(ids[VR], duration=60.0, width=5760, height=2880, is_vr=1, vr_layout=1, vr_proj=0,
                  title="Sunset")
    db.set_performers(ids[VR], ["Anna Bell"])  # set outright, so these tests do not lean on the parser
    db.set_fields(ids[SECRET], hidden=1)
    db.set_tags(ids[FLAT], ["Outdoor"])
    (data / "thumbs" / f"{ids[FLAT]}.jpg").write_bytes(b"jpegdata")
    db.commit()

    server = dlna.DlnaServer(name="Test Shelf", udn="11111111-2222-3333-4444-555555555555", port=0)
    server.start(announce=False)
    yield server, db, ids
    server.stop()
    db.close()


def post(server, service: str, action: str, arguments: dict) -> tuple[int, bytes]:
    inner = "".join(f"<{key}>{value}</{key}>" for key, value in arguments.items())
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">{inner}</u:{action}>'
            "</s:Body></s:Envelope>").encode()
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request("POST", f"/dlna/control/{service}", body, {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"'})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def browse(server, object_id: str, flag: str = "BrowseDirectChildren", start: int = 0, count: int = 0):
    status, body = post(server, "ContentDirectory", "Browse", {
        "ObjectID": object_id, "BrowseFlag": flag, "Filter": "*",
        "StartingIndex": start, "RequestedCount": count, "SortCriteria": ""})
    assert status == 200, body
    response = ElementTree.fromstring(body)
    values = {element.tag.split("}")[-1]: element.text for element in response.iter()}
    didl = ElementTree.fromstring(values["Result"])  # the listing arrives escaped inside the SOAP
    return didl, int(values["NumberReturned"]), int(values["TotalMatches"])


def get(server, route: str, headers: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request("GET", route, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), body


def titles(didl) -> list[str]:
    return [element.find("dc:title", NS).text for element in didl]


# --------------------------------------------------------------------------- the device

def test_the_device_describes_itself_as_a_media_server(served):
    server, *_ = served
    status, _headers, body = get(server, "/dlna/description.xml")
    assert status == 200
    device = ElementTree.fromstring(body).find("dev:device", NS)
    assert device.find("dev:deviceType", NS).text == "urn:schemas-upnp-org:device:MediaServer:1"
    assert device.find("dev:friendlyName", NS).text == "Test Shelf"
    assert device.find("dev:UDN", NS).text == "uuid:11111111-2222-3333-4444-555555555555"
    services = [s.find("dev:serviceType", NS).text for s in device.find("dev:serviceList", NS)]
    assert services == [dlna.CDS, dlna.CMS]


@pytest.mark.parametrize("route, actions", [
    ("/dlna/ContentDirectory.xml", {"Browse", "GetSearchCapabilities", "GetSortCapabilities", "GetSystemUpdateID"}),
    ("/dlna/ConnectionManager.xml", {"GetProtocolInfo", "GetCurrentConnectionIDs", "GetCurrentConnectionInfo"}),
])
def test_each_service_lists_its_actions(served, route, actions):
    server, *_ = served
    status, _headers, body = get(server, route)
    assert status == 200
    scpd = ElementTree.fromstring(body)
    found = {a.find("scpd:name", NS).text for a in scpd.find("scpd:actionList", NS)}
    assert found == actions
    # Every argument must point at a declared state variable, or strict clients reject the service.
    declared = {v.find("scpd:name", NS).text for v in scpd.find("scpd:serviceStateTable", NS)}
    for related in scpd.iter("{urn:schemas-upnp-org:service-1-0}relatedStateVariable"):
        assert related.text in declared, related.text


# --------------------------------------------------------------------------- browsing

def test_the_top_level_is_the_sidebar(served):
    server, *_ = served
    didl, returned, total = browse(server, "0")
    assert titles(didl) == ["All videos", "Performers", "Studios", "Categories", "VR", "Favorites",
                            "Recently added", "Unwatched"]
    assert returned == total == 8
    counts = {c.find("dc:title", NS).text: c.get("childCount") for c in didl}
    assert counts["All videos"] == "2", "the hidden video must not be counted"
    assert counts["VR"] == "1"


def test_performers_open_to_their_scenes(served):
    server, _db, ids = served
    didl, *_ = browse(server, "performers")
    assert titles(didl) == ["Anna Bell"]
    performer = didl[0].get("id")
    didl, *_ = browse(server, performer)
    assert len(didl) == 2
    flat = next(item for item in didl if item.find("dc:title", NS).text == "Pool Day")
    assert flat.find("upnp:class", NS).text == "object.item.videoItem"
    assert [a.text for a in flat.findall("upnp:actor", NS)] == ["Anna Bell"]
    assert [g.text for g in flat.findall("upnp:genre", NS)] == ["Outdoor"]

    res = flat.find("d:res", NS)
    assert res.text == f"http://127.0.0.1:{server.port}/media/{ids[FLAT]}.mp4", \
        "links must use the address the client reached us on"
    assert res.get("protocolInfo").startswith("http-get:*:video/mp4:DLNA.ORG_OP=01")
    assert res.get("duration") == "1:02:05.500"
    assert res.get("resolution") == "1920x1080"
    art = flat.find("upnp:albumArtURI", NS)
    assert art is not None and art.text.endswith(f"/thumb/{ids[FLAT]}.jpg")


def test_vr_scenes_carry_the_tags_deovr_and_skybox_read(served):
    server, *_ = served
    didl, *_ = browse(server, "vr")
    assert titles(didl) == ["Sunset_180_180x180_LR"]


@pytest.mark.parametrize("fields, suffix", [
    ({"is_vr": 0}, ""),
    ({"is_vr": 1, "vr_proj": 0, "vr_layout": 1}, "_180_180x180_LR"),
    ({"is_vr": 1, "vr_proj": 0, "vr_layout": 2}, "_180_180x180_TB"),
    ({"is_vr": 1, "vr_proj": 1, "vr_layout": 0}, "_360"),
    ({"is_vr": 1, "vr_proj": 1, "vr_layout": 2}, "_360_TB"),
    ({"is_vr": 1, "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 190}, "_FISHEYE190_LR"),
    ({"is_vr": 1, "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 200}, "_MKX200_LR"),
    ({"is_vr": 1, "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 220}, "_VRCA220_LR"),
])
def test_vr_tags_for_every_layout(fields, suffix):
    assert dlna.vr_suffix(fields) == suffix


def test_studios_and_categories(served):
    server, *_ = served
    studios, *_ = browse(server, "studios")
    assert set(titles(studios)) == {"Site", "VRStudio"}
    site = next(c.get("id") for c in studios if c.find("dc:title", NS).text == "Site")
    assert titles(browse(server, site)[0]) == ["Pool Day"]
    tags, *_ = browse(server, "tags")
    assert titles(tags) == ["Outdoor"]


def test_metadata_for_a_single_folder_or_item(served):
    server, _db, ids = served
    didl, returned, _ = browse(server, "vr", flag="BrowseMetadata")
    assert returned == 1 and didl[0].tag.endswith("container") and didl[0].get("parentID") == "0"
    didl, *_ = browse(server, f"all/{ids[FLAT]}", flag="BrowseMetadata")
    assert didl[0].tag.endswith("item") and didl[0].find("dc:title", NS).text == "Pool Day"


def test_long_folders_are_paged(served):
    server, *_ = served
    first, returned, total = browse(server, "0", start=0, count=3)
    rest, returned_rest, _ = browse(server, "0", start=3, count=0)
    assert (returned, total) == (3, 8)
    assert titles(first) + titles(rest) == titles(browse(server, "0")[0])


def test_asking_for_something_that_is_not_there(served):
    server, *_ = served
    for missing in ("nonsense", "p:99999", "all/99999"):
        status, body = post(server, "ContentDirectory", "Browse", {
            "ObjectID": missing, "BrowseFlag": "BrowseDirectChildren" if ":" not in missing else "BrowseMetadata",
            "Filter": "*", "StartingIndex": 0, "RequestedCount": 0, "SortCriteria": ""})
        assert status == 500 and b"<errorCode>701</errorCode>" in body, missing


def test_connection_manager_answers(served):
    server, *_ = served
    status, body = post(server, "ConnectionManager", "GetProtocolInfo", {})
    assert status == 200 and b"http-get:*:video/mp4:*" in body


# --------------------------------------------------------------------------- playing

def test_video_streams_with_what_tvs_insist_on(served):
    server, _db, ids = served
    status, headers, body = get(server, f"/media/{ids[FLAT]}.mp4", {"Range": "bytes=10-19"})
    assert status == 206 and len(body) == 10
    assert headers["Content-Range"] == "bytes 10-19/10240"
    assert headers["transferMode.dlna.org"] == "Streaming"
    assert headers["contentFeatures.dlna.org"].startswith("DLNA.ORG_OP=01")
    assert headers["Content-Type"] == "video/mp4"


def test_hidden_videos_cannot_be_played_even_by_guessing(served):
    """No passcode protects DLNA, so hidden has to mean hidden all the way down."""
    server, _db, ids = served
    status, _headers, _body = get(server, f"/media/{ids[SECRET]}.mp4")
    assert status == 404
    for view in ("all", "recent", "unwatched"):
        assert "Hidden Scene" not in titles(browse(server, view)[0])


def test_only_the_home_network_is_served():
    allowed = dlna._HttpServer.allowed
    for address in ("192.168.1.20", "10.0.0.5", "172.20.1.1", "127.0.0.1", "169.254.3.3"):
        assert allowed(address), address
    for address in ("8.8.8.8", "172.32.0.1", "100.64.0.1"):
        assert not allowed(address), address


def test_thumbnails(served):
    server, _db, ids = served
    status, headers, body = get(server, f"/thumb/{ids[FLAT]}.jpg")
    assert status == 200 and body == b"jpegdata" and headers["Content-Type"] == "image/jpeg"
    assert get(server, f"/thumb/{ids[VR]}.jpg")[0] == 404


# --------------------------------------------------------------------------- being found

def test_searches_are_answered_only_when_they_are_for_us():
    ssdp = dlna.Ssdp("abc", 8200)
    search = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
              "MX: 2\r\nST: {st}\r\n\r\n")
    assert ssdp.answers(search.format(st="ssdp:all").encode()) == ssdp.targets
    assert ssdp.answers(search.format(st=dlna.DEVICE_TYPE).encode()) == [dlna.DEVICE_TYPE]
    assert ssdp.answers(search.format(st="urn:schemas-upnp-org:device:MediaRenderer:1").encode()) == []
    assert ssdp.answers(b"NOTIFY * HTTP/1.1\r\n\r\n") == []

    reply = ssdp.response(dlna.DEVICE_TYPE, "192.168.1.169").decode()
    assert "LOCATION: http://192.168.1.169:8200/dlna/description.xml" in reply
    assert f"USN: uuid:abc::{dlna.DEVICE_TYPE}" in reply
    assert "USN: uuid:abc\r\n" in ssdp.response("uuid:abc", "10.0.0.1").decode()


def test_a_search_on_the_network_gets_an_answer(served):
    """A real multicast search, on a private port so Windows' own SSDP service is not involved."""
    server, *_ = served
    port = 41900
    ssdp = dlna.Ssdp(server.udn, server.port, ssdp_port=port)
    try:
        ssdp.start()
    except OSError as error:
        pytest.skip(f"multicast is not available here: {error}")
    try:
        asker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        asker.settimeout(1.0)
        asker.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        # Sent out of the LAN interface, as a TV's search would arrive. Windows' default multicast
        # route can point at an adapter nothing is listening on.
        lan = (dlna.local_ips() or ["127.0.0.1"])[0]
        asker.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan))
        message = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:{port}\r\nMAN: \"ssdp:discover\"\r\n"
                   f"MX: 1\r\nST: {dlna.DEVICE_TYPE}\r\n\r\n").encode()
        replies = []
        deadline = time.time() + 6
        while time.time() < deadline and not replies:
            asker.sendto(message, (dlna.SSDP_GROUP, port))
            try:
                while True:
                    data, _ = asker.recvfrom(4096)
                    if server.udn in data.decode():
                        replies.append(data.decode())
            except socket.timeout:
                pass
        asker.close()
        assert replies, "the server should answer a search for media servers"
        assert f"/dlna/description.xml" in replies[0]
    finally:
        ssdp.stop()


def test_an_oversized_packet_does_not_deafen_the_listener(served):
    """One chatty device once left BlueShelf visible to Skybox but invisible to DeoVR."""
    server, *_ = served
    port = 41960
    lan = (dlna.local_ips() or ["127.0.0.1"])[0]
    ssdp = dlna.Ssdp(server.udn, server.port, ssdp_port=port)
    try:
        ssdp.start()
    except OSError as error:
        pytest.skip(f"multicast is not available here: {error}")

    def answered() -> bool:
        asker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        asker.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan))
        asker.bind((lan, 0))
        asker.settimeout(1.5)
        search = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:{port}\r\n"
                  f"MAN: \"ssdp:discover\"\r\nMX: 1\r\nST: {dlna.DEVICE_TYPE}\r\n\r\n")
        asker.sendto(search.encode(), (dlna.SSDP_GROUP, port))
        try:
            return server.udn in asker.recvfrom(4096)[0].decode()
        except socket.timeout:
            return False
        finally:
            asker.close()

    try:
        assert answered()
        noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        noise.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan))
        noise.sendto(b"NOTIFY * HTTP/1.1\r\n" + b"X" * 6000, (dlna.SSDP_GROUP, port))
        noise.close()
        time.sleep(0.5)
        assert answered(), "a search after the big packet must still be answered"
        # The counters are raised by the listener's own thread, just after it puts the reply
        # on the wire - so a reply can reach us before the number moves. Wait for it rather
        # than reading it the instant the answer lands.
        until = time.monotonic() + 5.0
        while time.monotonic() < until and not (ssdp.heard >= 2 and ssdp.answered >= 2):
            time.sleep(0.05)
        assert ssdp.heard >= 2 and ssdp.answered >= 2
    finally:
        ssdp.stop()


def test_a_passthrough_scene_says_so(served):
    """Players only switch passthrough on when the name says ALPHA."""
    vr = {"is_vr": True, "vr_proj": 2, "vr_layout": 1, "vr_lens_fov": 190, "tag_names": "Passthrough, POV"}
    assert dlna.vr_suffix(vr) == "_FISHEYE190_LR_ALPHA"
    assert dlna.vr_suffix({**vr, "tag_names": "POV"}) == "_FISHEYE190_LR"
