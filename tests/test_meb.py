"""MEB parser tests.

The fixture copies the *structure* of a real MEB book page (checked 2026-11-01)
with made-up content, so the repo doesn't hold MEB's copyrighted text.
"""

from mebreco.extract import extract_book

URL = "https://www.mebmarket.com/index.php?action=BookDetails&book_id=123"

PAGE = """<html><head>
<meta property="og:type" content="books.book">
<meta property="og:title" content="นิยายทดสอบ เล่ม 1:: e-book นิยาย โดย นักเขียนหนึ่ง">
<meta property="og:description" content="นิยายทดสอบ เล่ม 1:: e-book นิยาย โดย นักเขียนหนึ่ง">
<meta property="product:price:amount" content="1,099.00">
<meta property="product:price:currency" content="THB">
</head><body>
<div id="book_details_lg">
  <h1 class="book_name">นิยายทดสอบ เล่ม 1</h1>
  โดย <a href="https://www.mebmarket.com/?action=search_book&type=author_name&search=x&exact_keyword=1">นักเขียนหนึ่ง /</a>
  <a href="https://www.mebmarket.com/?action=search_book&type=author_name&search=y&exact_keyword=1">นักเขียนสอง</a>
  <a href="https://www.mebmarket.com/?store=publisher&action=home_page&publisher_id=1">สนพ.ทดสอบ</a>
  <a href="https://www.mebmarket.com/?store=category&action=book_list&category_id=9">แฟนตาซี</a>
  <a href="https://www.mebmarket.com/?store=category&action=home_page&category_id=9">แฟนตาซี</a>
  <a href="https://www.mebmarket.com/?action=series_detail&series_id=5">ชุดทดสอบ</a>
  <div class="display_rating"><div class="rating_text">4.50</div><div class="total_rating_text">1,234 Rating</div></div>
</div>
<img id="image_book_detail_large" src="https://cdn.test/large.gif">
<div class="book_details_listing" id="description_listing">บรรทัดแรก<br>บรรทัดสอง</div>
<div id="wrap_genre_tag"><div class="book_details_listing">
  <a class="genre_tag" href="#">เวทมนตร์</a><a class="genre_tag" href="#">ผจญภัย</a>
</div></div>
</body></html>"""

HOMEPAGE = """<html><head><meta property="og:type" content="website">
<title>meb: e-book ร้านอีบุ๊กอันดับ 1</title></head><body>home</body></html>"""


def test_parse_meb_book():
    b = extract_book(URL, PAGE)
    assert b.source == "meb"
    assert b.book_id == "123"
    assert b.title == "นิยายทดสอบ เล่ม 1"
    assert b.authors == ["นักเขียนหนึ่ง", "นักเขียนสอง"]
    assert b.publisher == "สนพ.ทดสอบ"
    assert b.categories == ["แฟนตาซี"]  # duplicate links collapsed
    assert b.series == "ชุดทดสอบ"
    assert b.tags == ["เวทมนตร์", "ผจญภัย"]
    assert b.group == "นิยาย"
    assert b.description == "บรรทัดแรก\nบรรทัดสอง"
    assert (b.price, b.currency) == (1099.0, "THB")
    assert (b.rating, b.rating_count) == (4.5, 1234)
    assert b.image == "https://cdn.test/large.gif"


def test_missing_meb_book_is_soft_404():
    # MEB answers 200 with its homepage for removed IDs.
    assert extract_book(URL, HOMEPAGE) is None
