# BellenneProof Core

BellenneProof is the orchestration module for color-proof production. It accepts
amoCRM events, persists versioned Jobs, atomically assigns queued work to
individually authenticated Workers, stores immutable per-attempt Results, and
tracks processing and delivery independently.

Proof Core never processes images. A Worker receives the opaque input and preset
snapshots through `/api/v1`, uploads the generated file, and reports completion.

## Worker flow

1. `POST /api/v1/workers/heartbeat`
2. `POST /api/v1/jobs/claim`
3. `POST /api/v1/jobs/{job_id}/start`
4. `POST /api/v1/jobs/{job_id}/progress`
5. `POST /api/v1/jobs/{job_id}/result` as multipart form-data with
   `Idempotency-Key`, `file`, and optional `metadata_json`
6. `POST /api/v1/jobs/{job_id}/complete` or `/fail`

Use `Authorization: Bearer <worker token>` or `X-Proof-Worker-Token`. Tokens are
generated in the Workers page and displayed in full once.

The production Worker is maintained as the separate `BellenneProofWorker`
service. When it runs on the same Docker host, attach it to the
`bellenneone_default` network and use `PROOF_CORE_URL=http://proof:8000`.
For a remote Worker, use the public module root, for example
`https://bellenne.example/proof`; do not append `/api/v1`. One Worker token and
one durable Worker state volume must belong to exactly one Worker instance.

For amoCRM Jobs, Core sends the full UNC order path from the selected custom
field and `input.layout_number` as an integer. Each Worker has its own UNC prefix
to read-only mount mapping on the Workers page. Worker validates the mapped
mount against its bootstrap allowlist before using it. The preset snapshot is
opaque to Core and must conform to the Worker version being deployed. The
reference production parameters live in `BellenneProofWorker/examples/preset.json`.

## amoCRM adapter contract

The incoming webhook accepts both the JSON contract shown in the Integrations
page and the standard amoCRM form webhook. For the amoCRM form, the webhook is
only a wake-up/event envelope: Core fetches the current lead by ID, rechecks its
pipeline/status and extracts exactly three custom fields selected in the UI:
the order UNC path, layout number and one additional identifier. Other lead
custom fields are not copied into the Job. `event_id` is the idempotency key.
Only explicitly configured `event_type` values create Jobs.

The Integrations page also stores the queued, completed and failed status IDs.
Status changes are separate integration events and cannot roll back an already
persisted Job. Direct delivery wraps the immutable Worker Result in a ZIP,
uploads it through the amoCRM file-service session API, and adds an `attachment`
note to the lead. It then clears exactly two configured fields from the same
three-field mapping and applies the completed status in one lead PATCH.

The uploaded file UUID, version UUID and note ID are persisted separately from
the immutable Result. If creating the note or finalizing the lead fails, Retry
Delivery resumes from the last durable stage rather than re-running Worker or
uploading the same archive again. Before creating a note, Core also searches for
an existing attachment with that file UUID to tolerate a lost API response.

The optional legacy webhook-adapter mode posts multipart form-data to the
configured integration endpoint:

- `payload_json`: Job, CRM entity, Result metadata, and authenticated download path;
- `file`: the generated Result itself.

The adapter does not receive the amoCRM access token. Its endpoint must respond
with a 2xx status. A failed delivery leaves processing
as `completed`, sets delivery to `failed`, and can be retried without invoking a
Worker again.

## Mattermost error notifications

Configure a Mattermost Incoming Webhook on the Proof **Integrations** page. The
webhook URL is encrypted at rest and is never rendered back to the browser or
written to logs. An optional channel name (or `@username` for a direct message)
can override the channel selected when the webhook was created.

Every persisted Proof event with level `error` or `critical` creates one durable
notification outbox item. Delivery happens after the business transaction is
committed, so an unavailable Mattermost server cannot roll back a Job or Worker
state change. Successful events are intentionally not sent to avoid chat noise.
After saving the webhook, use **Отправить тестовое уведомление** to verify delivery.
The test uses the saved settings and does not save edits currently entered in the form.
