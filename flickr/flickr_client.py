"""
flickr_client.py — thin Flickr API wrapper for flickr-curator

Wraps OAuth 1.0a calls via requests-oauthlib. All methods return
plain Python dicts (parsed JSON). Raises FlickrError on API errors.

Usage:
    from flickr.flickr_client import FlickrClient
    client = FlickrClient.from_config(config)
    photos = client.get_recent_private(min_upload_date=ts)
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from requests_oauthlib import OAuth1Session
import requests

REST_URL = "https://api.flickr.com/services/rest/"

log = logging.getLogger("blue-pearmain.flickr")

# HTTP codes that are transient and worth retrying.
# 429 (rate limit) is explicitly here — do not move it to _PERMANENT_HTTP_CODES.
_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

# HTTP codes that are permanent client errors — raise immediately, never retry.
# Note: 429 is intentionally absent; it belongs in _TRANSIENT_HTTP_CODES above.
_PERMANENT_HTTP_CODES = {400, 401, 403, 404, 405, 410}

# Flickr application-level error codes that are transient
_TRANSIENT_FLICKR_CODES = {
    0,  # generic "something went wrong" — often transient
    106,  # service unavailable
}

# Flickr error codes are method-scoped: the same integer can appear in multiple
# constants below if different API methods use that code for different meanings.
# Always use the named constant, not the bare integer, so the context is clear.
FLICKR_ERR_NOT_FOUND = 1  # Photo not found (e.g. manually deleted on Flickr)
FLICKR_ERR_MAX_TAGS = 2  # Maximum number of tags reached (75 tag limit)
FLICKR_ERR_ALREADY_IN_SET = 3  # Photo already in photoset — idempotent success
FLICKR_ERR_PHOTO_NOT_IN_SET = 2  # photosets.removePhoto: photo not in the set


class FlickrError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.transient = code in _TRANSIENT_FLICKR_CODES
        super().__init__(f"Flickr API error {code}: {message}")


class FlickrClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        oauth_token: str,
        oauth_token_secret: str,
        user_nsid: str = "",
        rate_limit_delay: float = 0.5,
    ):
        self.api_key = api_key
        self.user_nsid = user_nsid
        self._rate_delay = rate_limit_delay
        self._session = OAuth1Session(
            api_key,
            client_secret=api_secret,
            resource_owner_key=oauth_token,
            resource_owner_secret=oauth_token_secret,
        )

    @classmethod
    def from_config(cls, config: dict) -> "FlickrClient":
        f = config["flickr"]
        return cls(
            api_key=f["api_key"],
            api_secret=f["api_secret"],
            oauth_token=f["oauth_token"],
            oauth_token_secret=f["oauth_token_secret"],
            user_nsid=f.get("user_nsid", ""),
        )

    # -----------------------------------------------------------------------
    # Core request
    # -----------------------------------------------------------------------

    def _call(
        self,
        method: str,
        params: dict | None = None,
        http_method: str = "GET",
        max_retries: int = 4,
        _attempt: int = 0,
    ) -> dict:
        """
        Make a signed Flickr API call with exponential backoff on transient errors.
        Returns the parsed JSON response body. Raises FlickrError on persistent failure.

        Retry policy by error type:
          HTTP 429: 8 retries, backoff capped at 60s (~3 min total).
                    Honors Retry-After header when present (clamped to 0–120s).
          Other transient errors (timeout, 5xx): max_retries attempts (default 4),
                    backoff capped at 8s (~15s total).
          Permanent errors (4xx, non-transient Flickr codes): raise immediately.
        """
        p = {
            "method": method,
            "format": "json",
            "nojsoncallback": 1,
        }
        if params:
            p.update(params)

        time.sleep(self._rate_delay)

        try:
            if http_method == "POST":
                resp = self._session.post(REST_URL, data=p, timeout=30)
            else:
                resp = self._session.get(REST_URL, params=p, timeout=30)
        except requests.Timeout:
            return self._retry(method, params, http_method, max_retries, _attempt, reason="timeout")
        except requests.ConnectionError:
            return self._retry(
                method, params, http_method, max_retries, _attempt, reason="connection error"
            )

        # Permanent client errors — raise immediately, no retry.
        # Wrap in FlickrError so callers only ever see one exception type.
        if resp.status_code in _PERMANENT_HTTP_CODES:
            raise FlickrError(
                resp.status_code,
                getattr(resp, "reason", None) or f"HTTP {resp.status_code}",
            )

        # Transient server errors — retry with backoff
        if resp.status_code in _TRANSIENT_HTTP_CODES:
            if resp.status_code == 429:
                # Honor Retry-After if present and valid; fall through to exponential on bad values
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                        delay = max(0.0, min(delay, 120.0))  # clamp: negative→0, absurd→2 min cap
                        time.sleep(delay)
                    except ValueError:
                        pass  # non-numeric header — exponential backoff will run via _retry
            return self._retry(
                method,
                params,
                http_method,
                max_retries,
                _attempt,
                reason=f"HTTP {resp.status_code}",
            )

        resp.raise_for_status()
        data = resp.json()

        if data.get("stat") != "ok":
            err = data.get("message", "unknown error")
            code = data.get("code", -1)
            err_obj = FlickrError(code, err)
            if err_obj.transient:
                return self._retry(
                    method,
                    params,
                    http_method,
                    max_retries,
                    _attempt,
                    reason=f"Flickr error {code}",
                )
            raise err_obj

        return data

    def _retry(
        self,
        method: str,
        params: dict | None,
        http_method: str,
        max_retries: int,
        attempt: int,
        reason: str,
    ) -> dict:
        """Sleep with exponential backoff and retry, or raise if exhausted.

        Policy by error type:
          HTTP 429 (rate limit): 8 retries, 60s backoff ceiling.
              Outlasts Flickr's ~1-minute rate-limit window before giving up.
          All other transient errors (timeout, 500, 502, etc.): caller's max_retries
              (default 4), 8s backoff ceiling. Network hiccups typically recover quickly.
        """
        photo_id = (params or {}).get("photo_id", "")
        context = f" photo_id={photo_id}" if photo_id else ""

        if "429" in reason:
            effective_max_retries = 8
            backoff_cap = 60
        else:
            effective_max_retries = max_retries  # caller's value, default 4
            backoff_cap = 8

        if attempt >= effective_max_retries:
            log.error(
                f"Flickr {method}{context} failed after {effective_max_retries} retries ({reason})"
            )
            raise FlickrError(
                -1, f"Flickr call failed after {effective_max_retries} retries ({reason})"
            )

        delay = min(2**attempt, backoff_cap) + random.uniform(0, 0.5)
        log.warning(
            f"Flickr {method}{context} failed ({reason}), "
            f"retry {attempt + 1}/{effective_max_retries} in {delay:.1f}s"
        )
        time.sleep(delay)
        return self._call(method, params, http_method, max_retries, attempt + 1)

    # -----------------------------------------------------------------------
    # Photo polling
    # -----------------------------------------------------------------------

    def get_recent_uploads(
        self,
        min_upload_date: int,
        privacy_filter: int | None = None,
        page: int = 1,
        per_page: int = 500,
        extras: str | None = None,
    ) -> dict:
        """
        Call flickr.photos.recentlyUpdated to get photos uploaded/changed
        since min_upload_date (Unix timestamp).

        privacy_filter: 1=public, 2=friends, 3=family, 4=friends+family, 5=private
        extras: comma-separated list of extra fields to return
        """
        default_extras = (
            "date_upload,date_taken,geo,tags,machine_tags,"
            "url_sq,url_t,url_s,url_m,url_l,url_o,"
            "original_format,media,description,license"
        )
        return self._call(
            "flickr.photos.recentlyUpdated",
            {
                "min_date": min_upload_date,
                "privacy_filter": privacy_filter or "",
                "page": page,
                "per_page": per_page,
                "extras": extras or default_extras,
            },
        )

    def get_not_in_set(
        self,
        privacy_filter: int = 5,
        page: int = 1,
        per_page: int = 500,
        min_upload_date: int | None = None,
    ) -> dict:
        """
        Get photos not in any album. Useful for finding unsorted uploads.
        privacy_filter=5 returns only private photos.
        """
        params: dict[str, Any] = {
            "privacy_filter": privacy_filter,
            "page": page,
            "per_page": per_page,
            "extras": "date_upload,date_taken,geo,tags,url_m,url_l,description",
        }
        if min_upload_date:
            params["min_upload_date"] = min_upload_date
        return self._call("flickr.photos.getNotInSet", params)

    def get_photo_info(self, photo_id: str, secret: str | None = None) -> dict:
        """Get full metadata for a single photo."""
        params: dict[str, Any] = {"photo_id": photo_id}
        if secret:
            params["secret"] = secret
        return self._call("flickr.photos.getInfo", params)

    def get_photo_sizes(self, photo_id: str) -> dict:
        """Get available size URLs for a photo."""
        return self._call("flickr.photos.getSizes", {"photo_id": photo_id})

    def search_photos(
        self,
        user_id: str = "me",
        min_upload_date: int | None = None,
        max_upload_date: int | None = None,
        privacy_filter: int | None = None,
        page: int = 1,
        per_page: int = 500,
        extras: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "user_id": user_id,
            "page": page,
            "per_page": per_page,
            "extras": extras or "date_upload,date_taken,geo,tags,url_m,url_l,description",
        }
        if min_upload_date:
            params["min_upload_date"] = min_upload_date
        if max_upload_date:
            params["max_upload_date"] = max_upload_date
        if privacy_filter:
            params["privacy_filter"] = privacy_filter
        return self._call("flickr.photos.search", params)

    # -----------------------------------------------------------------------
    # Writing back to Flickr
    # -----------------------------------------------------------------------

    def set_permissions(
        self,
        photo_id: str,
        is_public: int,
        is_friend: int = 0,
        is_family: int = 0,
        perm_comment: int = 3,
        perm_addmeta: int = 2,
    ) -> dict:
        """
        Set photo visibility.
        is_public: 1 = public, 0 = private
        perm_comment/perm_addmeta: 0=nobody,1=friends+family,2=contacts,3=everybody
        """
        return self._call(
            "flickr.photos.setPerms",
            {
                "photo_id": photo_id,
                "is_public": is_public,
                "is_friend": is_friend,
                "is_family": is_family,
                "perm_comment": perm_comment,
                "perm_addmeta": perm_addmeta,
            },
            http_method="POST",
        )

    def add_tags(self, photo_id: str, tags: list[str]) -> dict:
        """Add tags to a photo (does not remove existing tags)."""
        # Flickr expects space-separated tags; multi-word tags must be quoted
        tag_str = " ".join(f'"{t}"' if " " in t else t for t in tags if t.strip())
        return self._call(
            "flickr.photos.addTags",
            {"photo_id": photo_id, "tags": tag_str},
            http_method="POST",
        )

    def remove_tag(self, tag_id: str) -> None:
        """Remove a single tag by its Flickr tag instance ID.

        The tag_id comes from the 'id' attribute of a <tag> element in
        flickr.photos.getInfo. Each tag instance has a unique ID.
        Does NOT take photo_id — the tag_id identifies the instance globally.
        """
        self._call(
            "flickr.photos.removeTag",
            {"tag_id": tag_id},
            http_method="POST",
        )

    def set_tags(self, photo_id: str, tags: list[str]) -> dict:
        """Replace all tags on a photo (destructive — use add_tags to append)."""
        tag_str = " ".join(f'"{t}"' if " " in t else t for t in tags if t.strip())
        return self._call(
            "flickr.photos.setTags",
            {"photo_id": photo_id, "tags": tag_str},
            http_method="POST",
        )

    def set_meta(self, photo_id: str, title: str = "", description: str = "") -> dict:
        """Set title and/or description on a photo."""
        return self._call(
            "flickr.photos.setMeta",
            {"photo_id": photo_id, "title": title, "description": description},
            http_method="POST",
        )

    def set_location(self, photo_id: str, lat: float, lon: float) -> None:
        """Set the geotag on a Flickr photo via flickr.photos.geo.setLocation.

        Uses accuracy=16 (street level). Raises FlickrError on failure.
        """
        self._call(
            "flickr.photos.geo.setLocation",
            {"photo_id": photo_id, "lat": lat, "lon": lon, "accuracy": 16},
            http_method="POST",
        )

    def rotate(self, photo_id: str, degrees: int) -> dict:
        """Rotate a photo on Flickr clockwise by 90, 180, or 270 degrees.
        DESTRUCTIVE and irreversible — re-encodes the stored image."""
        if degrees not in (90, 180, 270):
            raise ValueError(f"degrees must be 90, 180, or 270; got {degrees}")
        return self._call(
            "flickr.photos.transform.rotate",
            {"photo_id": photo_id, "degrees": degrees},
            http_method="POST",
        )

    def delete_photo(self, photo_id: str) -> None:
        """Permanently delete a Flickr photo. Raises FlickrError on failure."""
        self._call(
            "flickr.photos.delete",
            {"photo_id": photo_id},
            http_method="POST",
        )

    def get_photosets(self) -> list[dict]:
        """Return all photosets for the authenticated user."""
        data = self._call(
            "flickr.photosets.getList",
            {"user_id": self.user_nsid or "me"},
        )
        return data.get("photosets", {}).get("photoset", [])

    def get_photosets_titled(self) -> dict[str, str]:
        """Return {photoset_id: title} for all the user's photosets."""
        result = {}
        for ps in self.get_photosets():
            title = ps.get("title", {})
            if isinstance(title, dict):
                title = title.get("_content", "")
            result[ps["id"]] = title
        return result

    def create_photoset(self, title: str, primary_photo_id: str) -> str:
        """Create a Flickr photoset. Returns the new photoset ID."""
        data = self._call(
            "flickr.photosets.create",
            {"title": title, "primary_photo_id": primary_photo_id},
            http_method="POST",
        )
        return data["photoset"]["id"]

    def add_photo_to_photoset(self, photoset_id: str, photo_id: str) -> None:
        """Add a photo to an existing Flickr photoset."""
        self._call(
            "flickr.photosets.addPhoto",
            {"photoset_id": photoset_id, "photo_id": photo_id},
            http_method="POST",
        )

    def edit_photoset_meta(self, photoset_id: str, title: str) -> None:
        """Update the title of an existing Flickr photoset."""
        self._call(
            "flickr.photosets.editMeta",
            {"photoset_id": photoset_id, "title": title},
            http_method="POST",
        )

    def remove_photo_from_photoset(self, photoset_id: str, photo_id: str) -> None:
        """Remove a photo from a Flickr photoset."""
        self._call(
            "flickr.photosets.removePhoto",
            {"photoset_id": photoset_id, "photo_id": photo_id},
            http_method="POST",
        )

    def list_photosets(self) -> list[dict[str, str | int]]:
        """Return all photosets for the authenticated user.

        Each entry: {"id": str, "title": str, "photos": int, "videos": int}.
        """
        result: list[dict[str, str | int]] = []
        page = 1
        while True:
            data = self._call(
                "flickr.photosets.getList",
                {"per_page": 500, "page": page, "primary_photo_extras": ""},
            )
            batch = data["photosets"]["photoset"]
            for s in batch:
                result.append(
                    {
                        "id": s["id"],
                        "title": s["title"]["_content"],
                        "photos": int(s["photos"]),
                        "videos": int(s.get("videos", 0)),
                    }
                )
            if len(batch) < 500:
                break
            page += 1
        return result

    def get_photoset_photos(
        self, photoset_id: str, extras: str = "date_taken"
    ) -> list[dict[str, str]]:
        """Return all photos in a photoset (paginated). Each entry has at least 'id'."""
        result: list[dict[str, str]] = []
        page = 1
        while True:
            data = self._call(
                "flickr.photosets.getPhotos",
                {"photoset_id": photoset_id, "extras": extras, "per_page": 500, "page": page},
            )
            batch = data["photoset"]["photo"]
            result.extend(batch)
            if len(batch) < 500:
                break
            page += 1
        return result

    def delete_photoset(self, photoset_id: str) -> None:
        """Delete a Flickr photoset. The photos themselves remain on Flickr."""
        self._call(
            "flickr.photosets.delete",
            {"photoset_id": photoset_id},
            http_method="POST",
        )

    # -----------------------------------------------------------------------
    # Collections (Flickr Pro only)
    # -----------------------------------------------------------------------

    def create_collection(self, title: str, description: str = "") -> str:
        """Create a Flickr Collection. Returns the collection_id string."""
        data = self._call(
            "flickr.collections.create",
            {"title": title, "description": description},
            http_method="POST",
        )
        return data["collection"]["id"]

    def edit_collection_sets(
        self,
        collection_id: str,
        photoset_ids: list[str],
        sub_collection_ids: list[str],
    ) -> None:
        """Full replace of a collection's photosets and sub-collections.
        Flickr's editSets is a complete overwrite, not additive."""
        self._call(
            "flickr.collections.editSets",
            {
                "collection_id": collection_id,
                "photoset_ids": " ".join(photoset_ids),
                "collection_ids": " ".join(sub_collection_ids),
            },
            http_method="POST",
        )

    def delete_collection(self, collection_id: str) -> None:
        """Delete a Flickr Collection."""
        self._call(
            "flickr.collections.delete",
            {"collection_id": collection_id},
            http_method="POST",
        )

    def edit_collection_meta(self, collection_id: str, title: str) -> None:
        """Update the title of an existing Flickr Collection."""
        self._call(
            "flickr.collections.editMeta",
            {"collection_id": collection_id, "title": title},
            http_method="POST",
        )

    def get_collections_flat(self) -> dict[str, str]:
        """Return {collection_id: title} by flattening flickr.collections.getTree.
        Raises FlickrError on non-Pro accounts."""
        data = self._call("flickr.collections.getTree")

        result: dict[str, str] = {}

        def _walk(nodes: list[dict]) -> None:
            for node in nodes:
                result[node["id"]] = node.get("title", "")
                _walk(node.get("collection", []))

        _walk(data.get("collections", {}).get("collection", []))
        return result

    # -----------------------------------------------------------------------
    # Thumbnail download
    # -----------------------------------------------------------------------

    def download_thumbnail(self, url: str, dest_path: str) -> bool:
        """
        Download a Flickr thumbnail URL to dest_path.
        Returns True on success. Uses a plain (non-OAuth) GET since
        Flickr static URLs are publicly accessible once you have the URL.
        """
        import requests
        from pathlib import Path

        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Account info
    # -----------------------------------------------------------------------

    def test_login(self) -> dict:
        """Verify authentication and return user info."""
        return self._call("flickr.test.login")

    def get_user_info(self, user_id: str = "me") -> dict:
        return self._call("flickr.people.getInfo", {"user_id": user_id})


# ---------------------------------------------------------------------------
# Privacy-state → Flickr permission flags
# ---------------------------------------------------------------------------

_STATE_PERMS: dict[str, tuple[int, int, int]] = {
    "approved_public": (1, 0, 0),
    "already_public": (1, 0, 0),
    "approved_friends": (0, 1, 0),
    "approved_family": (0, 0, 1),
    "approved_friends_family": (0, 1, 1),
}


def state_to_perms(privacy_state: str) -> tuple[int, int, int]:
    """Return (is_public, is_friend, is_family) for a DB privacy_state value.

    Unknown / private states return (0, 0, 0).
    """
    return _STATE_PERMS.get(privacy_state, (0, 0, 0))
