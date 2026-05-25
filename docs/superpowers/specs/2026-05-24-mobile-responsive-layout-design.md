# Mobile-responsive layout — design spec

**Date:** 2026-05-24  
**Issue:** TBD (to be filed before implementation)  
**Approach:** B — hamburger nav + swipe review mode

---

**Status:** ✓ Implemented in #131

## Overview

Blue Pearmain's reviewer UI is desktop-first and breaks down on narrow phone screens: the nav bar overflows, buttons are too small to tap accurately, and keyboard-shortcut-driven workflows don't translate to touch. This spec covers the changes needed to make the UI usable on a phone without disrupting the existing desktop experience.

**Scope:** Pure frontend changes — CSS and JS in Jinja2 templates only. No Python, no backend, no API changes.

---

## Breakpoint

`@media (max-width: 640px)` — phones and very narrow viewports.

- ≤ 640px: mobile layout
- > 640px: existing desktop layout, completely unchanged

All mobile rules are **additive overrides** on top of the existing desktop-first CSS. The desktop experience is not touched.

---

## 1. Navigation — hamburger drawer (base.html)

### Desktop (unchanged)
Flat horizontal nav bar with brand, 7 links, spacer, and stats pill.

### Mobile
The nav collapses to a single-line header:

```
┌────────────────────────────────┐
│ 🍎 Blue Pearmain    ☰  2 to review │
└────────────────────────────────┘
```

- All nav links are hidden (`display: none`)
- A `☰` hamburger button appears on the right
- The stats pill ("N to review") stays visible in the header
- `nav-key` keyboard shortcut badges are hidden on mobile

Tapping `☰` opens a dropdown panel directly below the header:

```
┌────────────────────────────────┐
│ 🍎 Blue Pearmain    ✕  2 to review │
├────────────────────────────────┤
│  Dashboard                     │
│  Review                        │
│  Faces                         │
│  Zones                         │
│  Duplicates        ⚠ 3        │
│  Conflicts                     │
│  Proposals         ⚠ 1        │
└────────────────────────────────┘
```

- Amber count badges appear inline next to their links in the drawer
- The active page link is visually highlighted
- Tapping any link navigates and closes the drawer
- Tapping outside the drawer closes it
- `☰` toggles to `✕` (close icon) while the drawer is open

**Implementation:** a `<div id="mobile-nav-drawer">` injected into base.html, toggled by a small JS snippet (no dependencies). CSS positions it absolutely below the nav bar with `z-index` above content.

---

## 2. Review page — single-card mobile mode (review.html)

### Desktop (unchanged)
Photo grid with `repeat(auto-fill, minmax(220px, 1fr))`, keyboard shortcuts in toolbar, small action buttons.

### Mobile
The grid is hidden. One photo is shown at a time in a full-width card layout:

```
┌────────────────────────────────┐
│ [state dropdown]   ← 12/47 →  │  ← toolbar
├────────────────────────────────┤
│                                │
│      [photo, full width]       │
│      aspect-ratio: 4/3         │
│                                │
├────────────────────────────────┤
│ IMG_4521.jpg                   │
│ landscape, nature              │  ← tags
│ ★★★☆☆  (28–32px stars)        │  ← star rating (larger)
├────────────────────────────────┤
│  ✓ Public  │  ✗ Private  │  →  │  ← action buttons (52px height)
│       [▸ More]                 │  ← friends/family expand
└────────────────────────────────┘
```

**Toolbar (mobile):**
- State dropdown: kept (essential for switching queues)
- Count/progress: shown as `← N/Total →` with tappable prev/next arrows
- Undo button: kept (important — accidental decisions are more likely on touch)
- Keyboard shortcuts block (`.shortcuts`): hidden entirely

**Photo area:**
- Full viewport width minus padding
- 4:3 aspect ratio (same as desktop grid cards)
- Badges (people-flag, screenshot-badge, video-badge, protected-badge) rendered as on desktop

**Meta row:**
- Filename, video label (if applicable), tags, album badge
- Star rating: `font-size: 28px` on mobile (up from 18px desktop) for accurate tapping
- Tap to set/clear stars works via existing click handler (no hover needed)

**Action buttons:**
- Three buttons side by side: `✓ Public` | `✗ Private` | `→ Skip`
- Height: 52px; each takes roughly 1/3 of width
- Protected photos: `⚠️ Override →` button replaces Public, same as desktop
- `▸ More` row below expands friends/family buttons (same toggle logic as desktop)

**Swipe gestures (on the photo area only):**

| Gesture | Decision |
|---|---|
| Swipe right | `make_public` |
| Swipe left | `keep_private` |
| Swipe up | `skip` |

Detection via `touchstart` / `touchend` on the photo element:
- Horizontal swipe (left/right): fires when `|deltaX| > 50` AND `|deltaX| > |deltaY|`
- Upward swipe (skip): fires when `deltaY < -50` AND `|deltaY| > |deltaX|`
- Any other movement (small delta or diagonal) is ignored — treated as a scroll attempt

Visual feedback while dragging:
- Dragging right: card background tints green
- Dragging left: card background tints red
- Dragging up: card background tints grey
- On release (decision fires): card fades out, next card appears

**Navigation between cards:**
- After a decision, the next card in the page's dataset is shown automatically (same as desktop `selectCard()` + `quickDecide()` logic)
- `← N/Total →` arrows in toolbar allow manual navigation (useful if a swipe misfires or to go back to review a card)
- If all cards on the current page are decided, the existing pagination logic applies (reload or next page)

**Single-card state tracking:**
- The mobile view maintains a `currentMobileIndex` pointing into the existing `cards` array (the same `photo-card` divs rendered in the hidden grid)
- No new server-side state; the grid is rendered as normal, just hidden on mobile

---

## 3. Other pages — minor responsive polish

Applied via a shared `@media (max-width: 640px)` block. Changes are the same across all non-review pages:

| Change | Detail |
|---|---|
| Container padding | Reduced from 24px → 12px |
| `.btn` min-height | Set to 44px for comfortable tap targets |
| `.kbd-hints` blocks | Hidden entirely (`display: none`) |
| Nav | Handled by base.html (Section 1) |

**Dashboard:** stat-grid already uses `minmax(160px, 1fr)` and adapts naturally to 2-column on phones. No grid changes needed.

**Faces / Zones / Conflicts / Proposals:** row-based flex lists adapt fine at narrow widths. Only padding + button-size fixes needed.

**Duplicates:** grid already uses `minmax(200px, 1fr)`, renders 1-column on 375px phones. No grid changes needed.

**Photo detail page (`photo.html`):** not in scope for this pass.

---

## Files changed

| File | Change |
|---|---|
| `reviewer/templates/base.html` | Hamburger nav: CSS + drawer HTML + ~30 lines JS |
| `reviewer/templates/review.html` | Mobile card view CSS + swipe JS |
| `reviewer/templates/dashboard.html` | Mobile `@media` padding/button fixes |
| `reviewer/templates/faces.html` | Mobile `@media` padding/button fixes |
| `reviewer/templates/zones.html` | Mobile `@media` padding/button fixes |
| `reviewer/templates/duplicates.html` | Mobile `@media` padding/button fixes |
| `reviewer/templates/conflicts.html` | Mobile `@media` padding/button fixes |
| `reviewer/templates/proposals.html` | Mobile `@media` padding/button fixes |

No Python files, no database changes, no new dependencies.

---

## Testing

- Verify in browser DevTools at 375px (iPhone SE), 390px (iPhone 14), and 430px (iPhone 14 Pro Max) widths
- Verify desktop layout unchanged at 1024px+
- Confirm swipe triggers correct decisions without conflicting with scroll
- Confirm hamburger drawer opens/closes correctly and badges show
- Confirm star rating tappable on mobile
- Run `python -m pytest tests/ -q` — no regressions expected (backend unchanged)
