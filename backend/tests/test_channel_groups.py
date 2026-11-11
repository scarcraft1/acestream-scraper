"""Channel groups: stable numbers by group block (synthetic names only)."""

from sqlalchemy import create_engine, inspect

from app.services import channel_numbering as N
from tests.migration_test_utils import database_url_for, downgrade_to_revision, upgrade_to_revision


def G(name, position, parent="", block=None, members=()):
    return N.Group(name, parent, position, block, list(members))


def C(id, *groups, number=None, order=None):
    return N.Channel(id, list(groups), order if order is not None else (number if number is not None else id), number)


def test_top_groups_get_blocks_in_tab_order_and_channels_fill_them():
    r = N.number_channels([G("Alpha", 0), G("Beta", 1)], [C(1, "Beta"), C(2, "Alpha"), C(3, "Alpha"), C(4)])
    assert r.blocks == {"Alpha": 100, "Beta": 200}
    assert r.numbers == {2: 100, 3: 101, 1: 200, 4: N.NO_GROUP}


def test_a_channel_keeps_its_number_inside_its_block_and_a_new_one_takes_the_first_gap():
    groups = [G("Alpha", 0, block=100)]
    r = N.number_channels(groups, [C(1, "Alpha", number=105), C(2, "Alpha", number=100), C(3, "Alpha")])
    assert r.numbers == {2: 100, 1: 105, 3: 101}


def test_moving_tabs_keeps_numbers_and_warns_until_blocks_are_reordered():
    groups = [G("Alpha", 1, block=100), G("Beta", 0, block=200)]
    chans = [C(1, "Alpha", number=100), C(2, "Beta", number=200)]
    r = N.number_channels(groups, chans)
    assert r.numbers == {1: 100, 2: 200}
    assert r.out_of_order == ["Alpha", "Beta"]
    blocks, moved = N.reblock(groups, r.numbers)
    assert blocks == {"Beta": 100, "Alpha": 200}
    assert moved == {1: 200, 2: 100}
    groups = [G("Alpha", 1, block=200), G("Beta", 0, block=100)]
    r = N.number_channels(groups, [C(1, "Alpha", number=200), C(2, "Beta", number=100)])
    assert r.out_of_order == []


def test_a_channel_outside_its_block_moves_into_it():
    r = N.number_channels([G("Alpha", 0, block=100), G("Beta", 1, block=200)], [C(1, "Beta", number=101)])
    assert r.numbers == {1: 200}


def test_subgroups_number_inside_the_top_block_and_the_channel_waits_for_its_last_tab():
    groups = [G("Sport", 0), G("League", 0, parent="Sport"), G("General", 1)]
    chans = [C(1, "League", "General"), C(2, "League"), C(3, "Sport")]
    r = N.number_channels(groups, chans)
    assert r.blocks == {"Sport": 100, "General": 200}
    # 1 belongs to League and General: it waits until General so that tab order holds.
    assert r.home == {3: "Sport", 2: "League", 1: "General"}
    assert r.numbers == {3: 100, 2: 101, 1: 200}


def test_order_chosen_inside_a_group_wins_over_numbers():
    r = N.number_channels([G("Alpha", 0, members=[3, 1])], [C(1, "Alpha"), C(2, "Alpha"), C(3, "Alpha")])
    assert [cid for cid in r.order] == [3, 1, 2]


def test_channels_already_numbered_follow_an_explicit_reorder_of_their_block_only():
    groups = [G("Alpha", 0, block=100, members=[3, 1, 2]), G("Beta", 1, block=200)]
    chans = [C(1, "Alpha", number=100), C(2, "Alpha", number=101), C(3, "Alpha", number=105), C(4, "Beta", number=200)]
    # Ordinary numbering keeps numbers that are already inside the block.
    assert N.number_channels(groups, chans).numbers == {1: 100, 2: 101, 3: 105, 4: 200}
    # The explicit reorder shares out the same numbers in the chosen order; Beta does not move.
    assert N.reorder_block(groups, chans, "Alpha") == {3: 100, 1: 101, 2: 105, 4: 200}


def test_reordering_subgroups_renumbers_their_top_block():
    groups = [G("Sport", 0, block=100), G("League", 1, parent="Sport"), G("Cup", 0, parent="Sport")]
    chans = [C(1, "League", number=100), C(2, "Cup", number=101)]
    assert N.number_channels(groups, chans).numbers == {1: 100, 2: 101}
    assert N.reorder_block(groups, chans, "Sport") == {2: 100, 1: 101}


def test_a_group_whose_channels_all_wait_drags_the_one_with_fewest_pending_tabs():
    groups = [G("Alpha", 0), G("Beta", 1), G("Gamma", 2)]
    chans = [C(1, "Alpha", "Beta", "Gamma"), C(2, "Alpha", "Gamma")]
    r = N.number_channels(groups, chans)
    assert r.drags == [("Alpha", 2)]
    assert r.home[2] == "Alpha"


def test_a_full_block_spills_and_is_reported():
    chans = [C(i, "Alpha") for i in range(1, N.BLOCK + 2)]
    r = N.number_channels([G("Alpha", 0), G("Beta", 1)], chans)
    assert r.overflow == ["Alpha"]
    assert r.numbers[N.BLOCK + 1] == 200


def test_service_creates_missing_groups_and_writes_stable_numbers(db_session):
    from app.models.models import ChannelGroup, TVChannel
    from app.services import channel_group_service as svc

    db_session.add_all([
        TVChannel(name="Demo Uno", category="Alpha"),
        TVChannel(name="Demo Dos", category="Beta; Alpha"),
        TVChannel(name="Demo Tres"),
    ])
    db_session.commit()
    svc.renumber(db_session)
    groups = {g.name: g for g in db_session.query(ChannelGroup)}
    assert {n: g.number_block for n, g in groups.items()} == {"Alpha": 100, "Beta": 200}
    numbers = {c.name: c.channel_number for c in db_session.query(TVChannel)}
    assert numbers == {"Demo Uno": 100, "Demo Dos": 200, "Demo Tres": N.NO_GROUP}

    # Moving Beta first keeps every number; reblocking swaps the blocks and keeps the offsets.
    groups["Beta"].position, groups["Alpha"].position = 0, 1
    db_session.add(TVChannel(name="Demo Cuatro", category="Alpha"))
    db_session.commit()
    svc.renumber(db_session)
    numbers = {c.name: c.channel_number for c in db_session.query(TVChannel)}
    assert numbers == {"Demo Uno": 100, "Demo Dos": 200, "Demo Tres": N.NO_GROUP, "Demo Cuatro": 101}
    svc.reblock(db_session)
    numbers = {c.name: c.channel_number for c in db_session.query(TVChannel)}
    assert numbers == {"Demo Uno": 200, "Demo Dos": 100, "Demo Tres": N.NO_GROUP, "Demo Cuatro": 201}

    # An order chosen inside Alpha renumbers only Alpha's channels, with the numbers they held.
    ids = {c.name: c.id for c in db_session.query(TVChannel)}
    svc.reorder_group(db_session, "Alpha", [ids["Demo Cuatro"], ids["Demo Uno"]])
    numbers = {c.name: c.channel_number for c in db_session.query(TVChannel)}
    assert numbers == {"Demo Uno": 201, "Demo Dos": 100, "Demo Tres": N.NO_GROUP, "Demo Cuatro": 200}


def test_channel_groups_migration_upgrade_and_downgrade(tmp_path):
    path = tmp_path / "groups.sqlite"
    upgrade_to_revision(path, "20261111_1200")
    engine = create_engine(database_url_for(path))
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("channel_groups")}
        assert {"id", "name", "parent_id", "position", "number_block", "member_order"} <= columns
        downgrade_to_revision(path, "20261109_1300")
        assert "channel_groups" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
