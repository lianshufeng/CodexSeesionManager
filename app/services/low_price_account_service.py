from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
from urllib.parse import quote, urlencode, unquote
from urllib.request import ProxyHandler, Request, build_opener, urlopen


@dataclass(frozen=True, slots=True)
class LowPriceAccount:
    product_id: str
    title: str
    price: str
    sales: str
    credit: str = ""
    reviews: str = ""
    marketplace_years: str = ""
    positive_feedback: str = ""
    negative_feedback: str = ""
    store_sales: str = ""
    href: str = ""


@dataclass(frozen=True, slots=True)
class LowPriceSellerInfo:
    credit: str = ""
    reviews: str = ""
    marketplace_years: str = ""
    positive_feedback: str = ""
    negative_feedback: str = ""
    store_sales: str = ""


@dataclass(frozen=True, slots=True)
class LowPriceProductVariant:
    name: str
    price: str = ""
    stock: str = ""


@dataclass(frozen=True, slots=True)
class LowPriceProductInfo:
    seller_info: LowPriceSellerInfo
    variants: tuple[LowPriceProductVariant, ...] = ()


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.replace("\xa0", " ").split())
        if value:
            self.parts.append(value)


class _LowPriceAccountParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[LowPriceAccount] = []
        self._depth = 0
        self._item_depth = 0
        self._current: dict[str, str] | None = None
        self._capture_key = ""
        self._capture_tag = ""
        self._capture_depth = 0
        self._capture_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        attr_map = {key: value or "" for key, value in attrs}
        classes = attr_map.get("class", "")
        if tag == "li" and self._has_class(classes, "section-list__item"):
            self._current = {}
            self._item_depth = self._depth
            return
        if self._current is None:
            return

        if tag == "a" and attr_map.get("product_id"):
            self._current["product_id"] = attr_map.get("product_id", "")
            self._current["title"] = attr_map.get("title", "")
            self._current["href"] = attr_map.get("href", "")
            return
        if tag == "span" and self._has_class(classes, "title-bold"):
            self._begin_capture("price", tag)
            return
        if (
            tag == "span"
            and self._has_class(classes, "footnote-regular")
            and (self._has_class(classes, "color-text-tertiary") or self._has_class(classes, "card-secondary-text"))
        ):
            self._begin_capture("sales", tag)

    def handle_endtag(self, tag: str) -> None:
        if self._current is not None and self._capture_key and tag == self._capture_tag and self._depth == self._capture_depth:
            value = self._normalize_text("".join(self._capture_parts))
            if self._capture_key != "sales" or value.lower().startswith("sold"):
                if self._capture_key == "sales":
                    value = self._normalize_sales(value)
                self._current[self._capture_key] = value
            self._capture_key = ""
            self._capture_tag = ""
            self._capture_depth = 0
            self._capture_parts = []

        if self._current is not None and tag == "li" and self._depth == self._item_depth:
            product_id = self._current.get("product_id", "")
            title = self._current.get("title", "")
            if product_id and title:
                self.items.append(
                    LowPriceAccount(
                        product_id=product_id,
                        title=title,
                        price=self._current.get("price", ""),
                        sales=self._current.get("sales", ""),
                        href=self._current.get("href", ""),
                    )
                )
            self._current = None
            self._item_depth = 0

        self._depth = max(self._depth - 1, 0)

    def handle_data(self, data: str) -> None:
        if self._capture_key:
            self._capture_parts.append(data)

    def _begin_capture(self, key: str, tag: str) -> None:
        self._capture_key = key
        self._capture_tag = tag
        self._capture_depth = self._depth
        self._capture_parts = []

    def _has_class(self, classes: str, expected: str) -> bool:
        return expected in classes.split()

    def _normalize_text(self, value: str) -> str:
        return " ".join(value.replace("\xa0", " ").split())

    def _normalize_sales(self, value: str) -> str:
        if value.lower().startswith("sold"):
            return value[4:].strip()
        return value


class _LowPriceOptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.options: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture_label = False
        self._label_depth = 0
        self._depth = 0
        self._label_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        attr_map = {key: value or "" for key, value in attrs}
        classes = attr_map.get("class", "").split()
        if tag == "input" and ("cl_selected2_option" in classes or "cl_checked_option" in classes):
            self._current = {
                "option_id": attr_map.get("data-id", ""),
                "value_id": attr_map.get("value", ""),
                "item_id": attr_map.get("data-item-id", ""),
                "disabled": "1" if "disabled" in attr_map else "",
                "price": self._normalize_price_delta(attr_map.get("data-delta-price", "")),
                "stock": "无货" if "disabled" in attr_map else "有货",
            }
            return
        if tag == "label" and self._current is not None:
            self._capture_label = True
            self._label_depth = self._depth
            self._label_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._current is not None and self._capture_label and tag == "label" and self._depth == self._label_depth:
            label = self._normalize_text(" ".join(self._label_parts))
            label = re.sub(r"\+\s*[\d\s.,]+[^\s]*", "", label).strip()
            label = re.sub(r"\bSelected\b", "", label, flags=re.IGNORECASE).strip()
            label = re.sub(r"(out of stock|нет в наличии)", "", label, flags=re.IGNORECASE).strip()
            self._current["name"] = label
            if re.search(r"(out of stock|нет в наличии)", " ".join(self._label_parts), re.IGNORECASE):
                self._current["stock"] = "无货"
            if self._current.get("option_id") and self._current.get("value_id") and label:
                self.options.append(self._current)
            self._current = None
            self._capture_label = False
            self._label_depth = 0
            self._label_parts = []
        self._depth = max(self._depth - 1, 0)

    def handle_data(self, data: str) -> None:
        if self._capture_label:
            self._label_parts.append(data)

    def _normalize_text(self, value: str) -> str:
        return " ".join(value.replace("\xa0", " ").split())

    def _normalize_price_delta(self, value: str) -> str:
        if not value:
            return ""
        digits = "".join(re.findall(r"\d+", value))
        return "" if not digits or int(digits) == 0 else digits


class LowPriceAccountService:
    URL = "https://plati.market/asp/block_goods_category_2.asp"
    COOKIE = (
        "language=en%2DUS; vz=b441d9a3%2D83bf%2D4135%2D92b6%2D83f69e6fe366; "
        "customerid=58dd3bd0f59740e5aa7d736c6f723b07; curr=USD; __ddg1_=bFxxT4IGHcCLSxg6vH7E; "
        "digiseller-currency=USD; __ddgid_=j0SEQJjyvawclzJc; __ddg2_=rz5Do54N0zmmbp58; "
        "email=251708339%40qq%2Ecom; uid=90C5F520%2DAE5B%2D4C08%2DB3FC%2D6B5A94F560AF; "
        "_ga=GA1.1.1917781672.1761410754; _ga_33RMCM93S8=GS2.1.s1776000174$o1$g1$t1776000221$j13$l0$h0; "
        "_ga_698HPYCMKC=GS2.1.s1776584100$o6$g0$t1776584100$j60$l0$h1824466282; "
        "ASPSESSIONIDCAACCCDS=KDPBKHADIGECMDPJBAKEFEJB; ASPSESSIONIDSACQCQSR=DJGPGPADFOIMEDLPMEMFEAID; "
        "ASPSESSIONIDSSRSARST=BHLKMCPCKNKBLMNKOCKHHHBL; ASPSESSIONIDQSDDBRQR=IIFOPFADKPFHDIKNFGIEKOEA; "
        "ASPSESSIONIDAQCAAQSS=NBBIKCADFKFDHOBDKCNBOKIN; __ddg9_=83.229.122.128; lastsrch=chatgpt; "
        "_ym_d=1777216976; _ym_isad=2; _ym_visorc=b; items_list_view=grid; "
        "_ga_2WF69VW4C9=GS2.1.s1777216973$o59$g1$t1777217327$j60$l0$h0; __ddg10_=1777217332; "
        "__ddg8_=sFcnqwjvfhL3P32g"
    )

    def fetch_accounts(self, proxy_url: str = "", page: int = 1, rows: int = 24) -> list[LowPriceAccount]:
        language, currency = self._parse_cookie_settings(self.COOKIE)
        query = urlencode(
            {
                "id_cb": "1267",
                "id_c": "0",
                "sort": "price",
                "page": str(max(page, 1)),
                "rows": str(rows),
                "curr": currency.lower(),
                "lang": language.split("-", 1)[0].lower(),
            }
        )
        request = Request(
            f"{self.URL}?{query}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": language,
                "Cookie": self.COOKIE,
                "X-Currency": currency,
            },
        )
        opener = self._build_opener(proxy_url)
        response_context = opener.open(request, timeout=15) if opener is not None else urlopen(request, timeout=15)
        with response_context as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read().decode(charset, "replace")

        parser = _LowPriceAccountParser()
        parser.feed(html)
        return parser.items

    def fetch_seller_info(self, href: str, proxy_url: str = "") -> LowPriceSellerInfo:
        return self.fetch_product_info(href, proxy_url).seller_info

    def fetch_product_info(self, href: str, proxy_url: str = "") -> LowPriceProductInfo:
        url = self._build_product_url(href)
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Cookie": self.COOKIE,
            },
        )
        opener = self._build_opener(proxy_url)
        response_context = opener.open(request, timeout=15) if opener is not None else urlopen(request, timeout=15)
        with response_context as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read().decode(charset, "replace")
        seller_info = self._parse_seller_info(html)
        variants = tuple(self._fetch_variant_prices(self._parse_product_variants(html), proxy_url))
        return LowPriceProductInfo(seller_info=seller_info, variants=variants)

    def _build_product_url(self, href: str) -> str:
        path = href if href.startswith("/") else f"/{href}"
        separator = "&" if "?" in path else "?"
        return f"https://plati.market{path}{separator}ai=1426781"

    def _parse_seller_info(self, html: str) -> LowPriceSellerInfo:
        match = re.search(
            r'<script\s+type="text/template"\s+id="seller_info_popup_template"\s*>(.*?)</script>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return LowPriceSellerInfo()
        parser = _TextParser()
        parser.feed(match.group(1))
        texts = parser.parts
        rating_values = self._values_after_label(texts, "Rating")
        return LowPriceSellerInfo(
            credit=self._compact_number_text(rating_values[1]) if len(rating_values) > 1 else "",
            reviews=self._normalize_reviews(rating_values[0]) if rating_values else "",
            marketplace_years=self._first_value_after_label(texts, "On the marketplace"),
            positive_feedback=self._compact_number_text(self._first_value_after_label(texts, "Positive feedback")),
            negative_feedback=self._compact_number_text(self._first_value_after_label(texts, "Negative feedback")),
            store_sales=self._compact_number_text(self._first_value_after_label(texts, "Number of sales")),
        )

    def _parse_product_variants(self, html: str) -> list[dict[str, str]]:
        form_match = re.search(
            r'<form\b[^>]*\bname="PriceForm"[^>]*>(.*?)</form>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        parser = _LowPriceOptionParser()
        parser.feed(form_match.group(1) if form_match else html)
        return parser.options

    def _fetch_variant_prices(self, options: list[dict[str, str]], proxy_url: str) -> list[LowPriceProductVariant]:
        selected_options = options[:8]
        prices = {index: "" for index in range(len(selected_options))}
        price_indexes = [index for index, option in enumerate(selected_options) if option.get("stock") != "无货"]
        if price_indexes:
            with ThreadPoolExecutor(max_workers=min(8, len(price_indexes))) as executor:
                futures = {
                    executor.submit(self._fetch_variant_price, selected_options[index], proxy_url): index
                    for index in price_indexes
                }
                for future in as_completed(futures):
                    try:
                        prices[futures[future]] = future.result()
                    except Exception:
                        prices[futures[future]] = ""

        variants: list[LowPriceProductVariant] = []
        for index, option in enumerate(selected_options):
            price = prices[index] or self._format_option_price(option.get("price", ""))
            variants.append(
                LowPriceProductVariant(
                    name=option.get("name", ""),
                    price=price,
                    stock=option.get("stock", ""),
                )
            )
        return variants

    def _fetch_variant_price(self, option: dict[str, str], proxy_url: str) -> str:
        product_id = option.get("item_id", "")
        option_id = option.get("option_id", "")
        value_id = option.get("value_id", "")
        if not product_id or not option_id or not value_id:
            return ""
        xml = f'<response><option O="{option_id}" V="{value_id}"/></response>'
        url = (
            "https://plati.market/asp/price_options.asp"
            f"?p={quote(product_id)}&n=0&c=USD&x={quote(xml, safe='')}"
        )
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/javascript,*/*;q=0.8",
                "Cookie": self.COOKIE,
            },
        )
        opener = self._build_opener(proxy_url)
        response_context = opener.open(request, timeout=5) if opener is not None else urlopen(request, timeout=5)
        with response_context as response:
            charset = response.headers.get_content_charset() or "utf-8"
            payload = response.read().decode(charset, "replace")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        if str(data.get("err", "0")) != "0":
            return ""
        amount = str(data.get("amount") or data.get("price") or "").strip()
        curr = str(data.get("curr") or "USD").strip()
        return f"{amount} {curr}".strip() if amount else ""

    def _format_option_price(self, value: str) -> str:
        if not value:
            return ""
        return f"{value} ₽"

    def _values_after_label(self, texts: list[str], label: str) -> list[str]:
        labels = {
            "Rating",
            "On the marketplace",
            "Positive feedback",
            "Negative feedback",
            "Number of sales",
            "Store page",
        }
        try:
            start = texts.index(label) + 1
        except ValueError:
            return []
        values: list[str] = []
        for value in texts[start:]:
            if value in labels:
                break
            values.append(value)
        return values

    def _first_value_after_label(self, texts: list[str], label: str) -> str:
        values = self._values_after_label(texts, label)
        return values[0] if values else ""

    def _normalize_reviews(self, value: str) -> str:
        return self._compact_number_text(re.sub(r"\s*reviews?\s*$", "", value, flags=re.IGNORECASE).strip())

    def _compact_number_text(self, value: str) -> str:
        if not re.search(r"\d", value):
            return value
        return re.sub(r"\s+", "", value)

    def _build_opener(self, proxy_url: str):
        normalized = self._normalize_proxy_url(proxy_url)
        if not normalized:
            return None
        return build_opener(ProxyHandler({"http": normalized, "https": normalized}))

    def _normalize_proxy_url(self, proxy_url: str) -> str:
        value = proxy_url.strip()
        if not value:
            return ""
        if "://" not in value:
            value = f"http://{value}"
        return value

    def _parse_cookie_settings(self, cookie: str) -> tuple[str, str]:
        values: dict[str, str] = {}
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            values[key] = unquote(value)
        language = values.get("language") or "en-US"
        currency = values.get("digiseller-currency") or values.get("curr") or "USD"
        return language, currency
