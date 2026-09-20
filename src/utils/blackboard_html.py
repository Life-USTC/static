"""HTML parsing for the Blackboard guest-session crawler.

Pure functions only: everything here takes markup and returns records, so the
crawl logic can be tested against recorded fixtures without touching the
network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup, Tag

from ..models.blackboard import (
    CATEGORY_ANSWER,
    CATEGORY_HOMEWORK,
    CATEGORY_LAB,
    CATEGORY_OTHER,
    CATEGORY_REFERENCE,
    CATEGORY_REPORT,
    CATEGORY_SLIDES,
    PAGE_KIND_CONTENT,
    PAGE_KIND_ERROR,
    PAGE_KIND_LOGIN,
    PAGE_KIND_NAVIGATION,
    PAGE_KIND_NOT_FOUND,
)

CONTENT_PATH = "/webapps/blackboard/content/listContent.jsp"
RESOURCE_PATH_PREFIX = "/bbcswebdav/"
LOGIN_PATH_PREFIX = "/webapps/login"

# Blackboard renders "resource not found" instead of a 403 when the guest
# session may not see a course, so the miss has to be recognised from the body.
NOT_FOUND_MARKERS = (
    "找不到资源",
    "resource not found",
    "the requested resource could not be found",
)

_LOGIN_INPUT_NAMES = {"password", "encoded_pw", "encoded_pw_unicode", "user_id"}

_TITLE_SEPARATORS = ("–", "—", " - ")

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CATEGORY_ANSWER, ("answer", "solution", "答案", "参考答案", "key")),
    (
        CATEGORY_HOMEWORK,
        ("homework", "assignment", "exercise", "作业", "习题", "练习"),
    ),
    (CATEGORY_LAB, ("labwork", "lab", "experiment", "实验", "上机")),
    (CATEGORY_REPORT, ("report", "报告")),
    (CATEGORY_SLIDES, ("slide", "lecture", "ppt", "pptx", "课件", "讲义")),
    (CATEGORY_REFERENCE, ("reference", "material", "参考", "资料", "阅读")),
)


@dataclass(frozen=True)
class ContentArea:
    """A course-menu entry pointing at a content listing."""

    content_id: str
    title: str


@dataclass(frozen=True)
class ResourceLink:
    """A `/bbcswebdav/` entry point found on a content page."""

    url: str
    title: str


@dataclass(frozen=True)
class ContentItem:
    """One `li` of a Blackboard content listing."""

    item_id: str | None
    title: str
    kind: str
    description: str = ""
    folder_content_id: str | None = None
    resources: tuple[ResourceLink, ...] = field(default_factory=tuple)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _query_value(url: str, key: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get(key)
    return values[0] if values else None


def content_id_of(url: str) -> str | None:
    """Return the `content_id` query parameter of a listing URL."""

    return _query_value(url, "content_id")


def course_id_of(url: str) -> str | None:
    """Return the `course_id` query parameter of a Blackboard URL."""

    return _query_value(url, "course_id")


def is_login_url(url: str) -> bool:
    """Whether a URL points at the Blackboard login shell."""

    return urlsplit(url).path.startswith(LOGIN_PATH_PREFIX)


def is_login_document(html: str) -> bool:
    """Whether the markup is a login shell rather than course content.

    Matching is done on real `<form>` elements; the localisation bundle that
    every Blackboard page ships mentions "password" in plain JavaScript and
    must not be mistaken for a login prompt.
    """

    for form in _soup(html).find_all("form"):
        if not isinstance(form, Tag):
            continue
        action = str(form.get("action") or "")
        if LOGIN_PATH_PREFIX in action:
            return True
        for input_tag in form.find_all("input"):
            if not isinstance(input_tag, Tag):
                continue
            if str(input_tag.get("type") or "").lower() == "password":
                return True
            if str(input_tag.get("name") or "").lower() in _LOGIN_INPUT_NAMES:
                return True
    return False


def is_not_found_document(html: str) -> bool:
    """Whether Blackboard answered with its "resource not found" placeholder."""

    text = _collapse(_soup(html).get_text(" ")).lower()
    return any(marker in text for marker in NOT_FOUND_MARKERS)


def parse_course_title(html: str) -> str | None:
    """Return the course name from a Blackboard page title."""

    title_tag = _soup(html).title
    if title_tag is None:
        return None
    title = _collapse(title_tag.get_text())
    for separator in _TITLE_SEPARATORS:
        if separator in title:
            return _collapse(title.rsplit(separator, 1)[1]) or None
    return title or None


def parse_page_title(html: str) -> str | None:
    """Return the full `<title>` text of a Blackboard page."""

    title_tag = _soup(html).title
    if title_tag is None:
        return None
    return _collapse(title_tag.get_text()) or None


def parse_course_menu(html: str, *, course_id: str) -> list[ContentArea]:
    """Return the content areas listed in a course menu, in document order."""

    areas: list[ContentArea] = []
    seen: set[str] = set()
    for anchor in _soup(html).find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor["href"])
        if CONTENT_PATH not in href:
            continue
        link_course_id = course_id_of(href)
        if link_course_id is not None and link_course_id != course_id:
            continue
        content_id = content_id_of(href)
        if content_id is None or content_id in seen:
            continue
        seen.add(content_id)
        areas.append(
            ContentArea(content_id=content_id, title=_collapse(anchor.get_text()))
        )
    return areas


def _item_id(list_item: Tag) -> str | None:
    raw = str(list_item.get("id") or "")
    _, _, suffix = raw.partition(":")
    return suffix or None


def _item_title(list_item: Tag) -> str:
    heading = list_item.find("h3")
    if isinstance(heading, Tag):
        # The drag handle is decorative and carries no text in view mode, but
        # strip it anyway so edit-mode captures parse identically.
        for reorder in heading.find_all("span", class_="reorder"):
            reorder.extract()
        title = _collapse(heading.get_text())
        if title:
            return title
    return _collapse(list_item.get_text())[:200]


def _item_description(list_item: Tag) -> str:
    details = list_item.find("div", class_="details")
    if not isinstance(details, Tag):
        return ""
    generated = details.find("div", class_="vtbegenerated")
    if isinstance(generated, Tag):
        return _collapse(generated.get_text(" "))
    return ""


def _item_resources(list_item: Tag) -> tuple[ResourceLink, ...]:
    resources: list[ResourceLink] = []
    seen: set[str] = set()
    for anchor in list_item.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor["href"])
        if not href.startswith(RESOURCE_PATH_PREFIX) or href in seen:
            continue
        seen.add(href)
        resources.append(ResourceLink(url=href, title=_collapse(anchor.get_text())))
    return tuple(resources)


def _item_folder(list_item: Tag, *, course_id: str) -> str | None:
    heading = list_item.find("h3")
    anchors = heading.find_all("a", href=True) if isinstance(heading, Tag) else []
    for anchor in anchors:
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor["href"])
        if CONTENT_PATH not in href:
            continue
        link_course_id = course_id_of(href)
        if link_course_id is not None and link_course_id != course_id:
            continue
        return content_id_of(href)
    return None


def parse_content_items(html: str, *, course_id: str) -> list[ContentItem]:
    """Return the items of a Blackboard content listing, in document order."""

    container = _soup(html).find("ul", id="content_listContainer")
    if not isinstance(container, Tag):
        return []

    items: list[ContentItem] = []
    for list_item in container.find_all("li", recursive=False):
        if not isinstance(list_item, Tag):
            continue
        folder_content_id = _item_folder(list_item, course_id=course_id)
        resources = _item_resources(list_item)
        if folder_content_id is not None:
            kind = "folder"
        elif resources:
            kind = "file"
        else:
            kind = "item"
        items.append(
            ContentItem(
                item_id=_item_id(list_item),
                title=_item_title(list_item),
                kind=kind,
                description=_item_description(list_item),
                folder_content_id=folder_content_id,
                resources=resources,
            )
        )
    return items


def classify_page(*, status: int, final_url: str, html: str, item_count: int) -> str:
    """Classify a fetched page so noise stays out of the content index."""

    if status in {401, 403}:
        return PAGE_KIND_LOGIN
    if is_login_url(final_url) or is_login_document(html):
        return PAGE_KIND_LOGIN
    if is_not_found_document(html):
        return PAGE_KIND_NOT_FOUND
    if status >= 400 or status == 0:
        return PAGE_KIND_ERROR
    if item_count > 0:
        return PAGE_KIND_CONTENT
    return PAGE_KIND_NAVIGATION


def classify_resource(*texts: str | None) -> str:
    """Categorise a resource from its item title, link text and filename."""

    haystack = " ".join(text.lower() for text in texts if text)
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return category
    return CATEGORY_OTHER


def filename_from_url(url: str) -> str | None:
    """Return the served filename of a resolved `/bbcswebdav/` URL."""

    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return name or None
