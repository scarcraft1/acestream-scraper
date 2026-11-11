"""Channel rules tree: pure classification of a stream name (synthetic names only)."""

from app.services import channel_tree as T


def node(id, conds=(), steps=(), children=(), leaf=None, match="any", group=""):
    return {
        "id": id,
        "conds": list(conds),
        "steps": list(steps),
        "children": list(children),
        "leaf": leaf,
        "match": match,
        "group": group,
    }


def words(value, field="name"):
    return {"field": field, "op": "words", "value": value}


PREP = {"type": "prep", "ops": ["upper", "trim", "collapse", "quality"], "cond": None}


def tree(*children, steps=(PREP,)):
    return node("root", steps=steps, children=children)


def test_first_matching_sibling_wins_and_later_ones_are_not_tested():
    root = tree(
        node("a", [words("DEMO SPORT")], leaf={"channel": "Demo Sport"}),
        node("b", [words("DEMO")], leaf={"channel": "Demo"}),
    )
    st = T.run(root, {"name": "demo sport 1080p"})
    assert st.leaf["channel"] == "Demo Sport"
    assert st.path == ["root", "a"]
    assert "b" not in st.tested


def test_a_node_whose_children_all_fail_is_where_the_stream_stops():
    root = tree(node("sec", [words("DEMO")], children=[node("x", [words("OTHER")], leaf={"channel": "X"})]))
    st = T.run(root, {"name": "Demo Plus"})
    assert st.leaf is None
    assert st.rest_at == "sec"


def test_hash_captures_a_number_without_leading_zero_and_fills_the_leaf():
    root = tree(node("t", [words("SAMPLE TV #")], leaf={"channel": "Sample TV #", "guide": "sample.#.tv"}))
    st = T.run(root, {"name": "Sample TV 04 HD"})
    assert st.num == "4"
    assert st.leaf == {"node": "t", "channel": "Sample TV 4", "guide": "sample.4.tv"}
    assert T.run(root, {"name": "Sample TV 007"}).leaf["channel"] == "Sample TV 7"


def test_template_without_a_number_drops_the_hash():
    root = tree(node("t", [words("SAMPLE TV #")], leaf={"channel": "Sample TV #"}))
    assert T.run(root, {"name": "Sample TV"}).leaf is None  # «#» in the condition is required
    root = tree(node("t", [words("SAMPLE TV")], leaf={"channel": "Sample TV #"}))
    st = T.run(root, {"name": "Sample TV"})
    assert st.num is None
    assert st.leaf["channel"] == "Sample TV"


def test_words_and_starts_take_an_optional_loose_number():
    root = tree(node("t", [words("SAMPLE")], leaf={"channel": "Sample #"}))
    assert T.run(root, {"name": "Sample 2"}).leaf["channel"] == "Sample 2"
    root = tree(node("t", [{"field": "name", "op": "starts", "value": "SAMPLE"}], leaf={"channel": "Sample #"}))
    assert T.run(root, {"name": "Sample-3"}).leaf["channel"] == "Sample 3"
    assert T.run(root, {"name": "Other Sample 3"}).leaf is None


def test_word_boundaries_follow_the_editor_and_plus_is_part_of_a_word():
    root = tree(node("t", [words("DEMO")], leaf={"channel": "Demo"}))
    assert T.run(root, {"name": "Demo+"}).leaf is None
    assert T.run(root, {"name": "Demos"}).leaf is None
    assert T.run(root, {"name": "Canal-Demo"}).leaf["channel"] == "Demo"


def test_not_exact_and_all_conditions():
    root = tree(
        node(
            "t",
            [words("DEMO"), {"field": "name", "op": "not", "value": "RADIO"}],
            match="all",
            leaf={"channel": "Demo"},
        ),
        node("e", [{"field": "name", "op": "exact", "value": "DEMO RADIO"}], leaf={"channel": "Demo Radio"}),
    )
    assert T.run(root, {"name": "Demo"}).leaf["channel"] == "Demo"
    assert T.run(root, {"name": "demo  radio"}).leaf["channel"] == "Demo Radio"


def test_quality_removes_resolution_and_codes_but_keeps_a_bare_360():
    assert T.prep_op("SAMPLE 360 N 1080P [ab12cd34] (BACKUP)", "quality") == "SAMPLE 360 N"
    assert T.prep_op("SAMPLE HD* ...151", "quality") == "SAMPLE"
    assert T.prep_op("DEMO E-435 UHD", "quality") == "DEMO"


def test_replace_step_changes_the_first_match_and_rename_regex_changes_all():
    root = tree(
        node(
            "t",
            [words("A")],
            steps=[
                {"type": "repl", "pattern": "X", "repl": "Y", "cond": None},
                {"type": "ren", "mode": "regex", "pattern": "x", "repl": "z"},
            ],
            leaf={"channel": "A"},
        )
    )
    st = T.run(root, {"name": "A X X"})
    assert st.work == "A Y X"
    assert st.kodi == "A z z"


def test_replacements_accept_javascript_groups():
    root = tree(
        steps=[
            PREP,
            {"type": "repl", "pattern": r'^TVG-NAME="([^"]+)".*$', "repl": "$1", "cond": None},
            {"type": "repl", "pattern": "SAMPLE", "repl": "[$&] $$", "cond": None},
        ]
    )
    assert T.run(root, {"name": 'tvg-name="Sample One" junk'}).work == "[SAMPLE] $ ONE"


def test_steps_with_a_condition_and_several_groups_without_duplicates():
    root = tree(
        node(
            "t",
            [words("DEMO")],
            steps=[
                {"type": "grp", "group": "Sports; General", "cond": None},
                {"type": "grp", "group": "Events", "cond": {"field": "list", "op": "exact", "value": "Lista B"}},
            ],
            group="General;Local",
            leaf={"channel": "Demo"},
        )
    )
    assert T.run(root, {"name": "Demo", "list": "Lista A"}).groups == ["Sports", "General", "Local"]
    assert T.run(root, {"name": "Demo", "list": "Lista B"}).groups == ["Sports", "General", "Events", "Local"]


def test_conditions_on_tvg_and_the_captured_number():
    root = tree(
        node(
            "t",
            [words("SAMPLE #")],
            children=[
                node("one", [{"field": "num", "op": "exact", "value": "1"}], leaf={"channel": "Sample Main"}),
                node("rest", [], leaf={"channel": "Sample #"}),
            ],
        ),
        node("g", [{"field": "tvg", "op": "exact", "value": "demo.tv"}], leaf={"channel": "Demo"}),
    )
    assert T.run(root, {"name": "Sample 1"}).leaf["channel"] == "Sample Main"
    assert T.run(root, {"name": "Sample 2"}).leaf["channel"] == "Sample 2"
    assert T.run(root, {"name": "anything", "tvg": "demo.tv"}).leaf["channel"] == "Demo"


def test_a_none_leaf_and_invalid_user_regex_do_not_raise():
    root = tree(
        node("bad", [{"field": "name", "op": "regex", "value": "("}], leaf={"channel": "Never"}),
        node("skip", [words("RADIO")], steps=[{"type": "repl", "pattern": "(", "repl": "", "cond": None}], leaf={"none": True}),
    )
    st = T.run(root, {"name": "Radio Demo"})
    assert st.leaf == {"node": "skip", "none": True, "channel": None, "guide": ""}


def test_spans_mark_where_the_name_matched():
    root = tree(node("t", [words("DEMO")], leaf={"channel": "Demo"}))
    assert T.run(root, {"name": "The Demo"}).spans["t"] == [(4, 8)]


def test_resolution_of_the_original_name():
    assert T.resolution_of("Sample UHD") == "4K"
    assert T.resolution_of("Sample 1080i") == "1080p"
    assert T.resolution_of("Sample HD") == "720p"
    assert T.resolution_of("Sample HDR") == "—"


def test_resolve_against_existing_channels():
    names = ["Demo Canal", "Sample", "Twin", "TWIN"]
    assert T.resolve("demo  canal", names) == {"status": "ok", "name": "Demo Canal"}
    assert T.resolve("Sample 1", names) == {"status": "ok", "name": "Sample", "implicit_one": True}
    assert T.resolve("Twin", names)["status"] == "amb"
    assert T.resolve("Nuevo", names) == {"status": "new", "name": "Nuevo"}


def test_norm_ignores_accents_case_and_punctuation_but_keeps_plus():
    assert T.norm("Canál  Demo+ ·1") == "canaldemo+1"


def test_a_hand_fix_is_a_branch_on_the_content_id_placed_first():
    fix = node(
        "fix",
        [{"field": "cid", "op": "exact", "value": "c0ffee01"}],
        children=[node("to-b", [{"field": "cid", "op": "exact", "value": "c0ffee01"}], leaf={"channel": "Demo B"})],
    )
    root = tree(fix, node("a", [words("DEMO")], leaf={"channel": "Demo A"}))
    assert T.run(root, {"name": "Demo", "cid": "c0ffee01"}).leaf["channel"] == "Demo B"
    assert T.run(root, {"name": "Demo", "cid": "c0ffee02"}).leaf["channel"] == "Demo A"


def test_groups_set_on_the_leaf_replace_the_inherited_ones():
    sec = node(
        "sec",
        [words("DEMO")],
        group="Sports",
        children=[
            node("own", [words("DEMO ONE")], leaf={"channel": "Demo One", "groups": "General; Local"}),
            node("rest", [], leaf={"channel": "Demo"}),
        ],
    )
    root = tree(sec)
    assert T.run(root, {"name": "Demo One"}).groups == ["General", "Local"]
    assert T.run(root, {"name": "Demo Two"}).groups == ["Sports"]
    sec["children"][0]["leaf"]["groups"] = ""
    assert T.run(root, {"name": "Demo One"}).groups == []
