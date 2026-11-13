"""Applying the channel rules tree to stored streams (synthetic names only)."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect

from app.models.models import AcestreamChannel, ChannelTreeRun, EPGChannel, EPGSource, TVChannel
from app.services.channel_tree_service import ChannelTreeService, TreeRunError, list_label
from tests.migration_test_utils import database_url_for, downgrade_to_revision, upgrade_to_revision


def leaf(label, words, channel, guide="", group="", **extra):
    return {"id": label, "label": label, "match": "any", "conds": [{"field": "name", "op": "words", "value": words}],
            "steps": [], "group": group, "leaf": {"channel": channel, "guide": guide, **extra}, "children": []}


def tree(*children, group=""):
    return {"id": "root", "label": "", "match": "any", "conds": [],
            "steps": [{"type": "prep", "ops": ["upper", "trim", "collapse", "quality"], "cond": None}],
            "group": group, "leaf": None, "children": list(children)}


def section(label, words, *children, group=""):
    return {"id": label, "label": label, "match": "any", "conds": [{"field": "name", "op": "words", "value": words}],
            "steps": [], "group": group, "leaf": None, "children": list(children)}


def stream(db, sid, name, tvg="", tv=None, source_url="https://lists.example/a.m3u"):
    row = AcestreamChannel(id=sid, name=name, tvg_id=tvg, source_url=source_url, tv_channel_id=tv)
    db.add(row)
    db.flush()
    return row


def channel(db, name, **kw):
    tv = TVChannel(name=name, **kw)
    db.add(tv)
    db.flush()
    return tv


def guide(db, *xml_ids):
    src = EPGSource(url="https://guide.example/a.xml", name="Example guide")
    db.add(src)
    db.flush()
    for xml_id in xml_ids:
        db.add(EPGChannel(epg_source_id=src.id, channel_xml_id=xml_id, name=xml_id))
    db.flush()
    return src


def by_name(db, name):
    return db.query(TVChannel).filter(TVChannel.name == name).one()


def test_list_label_names_the_origin_of_a_stream():
    assert list_label(None) == "manual"
    assert list_label("telegram-source:12") == "Telegram"
    assert list_label("zero://1Example/list") == "ZeroNet"
    assert list_label("https://lists.example/a.m3u") == "lists.example"


def test_a_missing_channel_is_created_with_the_groups_the_stream_reaches(db_session):
    stream(db_session, "a1", "ALPHA SPORT 2 HD")
    run = ChannelTreeService(db_session).apply(root=tree(
        section("Sports", "ALPHA SPORT", leaf("Alpha", "ALPHA SPORT", "Alpha Sport #"), group="Sports"), group=""))
    tv = by_name(db_session, "Alpha Sport 2")
    assert db_session.get(AcestreamChannel, "a1").tv_channel_id == tv.id
    assert tv.category == "Sports"
    assert run.stats["created"] == 1 and run.stats["moved"] == 1


def test_an_existing_channel_is_found_ignoring_case_and_a_publisher_code(db_session):
    plain = channel(db_session, "Beta News")
    coded = channel(db_session, "Gamma Kids 28fb")
    stream(db_session, "b1", "BETA NEWS")
    stream(db_session, "g1", "GAMMA KIDS")
    ChannelTreeService(db_session).apply(root=tree(leaf("Beta", "BETA NEWS", "BETA NEWS"),
                                                   leaf("Gamma", "GAMMA KIDS", "Gamma Kids")))
    assert db_session.get(AcestreamChannel, "b1").tv_channel_id == plain.id
    assert db_session.get(AcestreamChannel, "g1").tv_channel_id == coded.id
    assert db_session.query(TVChannel).count() == 2


def test_new_names_differing_only_in_case_become_one_channel_named_after_the_one_with_a_guide(db_session):
    stream(db_session, "d1", "DELTA ONE")
    stream(db_session, "d2", "Delta One", tvg="Delta.One.example")
    ChannelTreeService(db_session).apply(root=tree(leaf("Delta", "DELTA ONE", "{nombre}")))
    tv = db_session.query(TVChannel).one()
    assert tv.name == "Delta One"
    assert {r.tv_channel_id for r in db_session.query(AcestreamChannel)} == {tv.id}


def test_a_name_matching_two_channels_is_left_alone_and_reported(db_session):
    channel(db_session, "Epsilon TV")
    channel(db_session, "EPSILON-TV")
    stream(db_session, "e1", "EPSILON TV")
    run = ChannelTreeService(db_session).apply(root=tree(leaf("Eps", "EPSILON TV", "Epsilon TV")))
    assert db_session.get(AcestreamChannel, "e1").tv_channel_id is None
    assert run.stats["ambiguous"] == 1 and run.stats["ambiguous_names"] == ["Epsilon TV"]


def test_a_stream_in_a_remainder_keeps_its_channel(db_session):
    old = channel(db_session, "Zeta")
    stream(db_session, "z1", "ZETA SOMETHING", tv=old.id)
    run = ChannelTreeService(db_session).apply(root=tree(section("Other", "ZETA", leaf("x", "NOPE", "Nope"))))
    assert db_session.get(AcestreamChannel, "z1").tv_channel_id == old.id
    assert run.stats["rest"] == 1 and run.stats.get("moved", 0) == 0


def test_the_majority_guide_needs_two_votes_and_a_loaded_guide(db_session):
    guide(db_session, "Eta.example", "Theta.example")
    for i, tvg in enumerate(["Eta.example", "Eta.example", "Other.example"]):
        stream(db_session, f"h{i}", "ETA", tvg=tvg)
    stream(db_session, "t1", "THETA", tvg="Theta.example")
    ChannelTreeService(db_session).apply(root=tree(leaf("Eta", "ETA", "Eta"), leaf("Theta", "THETA", "Theta")))
    assert by_name(db_session, "Eta").epg_id == "Eta.example"
    assert by_name(db_session, "Theta").epg_id is None  # a single vote is only a suggestion


def test_a_channel_with_a_guide_keeps_it_and_a_different_majority_is_a_warning(db_session):
    guide(db_session, "Iota.example", "Iota.other")
    tv = channel(db_session, "Iota", epg_id="Iota.example")
    for i in range(2):
        stream(db_session, f"i{i}", "IOTA", tvg="Iota.other")
    run = ChannelTreeService(db_session).apply(root=tree(leaf("Iota", "IOTA", "Iota")))
    assert tv.epg_id == "Iota.example"
    assert run.stats["warnings"] == [{"channel": "Iota", "kind": "majority_differs", "guide": "Iota.other"}]


def test_a_guide_written_in_the_leaf_or_no_guide_wins(db_session):
    src = guide(db_session, "Kappa.example", "Lambda.example")
    kappa = channel(db_session, "Kappa", epg_id="Kappa.old")
    lam = channel(db_session, "Lambda", epg_id="Lambda.example", epg_source_id=src.id)
    stream(db_session, "k1", "KAPPA")
    for i in range(2):
        stream(db_session, f"l{i}", "LAMBDA", tvg="Lambda.example")
    ChannelTreeService(db_session).apply(root=tree(leaf("Kappa", "KAPPA", "Kappa", guide="Kappa.example"),
                                                   leaf("Lambda", "LAMBDA", "Lambda", noGuide=True)))
    assert (kappa.epg_id, kappa.epg_source_id) == ("Kappa.example", src.id)
    assert lam.epg_id is None and lam.epg_source_id is None


def test_undo_puts_back_what_the_run_replaced_and_removes_empty_channels_it_created(db_session):
    guide(db_session, "Mu.example")
    old = channel(db_session, "Mu old", category="Old")
    mu = channel(db_session, "Mu", category="Kept")
    stream(db_session, "m1", "MU", tvg="Mu.example", tv=old.id)
    stream(db_session, "m2", "MU", tvg="Mu.example")
    stream(db_session, "n1", "NU")
    service = ChannelTreeService(db_session)
    run = service.apply(root=tree(leaf("Mu", "MU", "Mu", group="Extra"), leaf("Nu", "NU", "Nu")))
    assert mu.category == "Kept; Extra" and mu.epg_id == "Mu.example"
    # Edited by hand after the run: undo still restores what the run replaced.
    mu.category = "Hand"
    result = service.undo(run.id)
    assert db_session.get(AcestreamChannel, "m1").tv_channel_id == old.id
    assert db_session.get(AcestreamChannel, "m2").tv_channel_id is None
    assert mu.category == "Kept" and mu.epg_id is None
    assert db_session.query(TVChannel).filter(TVChannel.name == "Nu").count() == 0
    assert result == {"streams": 3, "channels_removed": 1}
    with pytest.raises(TreeRunError):
        service.undo(run.id)


def test_undo_keeps_a_created_channel_that_has_streams_put_there_since(db_session):
    stream(db_session, "x1", "XI")
    service = ChannelTreeService(db_session)
    run = service.apply(root=tree(leaf("Xi", "XI", "Xi")))
    xi = by_name(db_session, "Xi")
    stream(db_session, "x2", "other name", tv=xi.id)
    db_session.flush()
    assert service.undo(run.id) == {"streams": 1, "channels_kept": 1}
    assert db_session.get(TVChannel, xi.id) is not None


def test_a_run_older_than_a_week_cannot_be_undone(db_session):
    stream(db_session, "o1", "OMICRON")
    service = ChannelTreeService(db_session)
    run = service.apply(root=tree(leaf("Omicron", "OMICRON", "Omicron")))
    run.created_at = run.created_at - timedelta(days=8)
    db_session.flush()
    with pytest.raises(TreeRunError):
        service.undo(run.id)


def test_the_stored_tree_is_used_when_none_is_given(db_session):
    service = ChannelTreeService(db_session)
    assert service.apply() is None
    service.save_tree(tree(leaf("Pi", "PI", "Pi")))
    stream(db_session, "p1", "PI")
    assert service.apply(["p1"]).stats["created"] == 1
    with pytest.raises(TreeRunError):
        service.save_tree({"label": "no children"})


def test_channel_tree_runs_migration_upgrade_and_downgrade(tmp_path):
    path = tmp_path / "runs.sqlite"
    upgrade_to_revision(path, "20261112_1200")
    engine = create_engine(database_url_for(path))
    try:
        tables = inspect(engine).get_table_names()
        assert {"channel_tree_runs", "channel_tree_changes"} <= set(tables)
        downgrade_to_revision(path, "20261111_1200")
        assert "channel_tree_runs" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


A, B = ("a" * 40, "b" * 40)
SOURCE = "https://lists.example.invalid/channels.m3u"


@pytest.mark.parametrize("mode", ["snapshot", "insert_only"])
def test_with_the_switch_on_an_import_puts_new_unassigned_streams_through_the_tree(db_session, mode, monkeypatch):
    from app.repositories.settings_repository import SettingsRepository
    from app.services.channel_tree_service import AUTO_SETTING
    from app.services.m3u_service import M3UService
    from app.services.import_pipeline import ImportEntry, ImportPipeline, ImportReference

    kept = channel(db_session, "Rho")
    stream(db_session, B, "SIGMA", tv=kept.id, source_url=SOURCE)
    db_session.commit()
    service = ChannelTreeService(db_session)
    service.save_tree(tree(leaf("Sigma", "SIGMA", "Sigma")))
    SettingsRepository(db_session).set_setting(AUTO_SETTING, "true")

    def guide_step(*_args, **_kwargs):
        raise AssertionError("with the tree on, the guide step must not assign channels")
    monkeypatch.setattr(M3UService, "auto_create_tv_channels_from_epg", guide_step)

    ImportPipeline.apply(db_session, ImportReference(SOURCE),
                         [ImportEntry(A, "SIGMA"), ImportEntry(B, "SIGMA")], mode)
    sigma = by_name(db_session, "Sigma")
    assert db_session.get(AcestreamChannel, A).tv_channel_id == sigma.id
    assert db_session.get(AcestreamChannel, B).tv_channel_id == kept.id  # assigned rows keep their channel
    assert db_session.query(ChannelTreeRun).one().trigger == "auto"


def test_with_the_switch_off_an_import_does_not_run_the_tree(db_session):
    from app.services.import_pipeline import ImportEntry, ImportPipeline, ImportReference
    ChannelTreeService(db_session).save_tree(tree(leaf("Tau", "TAU", "Tau")))
    ImportPipeline.apply(db_session, ImportReference(SOURCE), [ImportEntry(A, "TAU")], "insert_only")
    assert db_session.get(AcestreamChannel, A).tv_channel_id is None
    assert db_session.query(ChannelTreeRun).count() == 0


def test_the_api_stores_the_tree_previews_without_writing_applies_and_undoes(client, db_session):
    assert client.post("/api/v1/channel-tree/preview", json={"scope": "all"}).json()["error"]["code"] == "CHANNEL_TREE_MISSING"
    r = client.put("/api/v1/channel-tree", json={"tree": tree(leaf("Upsilon", "UPSILON", "Upsilon")), "auto": True})
    assert r.status_code == 200 and r.json()["auto"] is True and r.json()["tree"]["id"] == "root"
    stream(db_session, "u1", "UPSILON")
    db_session.commit()

    preview = client.post("/api/v1/channel-tree/preview", json={"scope": "unassigned"}).json()
    assert preview["stats"]["created"] == 1
    assert [c["kind"] for c in preview["changes"]] == ["create", "assign"]
    db_session.expire_all()
    assert db_session.query(TVChannel).count() == 0 and db_session.query(ChannelTreeRun).count() == 0

    run = client.post("/api/v1/channel-tree/apply", json={"scope": "all"}).json()
    assert run["trigger"] == "manual" and run["stats"]["moved"] == 1
    assert [x["id"] for x in client.get("/api/v1/channel-tree/runs").json()] == [run["id"]]
    undo = client.post(f"/api/v1/channel-tree/runs/{run['id']}/undo").json()
    assert undo == {"streams": 1, "channels_removed": 1, "channels_kept": 0}
    assert client.post(f"/api/v1/channel-tree/runs/{run['id']}/undo").status_code == 409
    assert client.post("/api/v1/channel-tree/runs/999999/undo").json()["error"]["code"] == "CHANNEL_TREE_RUN_NOT_FOUND"
    assert client.put("/api/v1/channel-tree", json={"tree": {"label": "bad"}}).status_code == 422


@pytest.mark.parametrize("bad, problem", [
    ({"id": "root", "children": [{"label": "no id", "children": []}]}, "id"),
    ({"id": "root", "children": [leaf("Phi", "PHI", "Phi") | {"conds": ["PHI"]}]}, "cond"),
    ({"id": "root", "children": [leaf("Phi", "PHI", "Phi") | {"conds": [{"field": "nombre", "op": "words", "value": "PHI"}]}]}, "field"),
    ({"id": "root", "children": [leaf("Phi", "PHI", "Phi") | {"children": {"id": "x"}}]}, "children"),
    ({"id": "root", "children": [leaf("Phi", "PHI", "Phi"), leaf("Phi", "CHI", "Chi")]}, "twice"),
    ({"id": "root", "children": [leaf("Phi", "PHI", "Phi") | {"leaf": "Phi"}]}, "leaf"),
    ({"id": "root", "children": [leaf("Phi", "PHI", "Phi") | {"steps": [{"type": "repl", "pattern": 3}]}]}, "step"),
])
def test_a_malformed_tree_is_refused_when_stored(db_session, bad, problem):
    with pytest.raises(TreeRunError, match=problem):
        ChannelTreeService(db_session).save_tree(bad)
    assert ChannelTreeService(db_session).tree() is None


def test_an_import_survives_a_stored_tree_that_fails(db_session, monkeypatch):
    from app.repositories.settings_repository import SettingsRepository
    from app.services.channel_tree_service import AUTO_SETTING, TREE_SETTING
    from app.services.import_pipeline import ImportEntry, ImportPipeline, ImportReference
    import json
    # Stored before validation existed, or by hand: a child without an id.
    SettingsRepository(db_session).set_setting(TREE_SETTING, json.dumps({"id": "root", "children": [{"conds": []}]}))
    SettingsRepository(db_session).set_setting(AUTO_SETTING, "true")
    ImportPipeline.apply(db_session, ImportReference(SOURCE), [ImportEntry(A, "PSI")], "insert_only")
    db_session.commit()
    assert db_session.get(AcestreamChannel, A).name == "PSI"
    assert db_session.get(AcestreamChannel, A).tv_channel_id is None
    assert db_session.query(ChannelTreeRun).count() == 0


@pytest.mark.parametrize("mode", ["snapshot", "insert_only"])
def test_the_automatic_pass_leaves_inactive_and_protected_streams_alone(db_session, mode):
    from app.repositories.settings_repository import SettingsRepository
    from app.services.channel_tree_service import AUTO_SETTING
    from app.services.import_pipeline import ImportEntry, ImportPipeline, ImportReference
    C = "c" * 40
    off = stream(db_session, B, "OMEGA", source_url=SOURCE)
    off.is_active = False
    kept = stream(db_session, C, "OMEGA", source_url=SOURCE)
    kept.epg_update_protected = True
    db_session.commit()
    ChannelTreeService(db_session).save_tree(tree(leaf("Omega", "OMEGA", "Omega")))
    SettingsRepository(db_session).set_setting(AUTO_SETTING, "true")
    ImportPipeline.apply(db_session, ImportReference(SOURCE),
                         [ImportEntry(A, "OMEGA"), ImportEntry(B, "OMEGA"), ImportEntry(C, "OMEGA")], mode)
    omega = by_name(db_session, "Omega")
    assert db_session.get(AcestreamChannel, A).tv_channel_id == omega.id
    assert db_session.get(AcestreamChannel, B).tv_channel_id is None
    assert db_session.get(AcestreamChannel, C).tv_channel_id is None


def test_a_pass_that_changes_nothing_leaves_no_run(db_session):
    stream(db_session, "n1", "NOBODY KNOWS")
    service = ChannelTreeService(db_session)
    assert service.apply(trigger="auto", root=tree(section("Section", "NOBODY"))) is None
    assert db_session.query(ChannelTreeRun).count() == 0
    # The preview still says what it saw.
    assert service.preview("all", root=tree(section("Section", "NOBODY")))["stats"]["rest"] == 1


def test_runs_past_the_undo_window_are_dropped_by_the_next_run(db_session):
    from app.models.models import _utcnow
    service = ChannelTreeService(db_session)
    stream(db_session, "o1", "OLD ONE")
    old = service.apply(root=tree(leaf("Old", "OLD ONE", "Old")))
    old.created_at = _utcnow() - timedelta(days=8)
    db_session.flush()
    stream(db_session, "o2", "NEW ONE")
    new = service.apply(["o2"], root=tree(leaf("New", "NEW ONE", "New")))
    assert [r.id for r in service.runs()] == [new.id]


def test_a_run_cannot_be_undone_while_a_later_run_changed_the_same_streams(db_session):
    service = ChannelTreeService(db_session)
    stream(db_session, "l1", "LAMBDA")
    first = service.apply(root=tree(leaf("Lambda", "LAMBDA", "Lambda", group="G1")))
    second = service.apply(root=tree(leaf("Lambda", "LAMBDA", "Lambda Two", group="G2")))
    with pytest.raises(TreeRunError, match=f"run {second.id}"):
        service.undo(first.id)
    assert db_session.get(AcestreamChannel, "l1").tv_channel_id == by_name(db_session, "Lambda Two").id
    service.undo(second.id)
    service.undo(first.id)
    assert db_session.get(AcestreamChannel, "l1").tv_channel_id is None
    # An unrelated later run does not block.
    stream(db_session, "m1", "MU")
    third = service.apply(["l1"], root=tree(leaf("Lambda", "LAMBDA", "Lambda")))
    service.apply(["m1"], root=tree(leaf("Mu", "MU", "Mu")))
    service.undo(third.id)


def test_a_moved_stream_loses_its_place_in_the_old_channel(db_session):
    old = channel(db_session, "Nu Old")
    row = stream(db_session, "n1", "NU", tv=old.id)
    row.stream_order = 3
    run = ChannelTreeService(db_session).apply(root=tree(leaf("Nu", "NU", "Nu")))
    assert row.stream_order is None
    row.stream_order = 1
    ChannelTreeService(db_session).undo(run.id)
    assert row.tv_channel_id == old.id and row.stream_order is None


def test_undo_unlinks_recordings_of_a_removed_channel_and_spares_a_reused_id(db_session):
    from datetime import datetime, timezone
    from app.models.recordings import Recording
    service = ChannelTreeService(db_session)
    stream(db_session, "x1", "XI")
    run = service.apply(root=tree(leaf("Xi", "XI", "Xi")))
    xi = by_name(db_session, "Xi")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rec = Recording(tv_channel_id=xi.id, snapshot={}, start_time=now, end_time=now + timedelta(hours=1), timezone="UTC",
                    idempotency_key="k1", request_hash="h1")
    db_session.add(rec)
    db_session.flush()
    assert service.undo(run.id)["channels_removed"] == 1
    assert rec.tv_channel_id is None

    stream(db_session, "o1", "OMICRON")
    run = service.apply(["o1"], root=tree(leaf("Omicron", "OMICRON", "Omicron")))
    omicron = by_name(db_session, "Omicron")
    reused_id = omicron.id
    db_session.get(AcestreamChannel, "o1").tv_channel_id = None
    db_session.delete(omicron)
    db_session.flush()
    other = channel(db_session, "Someone Else", id=reused_id)  # SQLite hands the id out again
    service.undo(run.id)
    assert db_session.get(TVChannel, other.id) is not None


def test_the_preview_says_why_each_stream_moves_and_where_the_rest_stopped(db_session):
    old = channel(db_session, "Pi Old")
    channel(db_session, "Rho 3f2a")
    stream(db_session, "p1", "PI", tv=old.id)
    stream(db_session, "r1", "RHO")
    stream(db_session, "s1", "SECTION STRAY")
    root = tree(leaf("Pi leaf", "PI", "Pi"), leaf("Rho leaf", "RHO", "Rho"),
                section("Section", "SECTION", leaf("Inner", "INNER", "Inner")))
    out = ChannelTreeService(db_session).preview("all", root=root)
    moves = {c["stream_id"]: c for c in out["changes"] if c["kind"] == "assign"}
    assert moves["p1"]["stream_name"] == "PI" and moves["p1"]["before_channel"] == "Pi Old"
    assert moves["p1"]["path"] == ["Pi leaf"] and moves["p1"]["how"] == "new"
    assert moves["r1"]["channel"] == "Rho 3f2a" and moves["r1"]["how"] == "publisher_code"
    assert out["unplaced"] == [{"stream_id": "s1", "stream_name": "SECTION STRAY", "status": "rest",
                                "channel": None, "candidates": [], "path": ["Section"]}]
    assert {n["label"]: n["streams"] for n in out["nodes"]} == {"root": 3, "Pi leaf": 1, "Rho leaf": 1, "Section": 1}
    assert out["total_changes"] == len(out["changes"]) and out["truncated"] is False
    cut = ChannelTreeService(db_session).preview("all", root=root, limit=1)
    assert len(cut["changes"]) == 1 and cut["total_changes"] > 1 and cut["truncated"] is True


def test_the_api_explains_one_stream_without_writing(client, db_session):
    client.put("/api/v1/channel-tree", json={"tree": tree(leaf("Sigma leaf", "SIGMA", "Sigma"))})
    stream(db_session, "g1", "SIGMA")
    db_session.commit()
    r = client.post("/api/v1/channel-tree/explain", json={"stream_id": "g1"}).json()
    assert r["channel"] == "Sigma" and r["how"] == "new" and r["path"] == ["Sigma leaf"]
    assert r["current_channel"] is None
    db_session.expire_all()
    assert db_session.query(TVChannel).count() == 0
    assert client.post("/api/v1/channel-tree/explain", json={"stream_id": "nope"}).status_code == 404


def test_the_majority_guide_is_not_taken_on_a_tie_or_when_no_loaded_guide_has_it(db_session):
    guide(db_session, "Tie.a", "Tie.b")
    for i, tvg in enumerate(["Tie.a", "Tie.a", "Tie.b", "Tie.b"]):
        stream(db_session, f"ti{i}", "TIE", tvg=tvg)
    for i in range(3):
        stream(db_session, f"un{i}", "UNLOADED", tvg="Unloaded.example")
    ChannelTreeService(db_session).apply(root=tree(leaf("Tie", "TIE", "Tie"), leaf("Unloaded", "UNLOADED", "Unloaded")))
    assert by_name(db_session, "Tie").epg_id is None
    assert by_name(db_session, "Unloaded").epg_id is None


def test_leaf_guides_that_disagree_or_are_not_loaded_are_warnings(db_session):
    guide(db_session, "Upsilon.example")
    stream(db_session, "ua", "UPSILON A")
    stream(db_session, "ub", "UPSILON B")
    stream(db_session, "ph", "PHI")
    run = ChannelTreeService(db_session).apply(root=tree(
        leaf("Ups A", "UPSILON A", "Upsilon", guide="Upsilon.example"),
        leaf("Ups B", "UPSILON B", "Upsilon", guide="Upsilon.other"),
        leaf("Phi", "PHI", "Phi", guide="Phi.missing")))
    assert by_name(db_session, "Upsilon").epg_id is None and by_name(db_session, "Phi").epg_id is None
    kinds = {(w["channel"], w["kind"]) for w in run.stats["warnings"]}
    assert kinds == {("Upsilon", "leaf_guides_disagree"), ("Phi", "leaf_guide_not_loaded")}


def test_saving_the_tree_and_its_switch_leaves_the_commit_to_the_caller(db_session):
    service = ChannelTreeService(db_session)
    service.save_tree(tree(leaf("Chi", "CHI", "Chi")))
    service.set_auto(True)
    assert service.tree() is not None and service.auto_enabled()
    db_session.rollback()
    # Nothing was committed on the way: an error later in the same request loses both.
    assert service.tree() is None
    assert not service.auto_enabled()


def test_a_bad_tree_in_a_put_does_not_switch_automatic_runs_on(client):
    r = client.put("/api/v1/channel-tree", json={"tree": {"label": "bad"}, "auto": True})
    assert r.status_code == 422
    assert client.get("/api/v1/channel-tree").json() == {"tree": None, "auto": False}


def test_apply_undo_and_save_take_the_import_claim_before_reading(db_session):
    from sqlalchemy import event
    service = ChannelTreeService(db_session)
    statements: list[str] = []

    def seen(conn, cursor, statement, *args):
        statements.append(statement.lstrip().split()[0].upper() + " " + statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", seen)
    try:
        service.save_tree(tree(leaf("Psi", "PSI", "Psi")))
        stream(db_session, "s1", "PSI")
        statements.clear()
        run = service.apply(["s1"])
        apply_sql = list(statements)
        statements.clear()
        service.undo(run.id)
        undo_sql = list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", seen)

    def claim_first(sql, table):
        claim = next(i for i, s in enumerate(sql) if s.startswith("UPDATE") and "import_policy_state" in s)
        read = next(i for i, s in enumerate(sql) if s.startswith("SELECT") and table in s)
        return claim < read

    assert claim_first(apply_sql, "acestream_channels")
    assert claim_first(undo_sql, "channel_tree_runs")
