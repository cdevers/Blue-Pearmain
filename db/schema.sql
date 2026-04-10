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
    --   'auto_private'     : geofence or home flag triggered, skip review queue
    --   'needs_review'     : has people signals, awaiting human decision
    --   'candidate_public' : no people signals, tags proposed, awaiting confirmation
    --   'approved_public'  : human said yes, push to Flickr
    --   'keep_private'     : human said no
    --   'already_public'   : was already public on Flickr before this tool existed
    --   'skipped'          : human deferred decision
    privacy_state           TEXT NOT NULL DEFAULT 'needs_review'
                                CHECK(privacy_state IN (
                                    'auto_private', 'needs_review', 'candidate_public',
                                    'approved_public', 'keep_private', 'already_public',
                                    'skipped', 'duplicate_flickr'
                                )),
    privacy_reason          TEXT,                   -- human-readable explanation of how state was set

    -- Proposed tags (staged, not yet pushed)
    proposed_tags           TEXT,                   -- JSON array of tag strings
    proposed_description    TEXT,                   -- draft description text (may be AI caption, edited)

    -- Push state
    tags_pushed_flickr      INTEGER DEFAULT 0,      -- boolean
    tags_pushed_photos      INTEGER DEFAULT 0,      -- boolean
    perms_pushed_flickr     INTEGER DEFAULT 0,      -- boolean: have we set Flickr visibility?

    -- Thumbnail cache
    thumbnail_path          TEXT,                   -- absolute path on NAS to cached url_l JPEG

    -- Review tracking
    reviewed_at             TEXT,                   -- ISO8601
    review_decision         TEXT,                   -- 'make_public' | 'keep_private' | 'skip'
    review_notes            TEXT,                   -- optional freeform notes
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
    photo_id    INTEGER NOT NULL REFERENCES photos(id),
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
CREATE INDEX IF NOT EXISTS idx_photos_location      ON photos(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_photos_reviewed      ON photos(reviewed_at);
CREATE INDEX IF NOT EXISTS idx_tag_events_photo     ON tag_events(photo_id);
CREATE INDEX IF NOT EXISTS idx_photos_push_state    ON photos(privacy_state, perms_pushed_flickr);
CREATE INDEX IF NOT EXISTS idx_photos_updated       ON photos(updated_at);
