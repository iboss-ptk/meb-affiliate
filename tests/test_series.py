import pytest

from mebreco.listings import Listing
from mebreco.series import assign_reps, group_report, is_periodical, volume_of


@pytest.mark.parametrize("title,base,vol", [
    ("เกาะอลวน รักอลเวง 16", "เกาะอลวน รักอลเวง", 16),
    ("คนสวยคนนี้ย้อนมายุค 70 เพื่อคุณสามีเลยนะคะ เล่ม 3", "คนสวยคนนี้ย้อนมายุค 70 เพื่อคุณสามีเลยนะคะ", 3),
    ("ยมลแห่งยมโลก เล่ม 12 (ฉบับการ์ตูน)", "ยมลแห่งยมโลก", 12),
    ("คนโง่ที่ไม่เคยมีรัก เล่ม 1 (ตอน 1-20)", "คนโง่ที่ไม่เคยมีรัก", 1),
    ("ทะลุมิติไปเป็นเจ้าของฟาร์มผู้ร่ำรวยในยุค 70 ภาคพิเศษ เล่ม 3 (4 เล่มจบ)", "ทะลุมิติไปเป็นเจ้าของฟาร์มผู้ร่ำรวยในยุค 70 ภาคพิเศษ", 3),
    ("Kaiju No.8 Vol. 2", "Kaiju No.8", 2),
    ("PATH A WAY Vol.22 (พาธ อะ เวย์ เล่ม 22)", "PATH A WAY", 22),
    ("ถ้ำนรกกินคน เล่ม 03", "ถ้ำนรกกินคน", 3),
    ("OFFICE ANIMALS เล่ม 02: งานด่วนก่อนเลิกงาน!", "OFFICE ANIMALS", 2),
    ("รักสุดวุ่นคุณพ่อบ้านหอหญิง เล่ม 2 (จบ)", "รักสุดวุ่นคุณพ่อบ้านหอหญิง", 2),
    ("Slime เกิดใหม่ทั้งทีก็เป็นสไลม์ไปซะแล้ว 30 (ฉบับการ์ตูน)", "Slime เกิดใหม่ทั้งทีก็เป็นสไลม์ไปซะแล้ว", 30),
    ("วัดป่วนชวนมารัก 12 - Temple", "วัดป่วนชวนมารัก", 12),
    ("เมื่อผมอยากพลิกชะตาเลยศึกษาเวทตั้งแต่ 8 ขวบ เล่ม 2", "เมื่อผมอยากพลิกชะตาเลยศึกษาเวทตั้งแต่ 8 ขวบ", 2),
    ("ทัณฑ์มื้อเช้า เล่ม 9 [e-book only]", "ทัณฑ์มื้อเช้า", 9),
    ("คิริโกะ สังหาร (เล่มเดียวจบ)", "คิริโกะ สังหาร (เล่มเดียวจบ)", None),
    ("หวนคืนมาอ้อนรักคุณทหารมาดขรึม 80s", "หวนคืนมาอ้อนรักคุณทหารมาดขรึม 80s", None),
    ("ย้อนกลับไปยุค 70", "ย้อนกลับไปยุค 70", None),
    ("ไม่ตลก", "ไม่ตลก", None),
])
def test_volume_of(title, base, vol):
    assert volume_of(title) == (base, vol)


def test_periodical():
    assert is_periodical("ขายหัวเราะ ฉบับ 1580") and is_periodical("Bangkok Post ฉบับที่ 12")
    assert not is_periodical("ยมลแห่งยมโลก เล่ม 12 (ฉบับการ์ตูน)")


def L(i, t, a="X", s=False):
    return Listing(i, t, a, None, 148, None, None, s)


ROWS = [L("10", "เกาะอลวน รักอลเวง 16"), L("11", "เกาะอลวน รักอลเวง 3"), L("12", "เกาะอลวน รักอลเวง 1"),
        L("13", "Giant Killing เล่ม 1-62", s=True), L("14", "ขายหัวเราะ ฉบับ 1580"), L("15", "ไม่ตลก")]


def test_assign_reps():
    a = assign_reps(ROWS)
    assert set(a) == {"10", "11", "12", "15"}
    assert a["12"][1] and not a["10"][1] and not a["11"][1] and a["15"][1]
    assert a["10"][0] == a["12"][0] != a["15"][0]


def test_assign_reps_same_title_other_author_is_other_series():
    a = assign_reps([L("1", "Kingdom เล่ม 1", a="Hara"), L("2", "Kingdom เล่ม 1", a="Someone Else")])
    assert a["1"][1] and a["2"][1]


def test_group_report_counts():
    r = group_report(assign_reps(ROWS), ROWS)
    assert r == {"listings": 6, "skipped_sets": 1, "skipped_periodicals": 1, "series": 2, "reps": 2}


def test_group_cli_marks_reps(tmp_path, capsys):
    from mebreco import cli
    from mebreco.store import Store

    s = Store(tmp_path / "db.sqlite")
    s.upsert_listings(ROWS)
    s.commit()
    cli.main(["--db", str(tmp_path / "db.sqlite"), "group"])
    assert Store(tmp_path / "db.sqlite").rep_ids() == ["15", "12"]
    assert '"reps": 2' in capsys.readouterr().out
