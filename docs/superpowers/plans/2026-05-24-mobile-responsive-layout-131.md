# Mobile-Responsive Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Blue Pearmain reviewer UI usable on a phone: hamburger nav on all pages, single-card swipe mode on the review page, responsive polish on secondary pages.

**Architecture:** Additive `@media (max-width: 640px)` CSS overrides in Jinja2 templates; vanilla JS for the hamburger drawer and swipe detection. Desktop layout is completely untouched. No Python/backend changes.

**Tech Stack:** Jinja2 templates, plain CSS, plain JS (no frameworks). Flask dev server: `python reviewer/app.py --config config/config.yml` or `bp ui`.

**GitHub Issue:** #131

---

## Note on testing

All changes are pure frontend (HTML/CSS/JS in Jinja2 templates). There are no unit tests to write for CSS. Each task's verification step uses browser DevTools device emulation. Run `python -m pytest tests/ -q` after each commit to confirm no Python regressions.

---

## File map

| File | Change |
|---|---|
| `reviewer/templates/base.html` | Mobile nav CSS + hamburger HTML + drawer HTML + JS |
| `reviewer/templates/review.html` | Mobile toolbar CSS/HTML + single-card CSS + state JS + swipe JS |
| `reviewer/templates/dashboard.html` | `@media` padding/button fixes |
| `reviewer/templates/faces.html` | `@media` padding/button/kbd fixes |
| `reviewer/templates/zones.html` | `@media` stacked layout + padding/button fixes |
| `reviewer/templates/duplicates.html` | `@media` padding/button/kbd fixes |
| `reviewer/templates/conflicts.html` | `@media` toolbar/scroll/button fixes |
| `reviewer/templates/proposals.html` | `@media` padding/button fixes |

---

## Task 1: Hamburger nav (base.html)

**Files:**
- Modify: `reviewer/templates/base.html`

### Step 1.1 — Add mobile nav CSS (inside `<style>`, before `{% block extra_style %}`)

In `base.html`, find the line:

```
    {% block extra_style %}{% endblock %}
  </style>
```

Replace with:

```html
    /* ── Mobile nav ─────────────────────────────────────── */
    /* Hamburger hidden by default (desktop shows flat nav) */
    #nav-hamburger { display: none; }

    @media (max-width: 640px) {
      /* Hide all nav links and keyboard hints on mobile */
      nav a { display: none; }
      .nav-key { display: none; }

      /* Show hamburger button */
      #nav-hamburger {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 36px;
        height: 36px;
        background: none;
        border: 1px solid var(--border);
        border-radius: var(--radius);
        color: var(--text);
        font-size: 18px;
        line-height: 1;
        cursor: pointer;
        flex-shrink: 0;
        margin-left: 4px;
      }
      #nav-hamburger:hover { background: var(--surface); border-color: #3a3a3a; }

      /* Nav needs relative positioning for the drawer overlay */
      nav { position: relative; }

      /* Dropdown drawer */
      #mobile-nav-drawer {
        display: none;
        position: absolute;
        top: 48px;     /* matches nav height */
        left: 0;
        right: 0;
        background: var(--surface);
        border-bottom: 1px solid var(--border);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
        z-index: 200;
      }
      #mobile-nav-drawer.open { display: block; }

      #mobile-nav-drawer a {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 14px 20px;
        color: var(--muted);
        font-size: 15px;
        border-bottom: 1px solid var(--border);
        text-decoration: none;
      }
      #mobile-nav-drawer a:last-child { border-bottom: none; }
      #mobile-nav-drawer a.active    { color: var(--text); font-weight: 600; }
      #mobile-nav-drawer a:hover     { color: var(--text); text-decoration: none; }

      /* Reduce base container padding on mobile */
      .container { padding: 12px; }

      /* Global tap target size for all .btn elements (applies to every page) */
      .btn { min-height: 44px; }
    }

    {% block extra_style %}{% endblock %}
  </style>
```

- [ ] Make this edit in `base.html`

### Step 1.2 — Add hamburger button and drawer to `<nav>`

Find this exact text in base.html:

```html
  <span class="spacer"></span>
  <span class="stats-pill" id="nav-stats">Loading…</span>
</nav>
```

Replace with:

```html
  <span class="spacer"></span>
  <span class="stats-pill" id="nav-stats">Loading…</span>
  <button id="nav-hamburger" aria-label="Open navigation" aria-expanded="false">☰</button>

  <!-- Mobile nav drawer (shown on ≤640px via hamburger) -->
  <div id="mobile-nav-drawer">
    <a href="{{ url_for('dashboard') }}" {% if request.endpoint == 'dashboard' %}class="active"{% endif %}>
      Dashboard
    </a>
    <a href="{{ url_for('review', state='candidate_public') }}" {% if request.endpoint == 'review' %}class="active"{% endif %}>
      Review
    </a>
    <a href="{{ url_for('faces') }}" {% if request.endpoint == 'faces' %}class="active"{% endif %}>
      Faces
    </a>
    <a href="{{ url_for('zones') }}" {% if request.endpoint == 'zones' %}class="active"{% endif %}>
      Zones
    </a>
    <a href="{{ url_for('duplicates') }}" {% if request.endpoint == 'duplicates' %}class="active"{% endif %}>
      <span>Duplicates</span>
      <span id="drawer-dup-count" class="nav-badge" style="display:none"></span>
    </a>
    <a href="{{ url_for('conflicts') }}" {% if request.endpoint == 'conflicts' %}class="active"{% endif %}>
      <span>Conflicts</span>
      <span id="drawer-conflict-count" class="nav-badge" style="display:none"></span>
    </a>
    <a href="{{ url_for('proposals') }}" {% if request.endpoint == 'proposals' %}class="active"{% endif %}>
      <span>Proposals</span>
      <span id="drawer-proposal-count" class="nav-badge" style="display:none"></span>
    </a>
  </div>
</nav>
```

- [ ] Make this edit in `base.html`

### Step 1.3 — Update `refreshStats()` to sync drawer badge counts

Find this block in base.html's `<script>`:

```javascript
  const dupBadge = document.getElementById('nav-dup-count');
  const dupCount = s.unresolved_duplicates || 0;
  dupBadge.textContent = dupCount;
  dupBadge.style.display = dupCount > 0 ? '' : 'none';
  const conflictBadge = document.getElementById('nav-conflict-count');
  const conflictCount = s.metadata_conflicts?.total || 0;
  conflictBadge.textContent = conflictCount;
  conflictBadge.style.display = conflictCount > 0 ? '' : 'none';
  const propBadge = document.getElementById('nav-proposal-count');
  const propCount = s.proposals?.total || 0;
  propBadge.textContent = propCount;
  propBadge.style.display = propCount > 0 ? '' : 'none';
```

Replace with:

```javascript
  function setBadge(navId, drawerId, count) {
    const nav = document.getElementById(navId);
    const drawer = document.getElementById(drawerId);
    if (nav) { nav.textContent = count; nav.style.display = count > 0 ? '' : 'none'; }
    if (drawer) { drawer.textContent = count; drawer.style.display = count > 0 ? '' : 'none'; }
  }
  setBadge('nav-dup-count',      'drawer-dup-count',      s.unresolved_duplicates || 0);
  setBadge('nav-conflict-count', 'drawer-conflict-count', s.metadata_conflicts?.total || 0);
  setBadge('nav-proposal-count', 'drawer-proposal-count', s.proposals?.total || 0);
```

- [ ] Make this edit in `base.html`

### Step 1.4 — Add hamburger toggle JS

Find this line in base.html's `<script>`:

```javascript
refreshStats();
```

Add these lines immediately after:

```javascript

// Mobile hamburger drawer
(function() {
  const btn = document.getElementById('nav-hamburger');
  const drawer = document.getElementById('mobile-nav-drawer');
  if (!btn || !drawer) return;

  btn.addEventListener('click', function(e) {
    e.stopPropagation();
    const open = drawer.classList.toggle('open');
    btn.textContent = open ? '✕' : '☰';
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });

  // Close on outside tap
  document.addEventListener('click', function() {
    drawer.classList.remove('open');
    btn.textContent = '☰';
    btn.setAttribute('aria-expanded', 'false');
  });

  // Close on nav link tap (navigation fires anyway, but close cleanly)
  drawer.querySelectorAll('a').forEach(a => {
    a.addEventListener('click', function() {
      drawer.classList.remove('open');
    });
  });
})();
```

- [ ] Make this edit in `base.html`

### Step 1.5 — Verify hamburger nav

- [ ] Start the dev server: `python reviewer/app.py --config config/config.yml`
- [ ] Open http://localhost:5173 in a browser
- [ ] Open DevTools → toggle device toolbar → set width to 375px
- [ ] Confirm: nav links are hidden, brand + stats pill + ☰ button are visible
- [ ] Tap ☰ → drawer opens with all 7 links; Duplicates/Conflicts/Proposals show badges if counts > 0
- [ ] Tap any link → drawer closes and navigates
- [ ] Tap outside drawer → drawer closes
- [ ] Set width to 1024px → confirm desktop nav unchanged (all links visible, no hamburger)
- [ ] Run `python -m pytest tests/ -q` → all tests pass

### Step 1.6 — Commit

```bash
git add reviewer/templates/base.html
git commit -m "feat(#131): mobile hamburger nav drawer

Adds responsive hamburger menu for ≤640px viewports. Desktop flat
nav unchanged. Drawer shows all 7 links with badge counts synced
from refreshStats(). Closes on outside tap or link click.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] Run commit

---

## Task 2: Review toolbar — mobile polish (review.html)

**Files:**
- Modify: `reviewer/templates/review.html`

### Step 2.1 — Add mobile toolbar CSS

In `review.html`'s `{% block extra_style %}`, add these rules at the end (just before `{% endblock %}`):

```css
/* ── Mobile toolbar ─────────────────────────────────── */
/* Mobile progress row: hidden on desktop, shown via JS on mobile */
.mobile-progress-row { display: none; }

@media (max-width: 640px) {
  .toolbar {
    flex-wrap: wrap;
    padding: 8px 12px;
    gap: 8px;
  }

  /* Hide keyboard hints entirely on mobile */
  .shortcuts { display: none !important; }

  /* Hide desktop count and spacer — replaced by mobile progress row */
  .toolbar .count,
  .toolbar .spacer { display: none; }

  /* State select fills available space */
  #state-select { flex: 1; min-width: 0; }

  /* Undo button: more prominent on mobile (accidental swipes are likely) */
  #undoBtn {
    order: 9;
    width: 100%;
    justify-content: center;
    min-height: 48px;
    font-size: 15px;
    color: #f5a623;
    border-color: #7a5c00;
    background: #2a1e00;
  }

  /* Mobile progress row: show when JS activates it */
  .mobile-progress-row {
    display: flex;
    align-items: center;
    gap: 8px;
    justify-content: center;
    width: 100%;
    order: 10;
    padding: 4px 0;
  }
  .mobile-nav-btn {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: var(--radius);
    padding: 4px 16px;
    font-size: 20px;
    line-height: 1.4;
    cursor: pointer;
    min-height: 36px;
    min-width: 44px;
  }
  .mobile-nav-btn:hover { background: #252525; }
  #mobile-progress-text {
    font-size: 13px;
    color: var(--muted);
    min-width: 64px;
    text-align: center;
  }
}
```

- [ ] Make this edit in `review.html`

### Step 2.2 — Add mobile progress row to toolbar HTML

In `review.html`, find the closing line of the toolbar:

```html
  </div>
</div>
```

(The `</div>` that closes `.shortcuts` and the `</div>` that closes `.toolbar`.)

More precisely, find:

```html
    <span><kbd>Z</kbd> undo</span>
    <span><kbd>R</kbd> reload</span>
  </div>
</div>
```

Replace with:

```html
    <span><kbd>Z</kbd> undo</span>
    <span><kbd>R</kbd> reload</span>
  </div>

  <!-- Mobile progress indicator (shown on ≤640px) -->
  <div class="mobile-progress-row">
    <button class="mobile-nav-btn" onclick="mobileNavPrev()" aria-label="Previous photo">‹</button>
    <span id="mobile-progress-text">— / —</span>
    <button class="mobile-nav-btn" onclick="mobileNavNext()" aria-label="Next photo">›</button>
  </div>
</div>
```

- [ ] Make this edit in `review.html`

### Step 2.3 — Verify toolbar on mobile

- [ ] Reload dev server if needed
- [ ] Open DevTools at 375px on the Review page
- [ ] Confirm: state dropdown + undo button (if history) + `‹ — / — ›` progress row visible
- [ ] Confirm: keyboard shortcuts (P/X/Space/J/K) are hidden
- [ ] Confirm: at 1024px, toolbar looks unchanged (shortcuts visible, no progress row)
- [ ] Run `python -m pytest tests/ -q` → all tests pass

### Step 2.4 — Commit

```bash
git add reviewer/templates/review.html
git commit -m "feat(#131): review toolbar mobile polish

Hides keyboard shortcuts on mobile, shows progress row with prev/next
arrows, and makes undo button full-width + prominent. Desktop toolbar
unchanged.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] Run commit

---

## Task 3: Review single-card CSS (review.html)

**Files:**
- Modify: `reviewer/templates/review.html`

### Step 3.1 — Add single-card mobile CSS

In `review.html`'s `{% block extra_style %}`, add these rules after the mobile toolbar block from Task 2 (just before `{% endblock %}`):

```css
/* ── Mobile single-card view ────────────────────────── */
@media (max-width: 640px) {
  /* Switch grid to block; cards shown one at a time */
  .grid {
    display: block;
    padding: 12px;
  }

  /* Hide all cards by default; JS adds mobile-current to show one */
  .photo-card { display: none; }

  /* The visible card: full width, block layout */
  .photo-card.mobile-current {
    display: block;
    width: 100%;
  }

  /* Normalise all thumb aspect ratios to 4/3 on mobile
     (overrides panoramic 3/1 and portrait-pano 1/3) */
  .photo-card.mobile-current .thumb,
  .photo-card.pano.mobile-current .thumb,
  .photo-card.pano-portrait.mobile-current .thumb {
    aspect-ratio: 4 / 3;
  }
  .photo-card.pano.mobile-current .thumb img,
  .photo-card.pano-portrait.mobile-current .thumb img {
    object-fit: cover;
  }

  /* Larger action buttons — easy to tap */
  .photo-card .actions { gap: 8px; }
  .photo-card .actions button {
    min-height: 52px;
    font-size: 14px;
    padding: 8px 4px;
  }

  /* Bigger star rating for accurate touch targets */
  .star-rating {
    font-size: 28px;
    margin: 8px 0;
  }

  /* Slightly more breathing room in the meta row */
  .photo-card .meta { padding: 12px; }
  .photo-card .filename { font-size: 13px; margin-bottom: 6px; }
}
```

- [ ] Make this edit in `review.html`

### Step 3.2 — Verify single-card CSS

Note: The CSS hides all cards until JS adds `mobile-current`. At this step (before Task 4 JS), you will see a blank grid on mobile — that is expected.

- [ ] Open DevTools at 375px on the Review page
- [ ] Confirm: the photo grid is invisible (cards hidden — expected, JS comes in Task 4)
- [ ] Open browser console → run `document.querySelectorAll('.photo-card')[0].classList.add('mobile-current')`
- [ ] Confirm: one full-width card appears with 4/3 aspect ratio, large buttons, large stars
- [ ] Confirm: at 1024px, grid layout unchanged
- [ ] Run `python -m pytest tests/ -q` → all tests pass

### Step 3.3 — Commit

```bash
git add reviewer/templates/review.html
git commit -m "feat(#131): review single-card CSS for mobile

Hides all photo cards on mobile; only .photo-card.mobile-current is
shown. Normalises thumb aspect ratios, enlarges action buttons (52px)
and star rating (28px). Desktop grid layout unchanged.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] Run commit

---

## Task 4: Review single-card JS state (review.html)

**Files:**
- Modify: `reviewer/templates/review.html`

### Step 4.1 — Add mobile state JS

In `review.html`, find the closing `</div>` and `{% endblock %}` at the very end of `{% block scripts %}`:

```html
</div>
{% endblock %}
```

(This is the `</div>` for `#override-modal`.)

Add a new `<script>` block after the override-modal div, just before `{% endblock %}`:

```html
<!-- Mobile single-card state management -->
<script>
// === MOBILE SINGLE-CARD VIEW ===
// Active only on mobile (≤640px). Maintains currentMobileIndex into the
// photo-card NodeList and updates which card has the mobile-current class.
// Exposes showMobileCard() globally so the swipe section (Task 5) and
// the mobileNavPrev/Next buttons can call it.

let currentMobileIndex = 0;

function showMobileCard(index) {
  const cards = [...document.querySelectorAll('.photo-card')];
  if (!cards.length) return;

  // Clamp to valid range
  currentMobileIndex = Math.max(0, Math.min(index, cards.length - 1));

  // Swap mobile-current class
  cards.forEach((c, i) => c.classList.toggle('mobile-current', i === currentMobileIndex));

  // Keep the desktop selection in sync (preserves keyboard shortcut state)
  selectCard(cards[currentMobileIndex]);

  // Update ‹ N/Total › indicator
  _updateMobileProgress(cards.length);

  // Re-attach swipe handler to the now-visible card
  // (window.attachSwipeHandler defined in Task 5 swipe section)
  if (window.attachSwipeHandler) window.attachSwipeHandler(cards[currentMobileIndex]);

  // Scroll to top so the photo is always fully visible
  window.scrollTo({ top: 0, behavior: 'instant' });
}

function _updateMobileProgress(total) {
  const el = document.getElementById('mobile-progress-text');
  if (el) el.textContent = `${currentMobileIndex + 1} / ${total}`;
}

function mobileNavPrev() {
  if (currentMobileIndex > 0) showMobileCard(currentMobileIndex - 1);
}

function mobileNavNext() {
  const cards = [...document.querySelectorAll('.photo-card')];
  if (currentMobileIndex < cards.length - 1) showMobileCard(currentMobileIndex + 1);
}

// Patch quickDecide to advance the mobile card view after a successful decision.
// This is applied here — after quickDecide is defined in the script above —
// so the original function is not modified directly (keeps the swipe logic isolated).
if (window.innerWidth <= 640) {
  const _quickDecide = quickDecide;
  quickDecide = async function(id, decision, card) {
    const cards = [...document.querySelectorAll('.photo-card')];
    const idx = cards.indexOf(card);
    await _quickDecide(id, decision, card);
    // Only advance if the decision was applied (card has a decided-* class)
    if (card.classList.contains('decided-pub') ||
        card.classList.contains('decided-prv') ||
        card.classList.contains('decided-skp')) {
      showMobileCard(idx + 1);
    }
  };

  // Initialise the mobile view once the page (and all cards) are loaded.
  // The existing load handler already calls selectCard(first) and scrolls
  // to top; our handler additionally activates the mobile-current class.
  window.addEventListener('load', () => {
    showMobileCard(0);
  });
}
</script>
```

- [ ] Make this edit in `review.html`

### Step 4.2 — Verify single-card JS state

- [ ] Reload the Review page in DevTools at 375px
- [ ] Confirm: first photo is visible (mobile-current class on card 0)
- [ ] Confirm: progress text shows `1 / N` (N = total cards on page)
- [ ] Tap `›` arrow → next photo appears, progress shows `2 / N`
- [ ] Tap `‹` arrow → returns to first photo
- [ ] Tap a "✓ Public" or "✗ Private" button → card advances to next automatically
- [ ] Confirm undo button is prominent (full-width, amber) after a decision
- [ ] Tap Undo → reverts and reloads
- [ ] At 1024px: grid shows normally, no mobile-current behaviour

### Step 4.3 — Commit

```bash
git add reviewer/templates/review.html
git commit -m "feat(#131): review single-card JS state management

Adds currentMobileIndex tracking, showMobileCard(), mobileNavPrev/Next,
and a quickDecide patch that advances the mobile card view after each
decision. Only active on mobile (≤640px). Desktop behaviour unchanged.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] Run commit

---

## Task 5: Swipe gesture detection (review.html)

**Files:**
- Modify: `reviewer/templates/review.html`

### Step 5.1 — Add swipe JS

Find the `{% endblock %}` at the very end of `{% block scripts %}` (after the Task 4 script block you just added).

Add another `<script>` block just before `{% endblock %}`:

```html
<!-- Mobile swipe gesture detection -->
<script>
// === MOBILE SWIPE ===
// Isolated touch handler for the review single-card view.
// Detects directional swipes on the photo thumbnail and calls quickDecide.
//
// Swipe right  →  make_public
// Swipe left   →  keep_private
// Swipe up     →  skip
//
// A gesture fires only when:
//   horizontal:  |deltaX| > 50 AND |deltaX| > |deltaY|
//   upward:      deltaY < -50  AND |deltaY| > |deltaX|
// All other movements (small or diagonal) are ignored — treated as scroll.

(function() {
  if (window.innerWidth > 640) return; // no-op on desktop

  let _swipeStart = null;

  // Named handler references (required for removeEventListener to work)
  function onTouchStart(e) {
    _swipeStart = {
      x: e.changedTouches[0].clientX,
      y: e.changedTouches[0].clientY,
    };
    e.currentTarget.addEventListener('touchmove', onTouchMove, { passive: true });
  }

  function onTouchMove(e) {
    if (!_swipeStart) return;
    const dx = e.changedTouches[0].clientX - _swipeStart.x;
    const dy = e.changedTouches[0].clientY - _swipeStart.y;
    const card = e.currentTarget.closest('.photo-card');
    if (!card) return;

    // Tint card background while dragging to signal pending decision
    if (Math.abs(dx) > Math.abs(dy)) {
      const intensity = Math.min(Math.abs(dx) / 200, 0.5);
      if (dx > 20) {
        card.style.background = `rgba(45, 125, 70, ${intensity})`; // green → public
      } else if (dx < -20) {
        card.style.background = `rgba(139, 32, 32, ${intensity})`; // red → private
      } else {
        card.style.background = '';
      }
    } else if (dy < -20) {
      card.style.background = 'rgba(60, 60, 60, 0.3)'; // grey → skip
    } else {
      card.style.background = '';
    }
  }

  function onTouchEnd(e) {
    const thumb = e.currentTarget;
    thumb.removeEventListener('touchmove', onTouchMove);

    if (!_swipeStart) return;
    const dx = e.changedTouches[0].clientX - _swipeStart.x;
    const dy = e.changedTouches[0].clientY - _swipeStart.y;
    _swipeStart = null;

    const card = thumb.closest('.photo-card');
    if (!card) return;

    // Always clear tinting on release
    card.style.background = '';

    const absDx = Math.abs(dx);
    const absDy = Math.abs(dy);

    if (absDx > 50 && absDx > absDy) {
      // Horizontal swipe
      if (dx > 0) {
        // Swipe right → public (or pulse badge if protected)
        if (isProtected(card)) {
          pulseProtectedBadge(card);
        } else {
          quickDecide(+card.dataset.id, 'make_public', card);
        }
      } else {
        // Swipe left → private
        quickDecide(+card.dataset.id, 'keep_private', card);
      }
    } else if (dy < -50 && absDy > absDx) {
      // Upward swipe → skip
      quickDecide(+card.dataset.id, 'skip', card);
    }
    // All other movements (scroll attempts, diagonal drags): do nothing
  }

  // Attach swipe handlers to a card's thumb element.
  // Called by showMobileCard() each time a new card becomes current.
  // Removes existing listeners before adding new ones (safe to call repeatedly).
  function attachSwipeHandler(card) {
    if (!card) return;
    const thumb = card.querySelector('.thumb');
    if (!thumb) return;
    thumb.removeEventListener('touchstart', onTouchStart);
    thumb.removeEventListener('touchend', onTouchEnd);
    thumb.addEventListener('touchstart', onTouchStart, { passive: true });
    thumb.addEventListener('touchend', onTouchEnd);
  }

  // Expose globally so showMobileCard (Task 4) can call it
  window.attachSwipeHandler = attachSwipeHandler;

  // Attach to the first card immediately (load handler in Task 4 will also
  // call showMobileCard(0) → attachSwipeHandler, but this handles the case
  // where this script runs after the load event)
  const first = document.querySelector('.photo-card');
  if (first) attachSwipeHandler(first);
})();
</script>
```

- [ ] Make this edit in `review.html`

### Step 5.2 — Verify swipe gestures

- [ ] Open DevTools at 375px on the Review page
- [ ] Confirm: first photo is visible
- [ ] In DevTools, enable touch emulation; slowly drag the photo right — card should tint green
- [ ] Release: card should be marked public and advance to next
- [ ] Drag left — card tints red → release → marked private → advances
- [ ] Drag up — card tints grey → release → marked skipped → advances
- [ ] Drag diagonally (equal X and Y) → card does NOT decide (diagonal dead zone)
- [ ] Small drag (< 50px) → card does NOT decide
- [ ] On a protected card: swipe right → protected badge pulses, no decision fires
- [ ] Confirm undo is accessible and functional after a swipe decision
- [ ] At 1024px: no swipe JS active (IIFE returns early on desktop)
- [ ] Run `python -m pytest tests/ -q` → all tests pass

### Step 5.3 — Commit

```bash
git add reviewer/templates/review.html
git commit -m "feat(#131): mobile swipe gestures on review page

Swipe right=public, left=private, up=skip. Dead zone: |delta|>50px and
dominant-axis check prevents accidental decisions during scroll. Visual
tinting during drag, cleared on release. Protected cards pulse badge
on right-swipe rather than deciding. Isolated in IIFE; desktop unaffected.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] Run commit

---

## Task 6: Responsive polish for secondary pages

**Files:**
- Modify: `reviewer/templates/dashboard.html`
- Modify: `reviewer/templates/faces.html`
- Modify: `reviewer/templates/zones.html`
- Modify: `reviewer/templates/duplicates.html`
- Modify: `reviewer/templates/conflicts.html`
- Modify: `reviewer/templates/proposals.html`

### Step 6.1 — dashboard.html

No extra CSS needed — `.container` padding and `.btn` tap targets are already covered by base.html's global `@media` block added in Task 1.

- [ ] Confirm no `{% block extra_style %}` changes needed for dashboard.html (skip to Step 6.2)

- [ ] Make this edit in `dashboard.html`

### Step 6.2 — faces.html

Add at the end of `{% block extra_style %}` (before `{% endblock %}`):

```css
@media (max-width: 640px) {
  .faces-container { padding: 12px; }
  .kbd-hints { display: none; }
  .person-row { padding: 10px 12px; }
}
```

(`.btn` tap targets handled globally by base.html — no need to repeat here.)

- [ ] Make this edit in `faces.html`

### Step 6.3 — zones.html

The zones page uses `grid-template-columns: 1fr 320px` (zone list + add-zone form side by side). On mobile this overflows. Stack it vertically.

Add at the end of `{% block extra_style %}` (before `{% endblock %}`):

```css
@media (max-width: 640px) {
  .zones-layout {
    grid-template-columns: 1fr;
    padding: 12px;
    gap: 16px;
  }
}
```

(`.btn` tap targets handled globally by base.html.)

- [ ] Make this edit in `zones.html`

### Step 6.4 — duplicates.html

Add at the end of `{% block extra_style %}` (before `{% endblock %}`):

```css
@media (max-width: 640px) {
  .kbd-hints { display: none; }
}
```

(`.container` padding and `.btn` tap targets handled globally by base.html.)

- [ ] Make this edit in `duplicates.html`

### Step 6.5 — conflicts.html

The conflicts page has a sticky toolbar with keyboard hints and a `.field-row` grid with 4 columns (`90px 1fr 1fr 180px`) that overflows on narrow screens. Make the field rows scrollable and hide keyboard hints.

Add at the end of `{% block extra_style %}` (before `{% endblock %}`):

```css
@media (max-width: 640px) {
  /* Hide keyboard hints */
  .toolbar .shortcuts { display: none; }
  .toolbar .spacer    { display: none; }

  .toolbar { flex-wrap: wrap; padding: 8px 12px; }

  /* Conflict card: stack thumb above body on mobile */
  .conflict-card {
    grid-template-columns: 1fr;
    margin: 12px;
  }
  .conflict-card .thumb {
    width: 100%;
    aspect-ratio: 4 / 3;
    height: auto;
  }

  /* Field rows: scroll horizontally rather than overflow */
  .field-row {
    overflow-x: auto;
    gap: 6px;
    font-size: 12px;
  }

}
```

(`.btn` tap targets handled globally by base.html.)

- [ ] Make this edit in `conflicts.html`

### Step 6.6 — proposals.html

Add at the end of `{% block extra_style %}` (before `{% endblock %}`):

```css
@media (max-width: 640px) {
  .toolbar { flex-wrap: wrap; padding: 8px 12px; }
  .toolbar .spacer { display: none; }
}
```

(`.btn`, `.btn-approve`, `.btn-reject` tap targets all handled globally by base.html's `.btn { min-height: 44px }` rule.)

- [ ] Make this edit in `proposals.html`

### Step 6.7 — Verify secondary pages

- [ ] Open DevTools at 375px; visit Dashboard, Faces, Zones, Duplicates, Conflicts, Proposals in turn
- [ ] Dashboard: stat grid 2 columns, action buttons easy to tap
- [ ] Faces: person list rows fit; kbd hints hidden; buttons tappable
- [ ] Zones: two panels stack vertically (list on top, add form below)
- [ ] Duplicates: single-column grid; kbd hints hidden
- [ ] Conflicts: conflict card stacks (thumb above body); field rows horizontally scrollable; keyboard hints hidden
- [ ] Proposals: toolbar wraps; buttons tappable
- [ ] All pages: hamburger nav opens correctly (Task 1)
- [ ] At 1024px: all pages look unchanged
- [ ] Run `python -m pytest tests/ -q` → all tests pass

### Step 6.8 — Commit

```bash
git add reviewer/templates/dashboard.html \
        reviewer/templates/faces.html \
        reviewer/templates/zones.html \
        reviewer/templates/duplicates.html \
        reviewer/templates/conflicts.html \
        reviewer/templates/proposals.html
git commit -m "feat(#131): responsive polish for secondary pages

Reduces padding, hides kbd hints, ensures 44px tap targets, stacks
zones layout vertically, and makes conflict field rows scrollable on
mobile (≤640px). Desktop layouts unchanged.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] Run commit

---

## Task 7: Verification, README update, and issue close

**Files:**
- Modify: `README.md`

### Step 7.1 — Full test suite

```bash
python -m pytest tests/ -q
```

- [ ] Run tests; confirm all pass

### Step 7.2 — Cross-device manual check

- [ ] DevTools at 375px (iPhone SE) — review swipe, nav drawer, secondary pages
- [ ] DevTools at 390px (iPhone 14) — same checks
- [ ] DevTools at 768px (iPad mini) — confirm desktop layout (not mobile) is used
- [ ] DevTools at 1024px — full desktop layout unchanged

### Step 7.3 — Update README

Find this paragraph in `README.md`:

```
**Mobile (iPhone/iPad):** The review grid works on iOS Safari. After reviewing a batch, tap **Reload ↺** at the bottom to refresh the queue in place — this avoids a pagination issue where "Next" would skip photos that had just been decided. The single-photo detail view is currently desktop-optimised; the sidebar overlaps the image on narrow screens, so the grid view is recommended on mobile.
```

Replace with:

```
**Mobile (iPhone/iPad):** The reviewer UI is optimised for phone and tablet use. On screens ≤640px wide the navigation collapses to a hamburger menu, and the review queue switches to a single-card swipe mode: swipe right to approve as public, left to keep private, or up to skip. Large tap buttons appear below each photo as an alternative to swiping. Tap the undo button (prominently displayed after any decision) to reverse accidental swipes. All secondary pages (Dashboard, Faces, Zones, Duplicates, Conflicts, Proposals) are responsive at the same breakpoint.
```

- [ ] Make this edit in `README.md`

### Step 7.4 — Mark spec done

In `docs/superpowers/specs/2026-05-24-mobile-responsive-layout-design.md`, add a status line after the first `---`:

```markdown
**Status:** ✓ Implemented in #131
```

- [ ] Make this edit

### Step 7.5 — Commit

```bash
git add README.md docs/superpowers/specs/2026-05-24-mobile-responsive-layout-design.md
git commit -m "docs(#131): update README for mobile-responsive UI

Documents swipe gestures, hamburger nav, and mobile breakpoint.
Replaces the old 'grid works on iOS' note. Marks spec complete.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] Run commit

### Step 7.6 — Push and close issue

```bash
git push origin main
```

Then close GitHub issue #131:

```bash
gh issue close 131 --comment "Implemented in this push.

- Hamburger nav drawer on all pages (≤640px)
- Single-card swipe mode on the review page (swipe right=public, left=private, up=skip)
- Prominent undo button on mobile
- Responsive polish on Dashboard, Faces, Zones, Duplicates, Conflicts, Proposals
- Desktop layout unchanged throughout"
```

- [ ] Push and close issue
