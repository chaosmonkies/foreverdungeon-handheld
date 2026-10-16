import pytest

from foreverdungeon_handheld.layout import Row, Screen, fit_font, metrics, paginate, render, text_width

SIZES = [(200, 200), (250, 122), (296, 128), (400, 300), (800, 480), (122, 250), (300, 400), (480, 800)]

LONG = ("The rain hammers the shutters of the Lantern Tavern while a hooded stranger counts coins by the fire. "
        "Somewhere below the floorboards, something heavy shifts. ") * 4


def big_screen(cursor=0):
    rows = [Row("text", LONG), Row("gap")]
    for s in range(4):
        rows.append(Row("heading", f"Section {s} with a fairly long heading title"))
        rows.append(Row("text", LONG[: 120 + 40 * s]))
        for i in range(4):
            rows.append(Row("item", f"Choice {s}.{i}: walk carefully toward the {['north', 'cellar', 'bar', 'stairs'][i]} and see"))
    return Screen(("t",), "The Lantern Tavern", "HP 12/14 OFFLINE", rows, cursor=cursor, footer="Enter:do t:type")


@pytest.mark.parametrize("w,h", SIZES)
def test_pagination(w, h):
    scr = big_screen()
    pages = paginate(scr, w, h)
    assert len(pages) >= 2
    m = metrics(w, h)
    budget = h - m.header_h - m.footer_h - 2
    items_seen = []
    for page in pages:
        assert sum(l.height for l in page) <= budget
        content = [l for l in page if l.kind != "gap"]
        assert content, "empty page"
        assert content[-1].kind != "heading", "orphaned heading at page bottom"
        for l in page:
            assert text_width(l.text, l.font) <= w - 2 * m.margin + 0.5
        items_seen += sorted({l.item for l in page if l.item is not None})
    assert items_seen == list(range(16))  # every item once, never split across pages
    for cursor in (0, 7, 15):
        img, pi, n = render(big_screen(cursor), w, h)
        assert img.mode == "1" and img.size == (w, h) and n == len(pages)
        assert any(l.item == cursor for l in pages[pi])
        assert set(img.convert("L").tobytes()) <= {0, 255}  # pure 1-bit contrast


@pytest.mark.parametrize("w,h", SIZES)
def test_pairing_code_fits(w, h):
    from foreverdungeon_handheld.app import App
    code = "ABCDE-23456"
    rows = [Row("text", "Go to foreverdungeon.moonshinelabs.io"), Row("text", "and enter:"), Row("big", code),
            Row("text", "Then approve this device.")]
    scr = Screen(("code", code), "Pair device", "", rows, footer="Waiting for approval...")
    pages = paginate(scr, w, h)
    assert len(pages) == 1
    big = [l for l in pages[0] if l.kind == "big"][0]
    assert text_width(code, big.font) <= w
    assert big.font.size >= 18  # legible on the smallest panel


def test_wrap_hard_splits_long_word():
    scr = Screen(("t",), "x", rows=[Row("text", "A" * 300)])
    pages = paginate(scr, 200, 200)
    assert sum(len(l.text) for p in pages for l in p) == 300
