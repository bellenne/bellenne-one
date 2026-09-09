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

Configure amoCRM on the Integrations page in four independent steps: OAuth,
webhook field mapping, lead statuses, then the Worker preset and activation.
Register these public endpoints in the amoCRM integration:

```text
Redirect URI: https://one.customcraft-mes.ru/proof/integrations/amocrm/oauth/callback
Revocation hook: https://one.customcraft-mes.ru/proof/integrations/amocrm/oauth/revoked
```

The integration needs access to account/CRM data and the file scope. Core stores
the OAuth client secret, access token and rotating refresh token encrypted. It
validates the one-time OAuth state, confirms the returned account, and refreshes
the token before expiry. `PROOF_PUBLIC_BASE_URL` must contain the public HTTPS
origin and the redirect URI registered in amoCRM must match the displayed value
exactly.

The standard amoCRM form webhook is only a wake-up/event envelope. All business
conditions belong to the amoCRM automation that sends it; Core deliberately does
not duplicate a source pipeline/status filter. Core fetches the current lead by
ID and extracts exactly three custom fields selected in the UI: the order UNC
path, layout number and one additional identifier. Other lead custom fields are
not copied into the Job. The derived event ID is the idempotency key.

The Integrations page also stores the queued, completed and failed status IDs.
After persisting a new Job as `received`, Core clears the selected one, two, or
three fields and applies the queued status in one lead PATCH. Only after that
acknowledgement succeeds does the Job enter the Worker queue. If the PATCH fails,
the Job remains `received`; a duplicate delivery retries the acknowledgement
without creating another Job or rereading the fields that may already be empty.

Direct delivery wraps the immutable Worker Result in a ZIP, uploads it through
the amoCRM file-service session API, and adds an `attachment` note to the lead.
After the attachment exists, Core applies the completed status. The final step
does not clear the source fields again.

The uploaded file UUID, version UUID and note ID are persisted separately from
the immutable Result. If creating the note or finalizing the lead fails, Retry
Delivery resumes from the last durable stage rather than re-running Worker or
uploading the same archive again. Before creating a note, Core also searches for
an existing attachment with that file UUID to tolerate a lost API response.

The internal JSON webhook contract and legacy webhook delivery mode remain
available for compatibility. Legacy delivery posts multipart form-data to the
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
