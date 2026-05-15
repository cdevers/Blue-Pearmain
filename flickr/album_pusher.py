"""
flickr/album_pusher.py — push Apple Photos album membership to Flickr photosets

Usage (called programmatically, not directly):
    from flickr.album_pusher import push_photo_to_albums
    n = push_photo_to_albums(db, flickr_client, photo_id)
"""

from __future__ import annotations

import logging

log = logging.getLogger("blue-pearmain.album_pusher")


def push_photo_to_albums(db, flickr, photo_id: int) -> int:
    """
    Push a photo to all Flickr photosets corresponding to its Apple Photos albums.
    Creates photosets that don't exist yet. Marks each pair pushed on success.

    Returns the number of photosets successfully updated.
    """
    photo = db.get_photo(photo_id)
    if not photo or not photo.get("flickr_id"):
        return 0

    flickr_id = photo["flickr_id"]

    pending = db.conn.execute(
        """SELECT pa.album_id, a.name AS album_name, a.flickr_set_id
           FROM photo_albums pa
           JOIN albums a ON a.id = pa.album_id
           WHERE pa.photo_id = ? AND pa.flickr_pushed = 0""",
        (photo_id,),
    ).fetchall()

    if not pending:
        return 0

    from flickr.flickr_client import FlickrError, FLICKR_ERR_ALREADY_IN_SET, FLICKR_ERR_NOT_FOUND

    updated = 0
    for row in pending:
        album_id = row["album_id"]
        album_name = row["album_name"]
        flickr_set_id = row["flickr_set_id"]

        try:
            if not flickr_set_id:
                # First photo in this album to be pushed — create the photoset
                flickr_set_id = flickr.create_photoset(album_name, flickr_id)
                db.set_album_flickr_set_id(album_id, flickr_set_id)
                db.set_album_flickr_name(album_id, album_name)
                log.info(
                    "created photoset %r (id=%s) for album %r",
                    album_name,
                    flickr_set_id,
                    album_name,
                )
            else:
                flickr.add_photo_to_photoset(flickr_set_id, flickr_id)

            db.mark_album_pushed(photo_id, album_id)
            updated += 1
            log.debug(
                "added flickr_id=%s to photoset %s (%r)",
                flickr_id,
                flickr_set_id,
                album_name,
            )

        except FlickrError as e:
            if e.code == FLICKR_ERR_ALREADY_IN_SET:
                # Photo is already in the set — desired state achieved; mark done
                db.mark_album_pushed(photo_id, album_id)
                updated += 1
                log.debug(
                    "flickr_id=%s already in photoset %s (%r) — marking pushed",
                    flickr_id,
                    flickr_set_id,
                    album_name,
                )
            elif e.code == FLICKR_ERR_NOT_FOUND:
                # Photo deleted from Flickr — mark done to prevent retries
                db.mark_album_pushed(photo_id, album_id)
                log.warning(
                    "flickr_id=%s not found on Flickr (deleted?) — skipping album push for %r",
                    flickr_id,
                    album_name,
                )
            else:
                log.error(
                    "album push failed photo_id=%s album_id=%s (%r): %s",
                    photo_id,
                    album_id,
                    album_name,
                    e,
                )

    return updated
