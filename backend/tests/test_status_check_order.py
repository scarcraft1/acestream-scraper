"""The status pass checks first what was never checked.

It used to walk the table in insertion order and restart from row 1 on every
start. A box switched on for an hour or two never finished a pass (about 3 h
for 1,000 sources): it kept checking the same first rows and never reached
the last ones. On a real box 58% were checked and the tail never was.
"""
from datetime import datetime, timedelta, timezone

from app.models.models import AcestreamChannel, TVChannel
from app.tasks.channel_status_task import channels_in_check_order

T0 = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)


def _stream(db, cid, last_checked=None, tv=None, active=True):
    db.add(AcestreamChannel(id=cid, name=cid, is_active=active,
                            last_checked=last_checked,
                            tv_channel_id=tv.id if tv else None))


def _tv(db, name, favourite):
    t = TVChannel(name=name, is_favorite=favourite)
    db.add(t)
    db.flush()
    return t


def test_LO_NUNCA_COMPROBADO_VA_PRIMERO_aunque_se_insertara_al_final(db_session):
    """El caso de la caja: lo ultimo que se subio no se comprobaba nunca."""
    _stream(db_session, "vieja", T0 - timedelta(days=1))
    _stream(db_session, "reciente", T0)
    _stream(db_session, "nunca")                      # insertada la ultima
    db_session.commit()
    assert channels_in_check_order(db_session)[0] == "nunca"


def test_DESPUES_LOS_FAVORITOS(db_session):
    fav = _tv(db_session, "M+ Liga de Campeones", True)
    otro = _tv(db_session, "Otro", False)
    _stream(db_session, "de_otro", T0 - timedelta(days=3), tv=otro)
    _stream(db_session, "de_favorito", T0, tv=fav)
    db_session.commit()
    assert channels_in_check_order(db_session) == ["de_favorito", "de_otro"]


def test_LO_NUNCA_COMPROBADO_GANA_INCLUSO_A_UN_FAVORITO(db_session):
    """La cola que nunca se alcanzaba va antes que todo.

    Sin este caso el nivel de «nunca comprobadas» no se distinguia: en
    SQLite los NULL ya ordenan primero en ASC, y solo frente a un favorito
    YA comprobado se ve si el nivel esta o no.
    """
    fav = _tv(db_session, "Favorito", True)
    _stream(db_session, "favorito_comprobado", T0, tv=fav)
    _stream(db_session, "nunca_sin_favorito")
    db_session.commit()
    assert channels_in_check_order(db_session) == [
        "nunca_sin_favorito", "favorito_comprobado"]


def test_LUEGO_LO_COMPROBADO_HACE_MAS_TIEMPO(db_session):
    _stream(db_session, "ayer", T0 - timedelta(days=1))
    _stream(db_session, "hoy", T0)
    _stream(db_session, "anteayer", T0 - timedelta(days=2))
    db_session.commit()
    assert channels_in_check_order(db_session) == ["anteayer", "ayer", "hoy"]


def test_UNA_VUELTA_CORTADA_SE_REANUDA_SOLA(db_session):
    """Lo que se acaba de comprobar pasa al final: al reiniciar no se repite."""
    for i in range(4):
        _stream(db_session, f"s{i}")
    db_session.commit()
    primera = channels_in_check_order(db_session)
    # se apaga despues de comprobar las dos primeras
    for cid in primera[:2]:
        db_session.get(AcestreamChannel, cid).last_checked = T0
    db_session.commit()
    segunda = channels_in_check_order(db_session)
    assert segunda[:2] == primera[2:], (primera, segunda)


def test_LAS_INACTIVAS_NO_ENTRAN(db_session):
    """Guarda: lo que ya filtraba, sigue filtrado."""
    _stream(db_session, "activa")
    _stream(db_session, "inactiva", active=False)
    db_session.commit()
    assert channels_in_check_order(db_session) == ["activa"]


def test_LAS_DE_UN_CANAL_ANTES_QUE_LAS_SUELTAS(db_session):
    """En la caja hay 0 favoritos: sin este nivel, las 613 fuentes de canales
    iban mezcladas con 935 sueltas que la lista del rele no abre."""
    otro = _tv(db_session, "La 1", False)
    _stream(db_session, "suelta_vieja", T0 - timedelta(days=5))
    _stream(db_session, "de_canal_reciente", T0, tv=otro)
    db_session.commit()
    assert channels_in_check_order(db_session) == ["de_canal_reciente", "suelta_vieja"]


def test_EL_FAVORITO_SIGUE_ANTES_QUE_OTRO_CANAL(db_session):
    fav = _tv(db_session, "Favorito", True)
    otro = _tv(db_session, "Otro", False)
    _stream(db_session, "de_otro_viejo", T0 - timedelta(days=5), tv=otro)
    _stream(db_session, "de_favorito_reciente", T0, tv=fav)
    db_session.commit()
    assert channels_in_check_order(db_session) == ["de_favorito_reciente", "de_otro_viejo"]
