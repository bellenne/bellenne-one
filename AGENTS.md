# BellenneEcho Agent Instructions

## Mandatory sources

This repository is part of the Bellenne product ecosystem.

Before creating, modifying, refactoring or reviewing any UI, frontend component, page, layout, table, chart, form, navigation element or visual state, you MUST read:

1. `docs/BRANDBOOK.md`
2. `docs/products/ECHO.md`

These documents are mandatory requirements.

They are not inspiration and not optional recommendations.

---

# Product architecture

BellenneEcho is a module of the future unified Bellenne platform.

Do not architect the UI as an isolated product with an independent design system.

Design every page so it can later live inside:

Bellenne Shell → Echo → Page

without requiring a visual redesign.

---

# Design reference

BellennePulse is the visual baseline of the Bellenne ecosystem.

BellenneEcho must look like the same application adapted to review and reputation workflows.

Do not attempt to make Echo visually unique by inventing a new UI language.

Echo identity is provided primarily by its Product Accent:

#06B6D4

---

# BellennePulse Protection

BellennePulse is the existing reference implementation of the Bellenne visual system.

When working with BellennePulse:

READ:

`docs/products/PULSE.md`

before making UI changes.

Do NOT proactively redesign, restyle or visually migrate existing BellennePulse pages.

If an existing Pulse component differs from the current BRANDBOOK, do not automatically modify it.

For existing Pulse pages, preserving the current implementation has priority over visual cleanup.

During the application merger:

* preserve Pulse;
* adapt Echo;
* adapt Vector;
* extract reusable Pulse components when useful;
* do not rewrite Pulse solely to make the architecture cleaner.

Any visual redesign of existing Pulse UI requires an explicit user request.

---

# No visual invention

Do not invent new:

* colors;
* gradients;
* fonts;
* typography scales;
* spacing systems;
* border radii;
* card styles;
* button styles;
* shadows;
* glows;
* form styles;
* table styles;
* navigation patterns;
* modal styles;
* dropdown styles;
* badge styles;
* chart palettes;
* icon styles;
* animations;
* decorative patterns.

Use only:

* `docs/BRANDBOOK.md`;
* `docs/products/ECHO.md`;
* existing compliant Bellenne components.

---

# Missing design rule

If the task requires a visual decision not defined by the design system:

1. Search for an existing equivalent Bellenne component.
2. Reuse the closest existing pattern.
3. Reuse existing design tokens.
4. Do not invent a new visual pattern.
5. If no compliant solution exists, report the missing design decision to the user.

Do not silently improvise.

---

# Design tokens

All visual values must come from centralized design tokens.

Hardcoded arbitrary:

hex
rgb
hsl
Tailwind colors
spacing
radius
shadows

inside components are forbidden.

Example of forbidden implementation:

`bg-cyan-500`

if it does not map exactly to the canonical Echo product token.

Prefer semantic token usage such as:

`product-accent`

---

# Product accent

BellenneEcho Product Accent:

#06B6D4

Use it only for Echo identity and interaction highlights.

Do not use it as a replacement for semantic status colors.

Success:

#22D3A5

Danger:

#FF5C70

Warning:

#F59E0B

Neutral:

Bellenne neutral tokens.

---

# Component reuse

Before creating any new visual component:

SEARCH THE CODEBASE.

Determine whether an equivalent already exists.

Prefer:

reuse
composition
extension

over duplication.

Do not create:

EchoButton
EchoCard
EchoModal

if generic Bellenne components can provide the same UI.

---

# Shared component strategy

Generic UI should remain product-independent.

Examples:

Button

Card

Modal

Input

Select

Badge

DataTable

Sidebar

FilterBar

Tooltip

Tabs

EmptyState

MetricCard

Product-specific components should represent business concepts.

Examples:

ReviewCard

ReplyEditor

AutomationRuleCard

RatingBadge

SentimentBadge

ReviewFilters

---

# Existing interface

Never perform an unsolicited redesign.

If the user requests:

"add review filter"

then add the review filter.

Do not additionally:

* redesign the sidebar;
* modify page typography;
* recolor cards;
* replace navigation;
* introduce a different table design;
* redesign existing buttons.

Only change what is necessary for the requested task.

---

# Dependencies

Use the project's existing dependencies.

Do not introduce a new:

* UI framework;
* CSS framework;
* icon library;
* chart library;
* animation library;
* component library

just to achieve a different visual appearance.

---

# Icons

Use the existing icon library.

Do not mix multiple icon families.

Do not use emoji as application icons.

---

# AI UI

BellenneEcho includes AI functionality.

Do not create a separate "AI aesthetic".

Forbidden AI clichés include:

* rainbow gradients;
* excessive purple glow;
* magical backgrounds;
* floating particles;
* excessive sparkle icons;
* glowing AI cards;
* separate AI typography.

AI elements must remain part of the Bellenne design system.

---

# Review UI

Review-heavy pages should prioritize scanability and density.

Do not turn every review into an oversized card.

Users must be able to quickly compare:

* rating;
* review text;
* marketplace;
* product;
* date;
* reply status;
* automation status.

---

# Tables and lists

Use shared Bellenne table/list patterns.

Maintain:

* aligned columns;
* consistent row heights;
* tabular numbers;
* readable statuses;
* compact actions.

Do not invent new list layouts for each page.

---

# Forms

Use shared Bellenne form controls.

All:

inputs
selects
textarea
toggles
checkboxes

must visually match the rest of the platform.

Do not create white form fields inside the dark interface.

---

# Status semantics

Never encode status only through color.

Use:

color + text

or

color + icon + text.

Examples:

Failed

Published

Waiting Approval

Generated

---

# Future integration

When implementing application structure, prefer abstractions that can later support:

Pulse
Echo
Vector

without duplication.

Do not over-engineer speculative features, but avoid unnecessary Echo-specific assumptions in shared UI infrastructure.

---

# Bellenne shell compatibility

New layouts must not depend on an Echo-only global navigation structure.

Product content should be separable from the global application shell.

Keep clear boundaries between:

Global Bellenne UI

Product Echo UI

Page-specific UI

---

# Visual reference

If a BellennePulse reference image exists under:

`docs/brand/`

use it only as a mood and hierarchy reference.

Written rules take precedence.

Do not estimate new:

colors
spacing
sizes
gradients

from the image.

---

# Before completing any UI task

Verify:

* `docs/BRANDBOOK.md` was followed;
* `docs/products/ECHO.md` was followed;
* no arbitrary colors were introduced;
* no arbitrary spacing was introduced;
* no new visual language was introduced;
* Product Accent was used correctly;
* semantic colors remain semantic;
* typography remains consistent;
* components were reused where appropriate;
* no unsolicited redesign occurred;
* responsive behavior works;
* focus states exist;
* hover states exist;
* disabled states exist where applicable;
* the page would visually fit next to BellennePulse;
* the page could later exist inside a unified Bellenne application.

Fix violations before considering the task complete.

---

# Decision priority

When instructions conflict:

1. Current explicit user request.
2. `AGENTS.md`.
3. `docs/BRANDBOOK.md`.
4. `docs/products/ECHO.md`.
5. Existing compliant Bellenne components.
6. Minimal implementation required.

Do not use personal aesthetic preference as a reason to override the design system.

---

# Final principle

BellenneEcho must look like Bellenne.

The product context may change.

The design language does not.

Consistency over novelty.

Function over decoration.

Reuse over reinvention.

# BellenneNest

When working on BellenneNest UI, API configuration, optimization settings, presets, jobs or calculation results:

READ:

`docs/products/NEST.md`

before making changes.

BellenneNest is an API-first optimization engine.

Do not treat it as an analytics dashboard.

Do not invent:

* algorithm settings;
* optimization parameters;
* configuration defaults;
* KPIs;
* charts;
* reports;
* job states;
* API functionality

that are not supported by the actual application or backend.

The frontend must represent the real Nest API and configuration model.

Use the shared Bellenne Design System.

Nest Product Accent:

`#A855F7`

Preserve semantic colors defined by the Bellenne Design System.

When a design pattern already exists in Pulse, Echo, Vector or shared Bellenne components, prefer reuse over creating a Nest-specific alternative.

Nest must be compatible with:

Bellenne Shell → Nest → Page

Do not create an independent Nest visual system.
