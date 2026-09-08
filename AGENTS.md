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

# BellenneProof

When working on BellenneProof, including UI, backend, API, webhooks, queue processing, workers, presets, results, integrations, logs or operational tooling:

READ:

`docs/products/PROOF.md`

before making changes.

For any UI-related task, also READ:

`docs/BRANDBOOK.md`

These documents are mandatory requirements.

---

## Product Role

BellenneProof is the production orchestration module of the Bellenne ecosystem.

Its primary responsibility is to:

- receive work from external systems such as CRM;
- create Proof Jobs;
- manage the processing queue;
- assign jobs to Proof Workers;
- track execution;
- receive processing results;
- deliver results back to external systems;
- expose operational status, logs and configuration through the UI.

BellenneProof is NOT an image editor.

BellenneProof is NOT a BI analytics product.

BellenneProof is NOT the component that performs the actual image processing.

---

## Architecture Boundary

The system consists conceptually of:

BellenneProof
→ Proof Core
→ Proof Queue
→ Proof Worker
→ Proof Result

### Proof Core

Proof Core is responsible for orchestration.

Typical responsibilities:

- API;
- webhooks;
- jobs;
- queue;
- worker coordination;
- presets;
- integrations;
- results;
- logs;
- configuration;
- operational UI.

### Proof Worker

Proof Worker is a separate executable/service responsible for actual processing.

Typical responsibilities may include:

- locating the source file;
- loading the source;
- analyzing the image;
- selecting the proof fragment;
- rendering the proof;
- saving the result;
- reporting execution state back to Proof Core.

Do not move processing responsibilities into Proof Core merely because it is easier to implement.

Do not move orchestration responsibilities into Proof Worker without an explicit architectural reason.

Maintain a clear Core / Worker boundary.

---

## Backend Is Source of Truth

Backend contracts are the source of truth for:

- Job statuses;
- Queue behavior;
- Worker statuses;
- Retry behavior;
- Preset fields;
- Processing stages;
- Integration states;
- Result states;
- API payloads;
- Webhook contracts.

Frontend must represent the real system.

Frontend must not invent additional workflow logic.

---

## No Invented Workflow

Do NOT invent processing stages.

For example, do not add stages such as:

- Preflight;
- AI Analysis;
- Quality Control;
- Approval;
- Validation;
- Rendering;
- Uploading;
- Post Processing

unless these stages actually exist in the implemented backend workflow.

The UI must display real states, not an imagined ideal workflow.

---

## No Invented Configuration

Do NOT invent Proof configuration parameters.

Do not add settings such as:

- crop dimensions;
- DPI;
- margins;
- thumbnail dimensions;
- color profile;
- output quality;
- selection strategy;
- AI confidence;
- render quality;
- optimization level

unless they exist in the backend, specification or explicit user request.

If a required setting is missing from the specification:

STOP and report the missing decision.

Do not improvise a default.

---

## No Invented Defaults

Do NOT invent default values for:

- timeouts;
- retry counts;
- queue limits;
- worker limits;
- file paths;
- dimensions;
- processing parameters;
- polling intervals;
- retention periods;
- API limits.

Defaults must come from:

1. existing code;
2. configuration;
3. documented requirements;
4. explicit user instruction.

---

## Job Model

Proof Job is a primary domain entity.

When implementing Job-related functionality, preserve separation between:

Processing Status

and

Delivery Status.

Example:

Processing:
Completed

Delivery:
Failed

This means the proof was successfully generated but delivery to CRM failed.

Do NOT collapse these states into a single generic `Failed` state.

Retrying delivery must not unnecessarily rerun proof generation.

---

## Retry Semantics

Retry operations must be explicit.

Possible operations may include:

Retry Job

Retry Delivery

Retry Integration

Retry Processing

Use only operations actually supported by the backend.

Do not implement one generic retry action if multiple failure domains require different behavior.

Never rerun expensive processing solely because an external callback failed if the existing result can be reused.

---

## Queue

Proof Queue is operational infrastructure.

Do not invent:

- priorities;
- drag-and-drop ordering;
- scheduling;
- manual reassignment;
- concurrency settings;
- queue pause/resume;
- queue groups

unless supported by the actual system.

The UI must not promise queue capabilities that the backend does not provide.

---

## Workers

Treat Workers as a collection.

Do not hardcode the architecture around exactly one Worker unless the backend explicitly requires it.

A Worker may expose data such as:

- identity;
- node;
- status;
- current job;
- last heartbeat;
- version;
- recent error.

Only display fields that actually exist.

Do not create fake worker health metrics.

---

## Worker Communication

Core ↔ Worker communication must use explicit contracts.

Avoid hidden coupling through implementation details.

Prefer clear DTOs / API contracts for:

- job assignment;
- status update;
- heartbeat;
- completion;
- failure;
- result submission.

Changes to Worker contracts should be made carefully because Core and Worker may be deployed separately.

Maintain backward compatibility when reasonably possible.

---

## Webhooks

Incoming CRM webhooks must not directly perform heavy image processing.

Preferred flow:

Webhook
→ validate request
→ create Job
→ enqueue
→ respond
→ Worker processes asynchronously

Do not block an external webhook request waiting for long-running proof generation unless explicitly required.

Webhook handlers should remain small and predictable.

---

## Idempotency

Webhook and external integration handling should be designed to tolerate duplicate delivery when possible.

Do not assume an external webhook will only be delivered once.

Before creating duplicate work, check whether the existing architecture provides:

- external event ID;
- CRM entity ID;
- idempotency key;
- existing Job relation.

Do not invent an idempotency strategy that could discard legitimate Jobs.

If the requirement is ambiguous, report it.

---

## External Integrations

CRM and other external systems must be treated separately from internal processing.

Integration failure must not automatically imply processing failure.

Maintain clear separation between:

Input reception

Processing

Result creation

External delivery

This separation should be visible both in backend domain logic and UI states.

---

## Secrets

Never expose:

- CRM tokens;
- webhook secrets;
- API keys;
- Worker tokens;
- passwords;
- authorization headers

inside:

- normal logs;
- Job timelines;
- tables;
- frontend source;
- API responses intended for normal UI.

Mask sensitive values.

Do not log full secret payloads.

---

## Logs

Logs must be useful for diagnostics.

Prefer structured context such as:

Job ID

Worker ID

Request ID

Integration

Event

Error Code

Do not log large binary/image contents.

Do not dump full sensitive webhook payloads without filtering secrets.

---

## Files

File paths and source files are operational data.

Do not assume:

- a specific drive letter;
- a specific network share;
- Windows-only path syntax in server-side domain logic;
- a fixed output directory

unless defined by configuration.

Filesystem locations must be configurable where appropriate.

Do not hardcode production paths into application logic.

---

## Result Integrity

A completed Proof Result must remain traceable to:

- Job;
- source data;
- Worker;
- Preset/configuration;
- processing time;
- output file where applicable.

Do not silently overwrite previous results unless this is intentional product behavior.

---

## UI Design

BellenneProof uses the shared Bellenne Design System.

Product Accent:

`#E94D8A`

Name:

Proof Rose

Use Product Accent only for Proof identity and active operational states.

Do not use Proof Rose as:

- Success;
- Error;
- Warning.

Use the semantic colors defined by `docs/BRANDBOOK.md`.

---

## UI Character

Proof UI should feel like an operational control center.

Prioritize:

- current state;
- queue;
- jobs;
- workers;
- errors;
- results;
- traceability.

Do not turn Proof into a decorative analytics dashboard.

Do not add charts solely to make the Overview look richer.

Do not create fake KPIs.

Do not create an oversized SaaS-style dashboard if compact operational tables communicate the information better.

---

## Shared Components

Use shared Bellenne components whenever possible.

Prefer:

Button

Card

StatusCard

DataTable

Input

Select

Badge

Modal

Tabs

Tooltip

Alert

FilterBar

Timeline

EmptyState

LogView

Do not create:

ProofButton

ProofInput

ProofModal

ProofTable

only because the component is used inside Proof.

---

## Product Components

Product-specific components are allowed when they represent Proof business concepts.

Examples:

JobTable

JobStatus

JobTimeline

QueueTable

WorkerTable

WorkerStatus

ProofPreview

ResultPanel

PresetSelector

IntegrationStatus

WebhookEvent

RetryAction

These components should still use the shared Bellenne visual primitives.

---

## Image Preview

A Proof Preview displays the actual processing result.

Do not:

- recolor it;
- apply Product Accent over it;
- apply decorative filters;
- alter the image for visual consistency;
- add editing tools without a real requirement.

Preview is for inspecting the generated result.

It is not a graphics editor.

---

## No AI Aesthetic

If computer vision, OpenCV, ML or another intelligent algorithm is used internally, do not create a separate AI visual identity.

Do not add:

- sparkles;
- magic gradients;
- AI avatars;
- glowing AI cards;
- fake confidence scores

unless explicitly required by actual functionality.

---

## No Unsolicited Redesign

Do not redesign unrelated Bellenne components while implementing Proof functionality.

If the task is:

"add Worker status"

then add Worker status.

Do not also:

- redesign the sidebar;
- change global colors;
- replace existing cards;
- modify typography;
- restructure unrelated pages.

---

## Future Bellenne Integration

BellenneProof must remain compatible with the future unified application structure:

Bellenne Shell
→ Proof
→ Section
→ Page

Do not build an unnecessarily isolated Proof-specific global shell.

Keep clear boundaries between:

Shared Bellenne UI

Proof product UI

Page-specific UI

---

## Implementation Priority

When making decisions, use this order:

1. Explicit current user instruction.
2. Existing working Proof architecture.
3. `AGENTS.md`.
4. `docs/BRANDBOOK.md`.
5. `docs/products/PROOF.md`.
6. Existing shared Bellenne components.
7. Minimal implementation necessary.

Do not replace working architecture merely because another approach appears cleaner.

---

## Before Completing Proof Work

Before considering any Proof task complete, verify that:

- Core and Worker responsibilities remain separated;
- backend remains the source of truth;
- no fictional workflow stages were added;
- no fictional settings were added;
- no arbitrary defaults were invented;
- Processing Status and Delivery Status remain distinct;
- retry behavior does not repeat unnecessary work;
- external integration failures are separated from processing failures;
- secrets are not exposed;
- file paths are not unnecessarily hardcoded;
- Worker architecture is not accidentally limited to one machine;
- shared Bellenne components are reused;
- Proof Product Accent is used correctly;
- no fake analytics or AI metrics were added;
- no unsolicited redesign occurred;
- the implementation remains compatible with the future Bellenne Shell.

Fix violations before reporting the task complete.

---

## Final Principle

BellenneProof coordinates work.

Proof Worker performs work.

Keep those responsibilities separate.

The system must always make it possible to answer:

What created this Job?

Where is it now?

Which Worker processed it?

What happened during processing?

Was the result created?

Was the result delivered?

If something failed, what exactly failed?

Prefer explicit state over hidden behavior.

Prefer traceability over convenience shortcuts.

Prefer reliability over architectural cleverness.

Same Bellenne ecosystem.

Same design language.

Different operational purpose.