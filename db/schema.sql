-- flickr-curator schema
-- SQLite database, lives on NAS, mounted by Mac poller via SMB

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ============================================================
-- Core photo record
-- One row per photo. A photo may exist in Photos only, Flickr
-- only, or both. The uuid/flickr_id pair tracks the match state.
-- ============================================================

CREATE TABLE IF NOT EXISTS photos (
    -- Identity
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid                    TEXT UNIQUE,            -- Apple Photos UUID (null if Flickr-only)
    flickr_id               TEXT UNIQUE,            -- Flickr photo ID (null if not yet matched/uploaded)
    flickr_secret           TEXT,                   -- needed to construct Flickr thumbnail URLs
    flickr_server           TEXT,                   -- needed for Flickr static URLs
    flickr_farm             INTEGER,                -- needed for legacy Flickr static URLs

    -- File identity (used for Photos<->Flickr matching)
    original_filename       TEXT,                   -- e.g. DSC_2802.JPG or IMG_7507.HEIC
    fingerprint             TEXT,                   -- osxphotos fingerprint / cloud_guid
    camera_make             TEXT,
    camera_model            TEXT,
    lens_model              TEXT,
    width                   INTEGER,                -- pixel width of original file
    height                  INTEGER,                -- pixel height of original file

    -- Timestamps
    date_taken              TEXT,                   -- ISO8601, from EXIF
    date_uploaded_flickr    TEXT,                   -- ISO8601, when Flickr received it
    date_added_photos       TEXT,                   -- ISO8601, when Photos ingested it
    date_analyzed           TEXT,                   -- ISO8601, when Apple completed ML analysis
    date_synced             TEXT,                   -- ISO8601, last time we synced this record

    -- Location
    latitude                REAL,
    longitude               REAL,
    place_city              TEXT,
    place_state             TEXT,
    place_country           TEXT,
    place_country_code      TEXT,
    place_address           TEXT,                   -- full formatted address string
    place_neighborhood      TEXT,                   -- sub-locality if available
    place_ishome            INTEGER DEFAULT 0,      -- Apple's own home flag (boolean)
    geofence_zone           TEXT,                   -- matched zone name from config, if any

    -- Apple ML outputs
    apple_ai_caption        TEXT,                   -- media_analysis.image_caption.imageCaptionText
    apple_ai_caption_conf   REAL,                   -- imageCaptionConfidence
    apple_labels            TEXT,                   -- JSON array e.g. ["People","Restaurant","Table"]
    apple_persons           TEXT,                   -- JSON array of named persons e.g. ["Chris Devers","_UNKNOWN_"]
    apple_unknown_faces     INTEGER DEFAULT 0,      -- count of _UNKNOWN_ entries in persons
    apple_named_faces       INTEGER DEFAULT 0,      -- count of named (non-_UNKNOWN_) entries
    apple_human_count       INTEGER DEFAULT 0,      -- count of humans[] entries in media_analysis
    apple_aesthetic_score   REAL,                   -- score.overall

    -- Privacy state machine
    -- Possible values:
    --   'auto_private'              : geofence or home flag triggered, skip review queue
    --   'needs_review'              : has people signals, awaiting human decision
    --   'candidate_public'          : no people signals, tags proposed, awaiting confirmation
    --   'approved_public'           : human said yes, push to Flickr as public
    --   'keep_private'              : human said no
    --   'already_public'            : was already public on Flickr before this tool existed
    --   'skipped'                   : human deferred decision
    --   'approved_friends'          : push to Flickr as Friends-only
    --   'approved_family'           : push to Flickr as Family-only
    --   'approved_friends_family'   : push to Flickr as Friends & Family
    privacy_state           TEXT NOT NULL DEFAULT 'needs_review'
                                CHECK(privacy_state IN (
                                    'auto_private', 'needs_review', 'candidate_public',
                                    'approved_public', 'keep_private', 'already_public',
                                    'skipped', 'duplicate_flickr',
                                    'approved_friends', 'approved_family', 'approved_friends_family'
                                )),
    privacy_reason          TEXT,                   -- human-readable explanation of how state was set

    -- Proposed tags (staged, not yet pushed)
    proposed_tags           TEXT,                   -- JSON array of tag strings
    pushed_tags             TEXT,                   -- JSON array; cumulative tags confirmed pushed to Flickr (write ledger)
    proposed_description    TEXT,                   -- draft description text (may be AI caption, edited)

    -- Push state
    tags_pushed_flickr      INTEGER DEFAULT 0,      -- boolean
    tags_pushed_photos      INTEGER DEFAULT 0,      -- boolean
    perms_pushed_flickr     INTEGER DEFAULT 0,      -- boolean: have we set Flickr visibility?
    flickr_deleted          INTEGER DEFAULT 0,      -- boolean: photo was deleted from Flickr; skip future syncs

    -- Metadata cache: last known state from each side
    flickr_title            TEXT,                   -- last title fetched from Flickr
    flickr_description      TEXT,                   -- last description fetched from Flickr
    flickr_tags             TEXT,                   -- JSON array — last tags fetched from Flickr (original casing)
    flickr_tags_hash        TEXT,                   -- SHA-256 of sorted normalised Flickr tag set
    flickr_last_updated     TEXT,                   -- ISO8601 — Flickr's lastupdate timestamp for this photo
    photos_title            TEXT,                   -- last title read from Apple Photos
    photos_description      TEXT,                   -- last description read from Apple Photos
    photos_tags             TEXT,                   -- JSON array — last keywords from Apple Photos (original casing)
    photos_tags_hash        TEXT,                   -- SHA-256 of sorted normalised Photos tag set
    meta_synced_flickr_at   TEXT,                   -- ISO8601 — when we last fetched from Flickr
    meta_synced_photos_at   TEXT,                   -- ISO8601 — when we last read from Photos
    meta_last_harmonized_at TEXT,                   -- ISO8601 — when sync engine last processed this photo
    tags_truncated_for_flickr INTEGER DEFAULT 0,    -- boolean: canonical tags exceeded 75 on last Flickr push

    -- Thumbnail cache
    thumbnail_path          TEXT,                   -- absolute path on NAS to cached url_l JPEG

    -- Display corrections (applied as CSS transform; does not affect stored file)
    display_rotation        INTEGER NOT NULL DEFAULT 0,  -- cumulative CW degrees applied via rotate-flickr

    -- Review tracking
    reviewed_at             TEXT,                   -- ISO8601
    review_decision         TEXT,                   -- 'make_public' | 'keep_private' | 'skip'
    review_notes            TEXT,                   -- optional freeform notes
    uuid_stale              INTEGER NOT NULL DEFAULT 0, -- 1 if Photos.app rejected UUID as invalid
    is_screenshot           INTEGER NOT NULL DEFAULT 0, -- 1 if osxphotos flagged this as a screenshot
    merged_into_id          INTEGER REFERENCES photos(id), -- soft-delete: points to record this donor was merged into
    updated_at              TEXT                    -- ISO8601, last time this row was written
);


-- ============================================================
-- Geofence zones (editable via settings UI)
-- ============================================================

CREATE TABLE IF NOT EXISTS geofence_zones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,   -- e.g. "home", "school", "work"
    label       TEXT,                   -- display label
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    radius_m    REAL NOT NULL,          -- radius in metres
    policy      TEXT NOT NULL DEFAULT 'auto_private',
                                        -- 'auto_private' | 'flag_review' | 'auto_public'
    active      INTEGER DEFAULT 1,      -- boolean: is this zone enabled?
    created_at  TEXT,
    notes       TEXT
);


-- ============================================================
-- Sync log: records each poll/sync run for auditing & resuming
-- ============================================================

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    source          TEXT NOT NULL,      -- 'flickr_poll' | 'photos_scan' | 'manual'
    status          TEXT,               -- 'running' | 'complete' | 'error'
    photos_seen     INTEGER DEFAULT 0,
    photos_new      INTEGER DEFAULT 0,
    photos_updated  INTEGER DEFAULT 0,
    error_message   TEXT
);


-- ============================================================
-- Tag history: audit trail of tag pushes
-- ============================================================

CREATE TABLE IF NOT EXISTS tag_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id    INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    event_at    TEXT NOT NULL,
    destination TEXT NOT NULL,          -- 'flickr' | 'photos'
    tags_before TEXT,                   -- JSON array
    tags_after  TEXT,                   -- JSON array
    success     INTEGER DEFAULT 1,
    error       TEXT
);


-- ============================================================
-- Schema migrations: tracks which migrations have been applied
-- ============================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,  -- e.g. "migrate_001_privacy_state_check"
    applied_at  TEXT NOT NULL          -- ISO8601
);


-- ============================================================
-- Indexes
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_photos_uuid          ON photos(uuid);
CREATE INDEX IF NOT EXISTS idx_photos_flickr_id     ON photos(flickr_id);
CREATE INDEX IF NOT EXISTS idx_photos_privacy_state ON photos(privacy_state);
CREATE INDEX IF NOT EXISTS idx_photos_date_taken    ON photos(date_taken);
CREATE INDEX IF NOT EXISTS idx_photos_review_queue  ON photos(privacy_state, date_taken DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_photos_location      ON photos(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_photos_reviewed      ON photos(reviewed_at);
CREATE INDEX IF NOT EXISTS idx_tag_events_photo     ON tag_events(photo_id);
CREATE INDEX IF NOT EXISTS idx_photos_push_state    ON photos(privacy_state, perms_pushed_flickr);
CREATE INDEX IF NOT EXISTS idx_photos_tags_pushed        ON photos(tags_pushed_flickr);
CREATE INDEX IF NOT EXISTS idx_photos_updated            ON photos(updated_at);
CREATE INDEX IF NOT EXISTS idx_photos_flickr_tags_hash   ON photos(flickr_tags_hash);
CREATE INDEX IF NOT EXISTS idx_photos_photos_tags_hash   ON photos(photos_tags_hash);
CREATE INDEX IF NOT EXISTS idx_photos_meta_harmonized    ON photos(meta_last_harmonized_at);


-- ============================================================
-- Folders: Apple Photos folder hierarchy mirrored as Flickr Collections
-- ============================================================

CREATE TABLE IF NOT EXISTS folders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    apple_uuid           TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    parent_id            INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    flickr_collection_id TEXT,
    flickr_name          TEXT,                   -- last name pushed to Flickr Collection title
    created_at           TEXT,
    updated_at           TEXT
);


-- ============================================================
-- Albums: Apple Photos album membership mirrored as Flickr photosets
-- ============================================================

CREATE TABLE IF NOT EXISTS albums (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    apple_uuid      TEXT NOT NULL UNIQUE,   -- Photos album UUID
    name            TEXT NOT NULL,
    folder_id       INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    flickr_set_id   TEXT,                   -- NULL until created on Flickr
    flickr_set_url  TEXT,
    flickr_name     TEXT,                   -- last name pushed to Flickr photoset title
    created_at      TEXT,
    updated_at      TEXT,
    deleted_at      TEXT                    -- ISO8601; set when album is removed from Photos
);

-- Per-photo album membership
CREATE TABLE IF NOT EXISTS photo_albums (
    photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    album_id        INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    flickr_pushed   INTEGER DEFAULT 0,      -- boolean: added to Flickr photoset?
    pushed_at       TEXT,
    removed_at      TEXT,                   -- ISO8601; set when photo is removed from album in Photos
    PRIMARY KEY (photo_id, album_id)
);

CREATE INDEX IF NOT EXISTS idx_photo_albums_photo   ON photo_albums(photo_id);
CREATE INDEX IF NOT EXISTS idx_photo_albums_album   ON photo_albums(album_id);
CREATE INDEX IF NOT EXISTS idx_photo_albums_pending ON photo_albums(flickr_pushed)
    WHERE flickr_pushed = 0;

-- ============================================================
-- Metadata conflicts: Flickr vs. Apple Photos field mismatches
-- ============================================================

CREATE TABLE IF NOT EXISTS metadata_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    field           TEXT NOT NULL
                        CHECK(field IN ('title', 'description', 'tags')),
    flickr_value    TEXT,
    photos_value    TEXT,
    resolved        INTEGER DEFAULT 0,      -- boolean
    resolution      TEXT                    -- 'flickr' | 'photos' | 'manual'
                        CHECK(resolution IS NULL OR
                              resolution IN ('flickr', 'photos', 'manual')),
    resolved_at     TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(photo_id, field)                 -- one open conflict per field per photo
);

CREATE INDEX IF NOT EXISTS idx_metadata_conflicts_photo
    ON metadata_conflicts(photo_id);
CREATE INDEX IF NOT EXISTS idx_metadata_conflicts_unresolved
    ON metadata_conflicts(resolved)
    WHERE resolved = 0;


-- ============================================================
-- Metadata proposals: sync engine output, reviewed before apply
-- ============================================================

CREATE TABLE IF NOT EXISTS metadata_proposals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id                INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    field                   TEXT NOT NULL
                                CHECK(field IN ('title', 'description', 'tags')),
    proposed_value          TEXT,                   -- JSON for tags, plain text for title/description
    source                  TEXT NOT NULL
                                CHECK(source IN ('flickr', 'photos', 'manual')),
    target                  TEXT NOT NULL
                                CHECK(target IN ('flickr', 'photos')),
    conflict_type           TEXT NOT NULL
                                CHECK(conflict_type IN ('non_conflict', 'divergence', 'collision')),
    source_hash_at_creation TEXT,                   -- source field hash when proposal was created
    target_hash_at_creation TEXT,                   -- target field hash when proposal was created
    status                  TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'applied', 'rejected', 'superseded', 'failed')),
    created_at              TEXT NOT NULL,
    resolved_at             TEXT,
    resolution_note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_photo
    ON metadata_proposals(photo_id);
CREATE INDEX IF NOT EXISTS idx_proposals_pending
    ON metadata_proposals(status)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_proposals_field_target
    ON metadata_proposals(field, target, status)
    WHERE status = 'pending';
-- Enforce the identity key: no two pending proposals with the same (photo, field, value, target, source)
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_identity
    ON metadata_proposals(photo_id, field, proposed_value, target, source)
    WHERE status = 'pending';
