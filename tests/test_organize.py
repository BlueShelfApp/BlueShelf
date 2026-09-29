import os

from blueshelf.organize import (NO_PERFORMER, NO_STUDIO, clean_tree, plan_tree, primary_folder, safe_name, unc_path,
                            write_manifest)


def video(vid, title, performers, path=None, studio="", is_vr=0):
    return {"id": vid, "title": title, "performers": performers, "studio": studio, "is_vr": is_vr,
            "path": path or f"Y:\\{title}.mp4"}


def test_a_scene_with_two_performers_appears_in_both_folders():
    plan = plan_tree([video(1, "Pool Day", ["Anna Bell", "Cara Mell"])])
    assert sorted(link for link, _ in plan.links) == [os.path.join("Anna Bell", "Pool Day.lnk"),
                                                      os.path.join("Cara Mell", "Pool Day.lnk")]
    assert all(target == "Y:\\Pool Day.mp4" for _, target in plan.links)
    assert plan.folders == ["Anna Bell", "Cara Mell"]


def test_videos_without_a_performer_are_counted_and_kept_together():
    plan = plan_tree([video(1, "Rec 0035", []), video(2, "Rec 0037", [])])
    assert plan.unsorted == 2
    assert plan.folders == [NO_PERFORMER]


def test_two_videos_with_the_same_title_in_one_folder():
    plan = plan_tree([video(1, "Untitled", ["Anna Bell"], "Y:\\a.mp4"),
                      video(2, "Untitled", ["Anna Bell"], "Y:\\b.mp4")])
    assert sorted(link for link, _ in plan.links) == [os.path.join("Anna Bell", "Untitled (2).lnk"),
                                                      os.path.join("Anna Bell", "Untitled.lnk")]


def test_studio_and_vr_structures_nest_the_folders():
    scene = video(1, "Scene", ["Anna Bell"], studio="BaDoinkVR", is_vr=1)
    assert plan_tree([scene], "studio_performer").folders == [os.path.join("BaDoinkVR", "Anna Bell")]
    assert plan_tree([scene], "vr_performer").folders == [os.path.join("VR", "Anna Bell")]
    no_studio = video(2, "Scene", ["Anna Bell"])
    assert plan_tree([no_studio], "studio_performer").folders == [os.path.join(NO_STUDIO, "Anna Bell")]


def test_names_windows_would_reject_are_cleaned():
    assert safe_name('Anna: Bell?/"') == "Anna Bell"
    assert safe_name("  trailing dots... ") == "trailing dots"
    assert safe_name("CON") == "Unnamed"
    assert safe_name("") == "Unnamed"
    assert len(safe_name("x" * 200)) <= 80
    plan = plan_tree([video(1, "What? A <Scene>", ["Anna Bell"])])
    assert plan.links[0][0] == os.path.join("Anna Bell", "What A Scene.lnk")


def test_falls_back_to_the_file_name_when_there_is_no_title():
    plan = plan_tree([{"id": 1, "title": "", "performers": ["Anna Bell"], "path": r"Y:\folder\clip 12.mp4"}])
    assert plan.links[0][0] == os.path.join("Anna Bell", "clip 12.lnk")


def test_primary_folder_picks_the_most_filmed_performer():
    scene = video(1, "Scene", ["Anna Bell", "Cara Mell"])
    assert primary_folder(scene, {"Anna Bell": 12, "Cara Mell": 3}) == "Anna Bell"
    assert primary_folder(scene, {"Anna Bell": 2, "Cara Mell": 30}) == "Cara Mell"
    assert primary_folder(video(2, "Solo", ["Anna Bell"]), {}) == "Anna Bell"


def test_local_and_unc_paths_are_left_alone(tmp_path):
    local = str(tmp_path / "clip.mp4")
    assert unc_path(local) == local
    assert unc_path(r"\\server\share\clip.mp4") == r"\\server\share\clip.mp4"
    assert unc_path("") == ""


def test_clean_tree_only_removes_what_it_made(tmp_path):
    root = tmp_path / "tree"
    folder = root / "Anna Bell"
    folder.mkdir(parents=True)
    (folder / "Scene.lnk").write_text("x")
    keeper = folder / "my notes.txt"
    keeper.write_text("mine")
    plan = plan_tree([video(1, "Scene", ["Anna Bell"])])
    write_manifest(str(root), plan, "performer")
    assert clean_tree(str(root)) == 1
    assert not (folder / "Scene.lnk").exists()
    assert keeper.exists()  # the folder survives because something of the user's is still in it
