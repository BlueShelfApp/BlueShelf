from blueshelf.suggestions import merge_suggestions, stray_suggestions


def test_keeps_the_more_common_spelling():
    suggestions = merge_suggestions([(1, "Lily LaBeau", 5), (2, "Lily Labele", 3)])
    assert len(suggestions) == 1
    assert suggestions[0].keep_name == "Lily LaBeau"
    assert suggestions[0].merge_name == "Lily Labele"
    assert suggestions[0].reason == "surname spelled differently"


def test_misspelled_surname():
    suggestions = merge_suggestions([(1, "Kira Perez", 4), (2, "Kira Prez", 1)])
    assert (suggestions[0].keep_name, suggestions[0].merge_name) == ("Kira Perez", "Kira Prez")


def test_surname_shortened_to_an_initial():
    suggestions = merge_suggestions([(1, "Siri Dahl", 4), (2, "Siri D", 1)])
    assert suggestions[0].reason == "surname shortened to an initial"
    assert suggestions[0].keep_name == "Siri Dahl"


def test_missing_surname():
    suggestions = merge_suggestions([(1, "Angie Faith", 3), (2, "Angie", 1)])
    assert suggestions[0].reason == "one is missing the surname"
    assert suggestions[0].keep_name == "Angie Faith"


def test_different_people_are_left_alone():
    assert merge_suggestions([(1, "Emma Rose", 2), (2, "Emma Starr", 2)]) == []
    assert merge_suggestions([(1, "Lily Lou", 1), (2, "Lily Love", 1)]) == []
    assert merge_suggestions([(1, "Kira Fox", 3), (2, "Kira Perez", 3)]) == []


def test_swapping_reverses_the_direction():
    suggestion = merge_suggestions([(1, "Lily LaBeau", 5), (2, "Lily Labele", 3)])[0].swapped()
    assert suggestion.keep_name == "Lily Labele"
    assert suggestion.merge_name == "Lily LaBeau"


def test_ignored_pairs_are_not_suggested_again():
    entries = [(1, "Kira Perez", 4), (2, "Kira Prez", 1)]
    pair = tuple(merge_suggestions(entries)[0].pair_key())
    assert merge_suggestions(entries, {pair}) == []


def test_vr_brands_are_not_merged_with_their_flat_sibling():
    assert merge_suggestions([("wankzvr", "WankzVR", 9), ("wankz", "Wankz", 2)]) == []
    assert merge_suggestions([("badoink", "BaDoink", 3), ("badoinkvr", "BaDoinkVR", 7)]) == []


def test_spacing_variants_of_one_studio_still_merge():
    suggestions = merge_suggestions([("slroriginals", "SLROriginals", 5), ("slr originals", "SLR Originals", 3)])
    assert suggestions and suggestions[0].keep_name == "SLROriginals"


def test_studio_variants_use_the_same_rules():
    suggestions = merge_suggestions([("naughtyamerica", "NaughtyAmerica", 9), ("naugtyamerica", "Naugty America", 1)])
    assert suggestions[0].keep_name == "NaughtyAmerica"


def test_names_that_are_really_titles():
    strays = stray_suggestions([(1, "Library Visit", 1, ["Library Visit"]),
                                (2, "Anna Bell", 1, ["Pool Day"])])
    assert [s.name for s in strays] == ["Library Visit"]
    assert strays[0].example_title == "Library Visit"


def test_popular_performers_are_never_called_titles():
    entries = [(1, "Valentina Nappi", 6, ["Nappi Ending Valentina Nappi"] * 6)]
    assert stray_suggestions(entries) == []


def test_ignored_stray_names_stay_hidden():
    entries = [(1, "Library Visit", 1, ["Library Visit"])]
    assert stray_suggestions(entries, {"library visit"}) == []
