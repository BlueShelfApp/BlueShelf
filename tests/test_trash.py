import os

import pytest

from blueshelf import trash


def make(path, size=10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


def test_file_in_library_root_is_moved_alone_and_restored(tmp_path):
    root = tmp_path / "lib"
    video = root / "clip.mp4"
    make(video)
    make(root / "other.mp4")
    item, origin = trash.move_to_trash(str(video), str(root))
    assert origin == str(video)
    assert not video.exists() and os.path.exists(item)
    assert os.path.dirname(item) == str(root / trash.TRASH_DIR_NAME)
    trash.restore(item, origin)
    assert video.exists()
    assert (root / "other.mp4").exists()


def test_release_folder_with_only_extras_is_moved_whole(tmp_path):
    root = tmp_path / "lib"
    folder = root / "Site - Scene - Anna Bell (Oculus 8K)"
    video = folder / "scene.mp4"
    make(video)
    make(folder / "cover.jpg")
    make(folder / "Sample" / "sample.mp4")
    item, origin = trash.move_to_trash(str(video), str(root))
    assert origin == str(folder)
    assert not folder.exists()
    assert os.path.exists(os.path.join(item, "scene.mp4"))


def test_folder_with_other_videos_keeps_them(tmp_path):
    root = tmp_path / "lib"
    folder = root / "Collection 2"
    video = folder / "01.mp4"
    make(video)
    make(folder / "02.mp4")
    _, origin = trash.move_to_trash(str(video), str(root))
    assert origin == str(video)
    assert (folder / "02.mp4").exists()


def test_person_folder_is_never_removed(tmp_path):
    root = tmp_path / "lib"
    video = root / "organized" / "Anna Bell" / "scene.mp4"
    make(video)
    assert trash.removal_target(str(video), str(root)) == str(video)


def test_large_leftovers_keep_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(trash, "LEFTOVER_LIMIT", 5)
    root = tmp_path / "lib"
    video = root / "Release" / "scene.mp4"
    make(video)
    make(root / "Release" / "scene.mp4.part", 10)
    assert trash.removal_target(str(video), str(root)) == str(video)


def test_permanent_delete_removes_release_folder(tmp_path):
    root = tmp_path / "lib"
    video = root / "Release" / "scene.mp4"
    make(video)
    assert trash.delete_permanently(str(video), str(root)) == str(root / "Release")
    assert not (root / "Release").exists()


def test_restore_refuses_to_overwrite(tmp_path):
    root = tmp_path / "lib"
    video = root / "clip.mp4"
    make(video)
    item, origin = trash.move_to_trash(str(video), str(root))
    make(video)
    with pytest.raises(FileExistsError):
        trash.restore(item, origin)


def test_trash_contents_and_size(tmp_path):
    root = tmp_path / "lib"
    make(root / "Release" / "scene.mp4", 7)
    make(root / "Release" / "cover.jpg", 3)
    item, _ = trash.move_to_trash(str(root / "Release" / "scene.mp4"), str(root))
    assert trash.trash_contents(str(root)) == [item]
    assert trash.path_size(item) == 10
    trash.remove_path(item)
    assert trash.trash_contents(str(root)) == []
