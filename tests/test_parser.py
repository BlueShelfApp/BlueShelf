from blueshelf.parser import (LAYOUT_SBS, LAYOUT_TB, PROJ_EQUI180, PROJ_EQUI360, PROJ_FISHEYE, bad_tag_rule_lines,
                          clean_title, detect_vr, find_date, is_person_folder, parse_name, parse_path, performer_key,
                          similar_names, split_names, text_ngrams)


def test_dotted_scene_release_with_two_names():
    p = parse_name("StudioOne.24.03.05.Jane.Doe.And.Mary.Roe.Pool.Day.XXX.720p")
    assert p.studio == "StudioOne"
    assert p.date == "2024-03-05"
    assert p.performers == ["Jane Doe", "Mary Roe"]
    assert p.title == "Pool Day"


def test_lowercase_dotted_release():
    p = parse_name("somesite.21.03.18.kara.lane.nurse.visit")
    assert p.performers == ["Kara Lane"]
    assert p.title == "Nurse Visit"
    assert p.studio == "Somesite"


def test_dotted_release_does_not_guess_second_name_from_title_words():
    p = parse_name("Site.23.09.23.anna.bell.sauna.temptation.4k")
    assert p.performers == ["Anna Bell"]
    assert p.title == "Sauna Temptation"


def test_dotted_release_with_trailing_spaced_resolution():
    p = parse_name("site.21.04.06.kara.lane.visit.and.a.nutty.guy 1080p")
    assert p.performers == ["Kara Lane"]


def test_day_month_year_release():
    p = parse_name("LoveSite.07.12.2021.Anna.Bell.1080p")
    assert p.date == "2021-12-07"
    assert p.performers == ["Anna Bell"]


def test_dash_style_title_then_performers_with_device():
    p = parse_name("SomeVR - A Quiet Evening - Jane Doe, Mary Roe (Oculus 8K)")
    assert p.studio == "SomeVR"
    assert p.performers == ["Jane Doe", "Mary Roe"]
    assert p.title == "A Quiet Evening"


def test_dash_style_performer_then_title():
    p = parse_name("TryStudio - Kara Lane - Obsessed With The Beach!")
    assert p.performers == ["Kara Lane"]
    assert p.title == "Obsessed With The Beach!"


def test_dash_style_prefers_name_list_over_two_word_title():
    p = parse_name("SiteName - Anna Bell, Cara Mell - Bird Watching")
    assert p.performers == ["Anna Bell", "Cara Mell"]
    assert p.title == "Bird Watching"


def test_dash_style_prefers_first_of_two_name_like_segments():
    p = parse_name("FamilySite - Anna Bell - Natural Healing - 1080p")
    assert p.performers == ["Anna Bell"]
    assert p.title == "Natural Healing"


def test_dash_style_with_date_and_bitrate():
    p = parse_name("SiteVR - 2025-12-15 - Girlfriend Material - Anna Bell - 8k - 26 Mbps")
    assert p.date == "2025-12-15"
    assert p.performers == ["Anna Bell"]
    assert p.title == "Girlfriend Material"


def test_single_name_in_last_segment():
    p = parse_name("SiteVR - Lesson Break - Remmy (Oculus 5K)")
    assert p.performers == ["Remmy"]
    assert p.title == "Lesson Break"


def test_bracket_studio():
    p = parse_name("[Site.com] Jane Doe & Mary Roe - Sweet Afternoon (November 30, 2024)")
    assert p.studio == "Site"
    assert p.date == "2024-11-30"
    assert p.performers == ["Jane Doe", "Mary Roe"]
    assert p.title == "Sweet Afternoon"


def test_bracket_studio_with_two_word_title():
    p = parse_name("[Customs] Anna Bell & Cara Mell - Lesbian Oil Wrestle")
    assert p.performers == ["Anna Bell", "Cara Mell"]


def test_bracket_studio_with_names_in_parentheses():
    p = parse_name("[SomeHunters] Sleepwalk (Kara Lane, Anna Bell) [11-5-18]")
    assert p.performers == ["Kara Lane", "Anna Bell"]
    assert p.title == "Sleepwalk"
    assert p.date == "2018-11-05"


def test_slr_underscore_style():
    p = parse_name("SLR_LustSite_Jane Doe_Your Neighbor Visits_3840p_LR_180")
    assert p.studio == "LustSite"
    assert p.performers == ["Jane Doe"]
    assert p.title == "Your Neighbor Visits"


def test_underscore_style_last_segment_is_title():
    p = parse_name("SiteVR_Lily Anne_Extremely Satisfied_3840p_LR_180")
    assert p.studio == "SiteVR"
    assert p.performers == ["Lily Anne"]
    assert p.title == "Extremely Satisfied"


def test_underscore_style_with_split_first_and_last_name():
    p = parse_name("SiteVR_Kara_Lane_Skimming the Pool, Plowing Ahead - 8K")
    assert p.studio == "SiteVR"
    assert p.performers == ["Kara Lane"]


def test_hyphen_slug_with_vr_studio():
    p = parse_name("sitevr-backdoor-chores-180_180x180_3dh_LR")
    assert p.studio == "Sitevr"
    assert p.performers == []
    assert p.title == "Backdoor Chores"


def test_studio_hyphen_then_name_list():
    p = parse_name("MassageSite-Anna Bell, Cara Mell-1080p")
    assert p.studio == "MassageSite"
    assert p.performers == ["Anna Bell", "Cara Mell"]


def test_code_style():
    p = parse_name("cjvr-026-1")
    assert p.studio == "CJVR"
    assert p.title == "CJVR-026 (Part 1)"


def test_starring():
    p = parse_name("Break Up Therapy starring Kara Lane & Anna Bell")
    assert p.performers == ["Kara Lane", "Anna Bell"]


def test_with_only_splits_real_name_lists():
    assert split_names("Sexual Healing with Anna Bell") is None
    assert split_names("Anna Bell & Cara Mell with Rick Stone") == ["Anna Bell", "Cara Mell", "Rick Stone"]


def test_movie_title_junk_is_removed():
    assert clean_title("Some Movie 2006 720p BluRay x264-GRP") == "Some Movie"
    assert clean_title("Rec 0035") == "Rec 0035"


def test_folder_supplies_metadata_for_short_filenames():
    p = parse_path("SiteVR - Grid Check - Jane Doe (Oculus 8K)/SiteVR_Grid_Check_8K_180_180x180_3dh.mp4")
    assert p.performers == ["Jane Doe"]
    assert p.title == "Grid Check"
    assert p.vr.is_vr


def test_numbered_files_take_folder_title():
    p = parse_path("Big Collection 2/01.mp4", single_video_dir=False)
    assert p.title == "Big Collection 2 (01)"


def test_person_folder_adds_performer():
    p = parse_path("organized/mary roe/Another Studio - Library Visit.mp4", single_video_dir=False)
    assert "Mary Roe" in p.performers


def test_misspelled_person_folder_does_not_duplicate_performer():
    p = parse_path("organized/Kira Prez/Site.22.05.17.Kira.Perez.XXX.mp4", single_video_dir=False)
    assert p.performers == ["Kira Perez"]


def test_person_folder_completes_first_name_only():
    p = parse_path("organized/Anna Bell/NetSite.23.02.28.Anna.XXX.1080p.mp4", single_video_dir=False)
    assert p.performers == ["Anna Bell"]


def test_person_folder_detection():
    assert is_person_folder("mary roe")
    assert not is_person_folder("New folder")
    assert not is_person_folder("Busty Collection 2")
    assert not is_person_folder("SchmatesVids")


def test_similar_names():
    assert similar_names("Kira Perez", "Kira Prez")
    assert similar_names("Anna Belle", "Ana Bell")
    assert similar_names("Siri Dahl", "Siri D")
    assert similar_names("Lily LaBeau", "Lily Labele")
    assert not similar_names("Lily Lou", "Lily Love")
    assert not similar_names("Kira Fox", "Kira Perez")
    assert not similar_names("Emma Rose", "Emma Starr")


def test_performer_key_ignores_case_and_doubled_letters():
    assert performer_key("Abigaiil Morris") == performer_key("abigail morris")
    assert performer_key("Chloé Dupont") == performer_key("chloe dupont")


def test_text_ngrams_finds_names_in_camel_and_dotted_text():
    assert "kara lane" in text_ngrams("Site - Stepmom Kara Lane's Day")
    assert "kara lane" in text_ngrams("site.kara.lane.xxx")
    assert "kara lane" in text_ngrams("FPVR-KaraLane-180")


def test_dates():
    assert find_date("Kara (Sep 12, 2014)")[0] == "2014-09-12"
    assert find_date("scene 28.03.2023 done")[0] == "2023-03-28"
    assert find_date("clip 2025.06.03 1080p")[0] == "2025-06-03"


def test_vr_detection():
    v = detect_vr("SiteVR_Scene_8K_180_180x180_3dh.mp4")
    assert v.is_vr and v.layout == LAYOUT_SBS and v.proj == PROJ_EQUI180
    v = detect_vr("SLR_Site_Scene_4000p_37262_FISHEYE190.mp4")
    assert v.is_vr and v.proj == PROJ_FISHEYE and v.lens_fov == 190
    v = detect_vr("scene_360_TB.mp4")
    assert v.is_vr and v.layout == LAYOUT_TB and v.proj == PROJ_EQUI360
    assert not detect_vr("Some.Movie.2024.1080p.WEB-DL.x264-GRP.mkv").is_vr
    assert not detect_vr("Chevrolet review 2019.mp4").is_vr


def test_mainstream_video_is_not_adult():
    assert not parse_path("Some.Movie.2024.1080p.WEB-DL.x264-GRP/Some.Movie.2024.1080p.WEB-DL.x264-GRP.mkv").adult
    assert not parse_path("Show.2026.S01E03.1080p.WEB.h264-GRP.mkv").adult
    assert not parse_path("Show S01 1080p BluRay x265-GRP/Show - 01.mkv", single_video_dir=False).adult
    assert parse_path("Site - 2025-08-16 - The Plan Part 2 [WEBDL-2160p].mp4").adult
    assert parse_path("HappySite-Versary_s01_CodiVore_2160p_h264.mp4").adult
    assert parse_path("Rec 0035.mp4").adult


def test_resolution_in_parentheses_is_not_a_vr_device_marker():
    p = parse_name("[Straight] (2023.06.05) Anna Bell - Stunning Flame (2160p)")
    assert p.performers == ["Anna Bell"]
    assert p.title == "Stunning Flame"


def test_vr_studio_dash_style_prefers_last_name_like_segment():
    p = parse_name("SiteVR - Backdoor Chores - Kara Lane")
    assert p.performers == ["Kara Lane"]
    assert p.title == "Backdoor Chores"


def test_slr_originals_title_words_are_not_a_performer():
    p = parse_name("SLR_SLR_Originals_Indecent_Photographs_4000p_37262_FISHEYE190")
    assert p.studio == "SLR Originals"
    assert p.performers == []
    assert p.title == "Indecent Photographs"


def test_leading_numeric_id_is_skipped():
    assert parse_name("91550_anna-bell-good-vibes_8192_180x180_3dh").title == "Anna Bell Good Vibes"


def test_person_folder_breaks_tie_between_name_like_segments():
    p = parse_path("organized/Mary Roe/Another Studio - Library Visit - Mary Roe.mp4", single_video_dir=False)
    assert p.performers == ["Mary Roe"]
    assert p.title == "Library Visit"


def test_exact_folder_name_beats_a_similar_title():
    p = parse_path("organized/anna bell/Originals - Anna Unbelievable - Anna Bell (Oculus 6K)/x_180.mp4")
    assert p.performers == ["Anna Bell"]
    assert p.title == "Anna Unbelievable"


def test_underscore_suffix_after_device_parens():
    p = parse_name("SiteVR - Grid Check - Jane Doe (Oculus 4K)_180_LR")
    assert p.performers == ["Jane Doe"]
    assert p.title == "Grid Check"


def test_auto_tags():
    assert "Massage" in parse_path("NuruSite.23.11.27.Kara.Lane.Massage.Day.XXX.1080p.mp4").tags


def test_bad_tag_rules_are_reported():
    assert bad_tag_rule_lines("Good = abc\nno equals sign\nBroken = (unclosed") == [
        "line 2: no equals sign", "line 3: missing ), unterminated subpattern at position 0"]


def test_passthrough_scenes_get_their_own_category():
    assert "Passthrough" in parse_path("VRStudio.Sunset.180_LR_ALPHA.mp4").tags
    assert "Passthrough" in parse_path("Studio.Scene.Passthrough.mp4").tags
    assert "Passthrough" in parse_path("Site.Green.Screen.Scene.mp4").tags
    assert "Passthrough" not in parse_path("Site.Alphabet.Soup.mp4").tags, "a word containing alpha is not one"
