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
