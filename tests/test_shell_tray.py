import pytest

from airlinx.tray import MenuModel


def test_layout_plain():
    m = MenuModel()
    m.build("Gotowe · Salon", "Salon", False, True)
    rev, (rid, props, kids) = m.layout()
    assert rid == 0 and props == {"children-display": "submenu"}
    labels = [k[1].get("label") for k in kids]
    assert labels == ["Gotowe · Salon", "Udostępnij ekran → Salon", "Otwórz AirLinx", None, "Zakończ"]
    assert kids[0][1]["enabled"] is False
    assert kids[3][1] == {"type": "separator"}
    assert all(k[2] == [] for k in kids)


def test_mirroring_label_and_revision_bump():
    m = MenuModel()
    r0 = m.revision
    m.build("Udostępniasz ekran → Salon", "Salon", True, True)
    assert m.revision == r0 + 1
    assert m.find(MenuModel.ID_MIRROR).label == "Zatrzymaj udostępnianie"
    m.build("Nie znaleziono telewizora", None, False, False)
    it = m.find(MenuModel.ID_MIRROR)
    assert it.label == "Udostępnij ekran" and not it.enabled


def test_layout_depth_and_names():
    m = MenuModel()
    _, (_, _, kids) = m.layout(0, 0)
    assert kids == []
    _, node = m.layout(MenuModel.ID_QUIT, -1, ["label"])
    assert node == (MenuModel.ID_QUIT, {"label": "Zakończ"}, [])


def test_layout_variant_signature():
    pytest.importorskip("gi")
    m = MenuModel()
    v = m.layout_variant(0, -1, [])
    assert v.get_type_string() == "(u(ia{sv}av))"
    rev, (rid, props, kids) = v.unpack()
    assert rev == m.revision and rid == 0 and len(kids) == 5
    assert kids[1][1]["label"].startswith("Udostępnij")
    g = m.group_props_variant([1, 5], ["label"])
    assert g.unpack() == ([(1, {"label": "AirLinx — szukam telewizora…"}), (5, {"label": "Zakończ"})],)
