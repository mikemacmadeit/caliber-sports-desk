from spe.identity.match import match_name, normalize_name


def test_normalize_strips_suffix():
    assert normalize_name("Kenneth Walker III") == "kenneth walker"


def test_auto_match_unique():
    catalog = [("a", "Rashee Rice"), ("b", "Travis Kelce")]
    d = match_name("Rashee Rice", catalog)
    assert d.review_status == "auto"
    assert d.canonical_id == "a"


def test_never_silently_join_same_name():
    catalog = [("a", "John Smith"), ("b", "John Smith")]
    d = match_name("John Smith", catalog)
    assert d.canonical_id is None
    assert d.review_status == "needs_review"
    assert d.reason == "DUPLICATE_DISPLAY_NAME"


def test_exact_name_wins_over_a_near_spelling():
    catalog = [("a", "JaMarr Chase"), ("b", "Ja Marr Chase")]
    d = match_name("JaMarr Chase", catalog)
    assert d.review_status == "auto"
    assert d.canonical_id == "a"


def test_near_tie_needs_review():
    catalog = [("a", "JaMarr Chase"), ("b", "Ja Marr Chase")]
    d = match_name("Jamarr Chse", catalog)
    assert d.review_status == "needs_review"
    assert d.canonical_id is None


def test_reordered_name_is_not_the_same_player():
    catalog = [("sf", "Jordan James")]
    d = match_name("James Jordan", catalog)
    assert d.canonical_id is None
    assert d.review_status == "needs_review"
    assert d.reason == "REORDERED_NAME"
