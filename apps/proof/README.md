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

## amoCRM adapter contract

The incoming webhook accepts the JSON contract shown in the Integrations page.
`event_id` is the idempotency key. Only explicitly configured `event_type`
values create Jobs.

Delivery posts multipart form-data to the configured integration endpoint:

- `payload_json`: Job, CRM entity, Result metadata, and authenticated download path;
- `file`: the generated Result itself.

The endpoint must respond with a 2xx status. A failed delivery leaves processing
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
