"""Records produced by the Blackboard anonymous guest-session crawler.

Every row carries `access_mode`, which is always `guest_session`: the crawler
never submits credentials. Anything the guest session is not allowed to read is
recorded with `access_state = "auth_required"` and left untouched.
"""

from pydantic import BaseModel

ACCESS_MODE_GUEST = "guest_session"

# Page kinds. Only `content` pages carry course material worth indexing;
# the others are the login shell, empty navigation scaffolding, or misses.
PAGE_KIND_CONTENT = "content"
PAGE_KIND_NAVIGATION = "navigation"
PAGE_KIND_LOGIN = "login"
PAGE_KIND_NOT_FOUND = "not_found"
PAGE_KIND_ERROR = "error"

# Access states shared by pages and resources.
ACCESS_PUBLIC = "public"
ACCESS_AUTH_REQUIRED = "auth_required"
ACCESS_NOT_FOUND = "not_found"
ACCESS_ERROR = "error"
ACCESS_TOO_LARGE = "too_large"

# Resource categories, derived from the item title and the served filename.
CATEGORY_HOMEWORK = "homework"
CATEGORY_LAB = "lab"
CATEGORY_ANSWER = "answer"
CATEGORY_REPORT = "report"
CATEGORY_SLIDES = "slides"
CATEGORY_REFERENCE = "reference"
CATEGORY_OTHER = "other"


class BlackboardPage(BaseModel):
    """One Blackboard page the guest session requested."""

    course_id: str
    course_title: str | None = None
    content_id: str | None = None
    title: str | None = None
    requested_url: str
    final_url: str
    status: int
    page_kind: str
    access_mode: str = ACCESS_MODE_GUEST
    access_state: str
    item_count: int = 0
    indexed: bool = False
    exclusion_reason: str | None = None
    body_sha256: str | None = None
    duplicate_of: str | None = None
    source_page: str | None = None
    depth: int = 0
    fetched_at: str


class BlackboardResource(BaseModel):
    """One downloadable course resource reachable from a content page."""

    course_id: str
    course_title: str | None = None
    content_id: str | None = None
    item_id: str | None = None
    title: str | None = None
    category: str = CATEGORY_OTHER
    requested_url: str
    final_url: str | None = None
    status: int
    content_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    local_path: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    access_mode: str = ACCESS_MODE_GUEST
    access_state: str
    not_modified: bool = False
    source_page: str
    fetched_at: str
