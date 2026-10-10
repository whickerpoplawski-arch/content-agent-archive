# XHS Agent Console Design

Date: 2026-08-11
Status: Approved design, pending implementation

## Objective

Add an Agent console that makes autonomous behavior observable and controllable. It must clearly distinguish the autonomous Agent from the existing deterministic workflow pages.

The console lets a user provide a goal and constraints, observe planning and tool selection in real time, inspect reflection and replanning, review generated artifacts, stop execution, approve external side effects, and revisit prior runs.

## Scope

The first version includes:

- A new `static/agent_console.html` page.
- A vertical Agent toolbox ordered by workflow capability.
- A goal-oriented conversation area.
- A real-time execution trace delivered through SSE.
- Run creation, status, stop, approval, history, and recovery APIs.
- Persistent run events and outputs.
- Explicit approval before publishing or Feishu synchronization.
- Read-only history for the latest 50 runs.

The first version does not include:

- Editing a running plan through bidirectional chat.
- Resuming execution from an arbitrary failed step.
- Replacing or removing the existing workflow pages and APIs.
- WebSocket transport.

## Product Layout

The approved layout uses three columns on desktop:

1. A compact vertical Agent toolbox.
2. A goal and conversation panel.
3. An execution and result panel.

On narrower viewports, the toolbox remains fixed to the left while conversation and execution stack vertically.

### Agent Toolbox

The toolbox is ordered from top to bottom:

1. Crawl
2. Analyze
3. Topics
4. Generate
5. Images
6. Publish
7. Feishu

The toolbox represents Agent capabilities, not links to legacy workflow pages.

Each item displays one of these states:

- Available
- Disabled by user or policy
- Waiting
- Running
- Completed
- Failed

Clicking an item opens the latest invocation details, including parameters, result summary, duration, and error information. A user can enable or disable tools before creating a run. Tool state changes do not modify a run already in progress.

### Conversation Panel

The conversation panel contains:

- The user goal and constraints.
- Agent summaries of its plan and significant decisions.
- Reflection and replan explanations.
- Audience and tone settings.
- A new-task composer.

The panel does not expose hidden chain-of-thought. It displays concise, auditable decision summaries derived from structured Agent events.

New constraints entered while a task is running apply only to a newly created task.

### Execution Panel

The execution panel has three tabs:

- Execution trace
- Final artifacts
- Run metrics

The trace displays planning, steps, tool calls, observations, reflection, replanning, quality evaluation, approvals, failures, cancellation, and completion.

The artifact tab displays analysis findings, topics, generated posts, image outputs, publishing previews, publishing results, and Feishu synchronization results when available.

The metrics tab displays elapsed time, model call count, estimated tokens, tool-call counts, stop reason, and backend/model information.

### Run History

A history entry near the top of the toolbox opens the latest 50 runs. Each item shows goal, status, creation time, duration, and artifact count.

History is read-only. A user can copy the goal and settings into a new run. Failed runs cannot continue from the failed step in the first version.

## Safety Policy

Publishing and Feishu synchronization are side-effecting operations and require explicit approval.

- Publish is disabled by default.
- If a goal requests publishing, the Agent may prepare content, images, and a publishing preview.
- Before the publish tool executes, the run enters `waiting_for_approval` and emits `approval_required`.
- The user must confirm or reject the action in the console.
- Feishu synchronization follows the same approval process.
- Rejection marks that step skipped and allows the Agent to reflect on whether the remaining goal is complete.
- Stop prevents new steps from starting. An in-flight external request may finish, but its result is recorded and no subsequent step starts.

The approval payload must show the exact operation, destination, titles or record count, and referenced local files before confirmation.

## Backend Architecture

The existing `POST /agent/run` deterministic workflow and `POST /agent/autonomous` compatibility endpoint remain unchanged.

A new run-management layer owns asynchronous Agent tasks, state, cancellation flags, approval waits, event publication, and persistence.

### API

#### Create Run

`POST /agent/runs`

Request fields:

- `goal`: required string
- `audience`: optional string
- `tone`: optional string
- `backend`: `openai` or `mock`
- `enabled_tools`: optional list of tool names

Returns `202 Accepted` with `run_id`, initial status, and URLs for status and events.

#### Stream Events

`GET /agent/runs/{run_id}/events`

Returns `text/event-stream`. Events include monotonically increasing sequence numbers. The endpoint accepts `Last-Event-ID` so reconnecting clients receive missed events before live events.

#### Get Run

`GET /agent/runs/{run_id}`

Returns metadata, status, plan, events, outputs, usage, tool-call counts, pending approval, and error information.

#### Stop Run

`POST /agent/runs/{run_id}/stop`

Sets a cooperative cancellation flag. It is idempotent.

#### Resolve Approval

`POST /agent/runs/{run_id}/confirm`

Request fields:

- `approval_id`
- `approved`: boolean

Rejects stale or mismatched approvals.

#### List Runs

`GET /agent/runs?limit=50`

Returns newest-first run summaries. The server caps the limit at 50.

## Run States

Supported run states:

- `queued`
- `running`
- `waiting_for_approval`
- `stopping`
- `stopped`
- `completed`
- `failed`

Terminal states are `stopped`, `completed`, and `failed`.

## Event Model

Every event includes:

- `id`: monotonically increasing integer within the run
- `run_id`
- `type`
- `timestamp`
- `data`: event-specific object

Supported event types:

- `run_started`
- `plan_created`
- `step_started`
- `step_completed`
- `tool_started`
- `tool_completed`
- `reflection`
- `replan`
- `quality_evaluation`
- `approval_required`
- `approval_resolved`
- `step_failed`
- `run_stopping`
- `run_stopped`
- `run_completed`
- `run_failed`

Heartbeats are SSE comments and are not persisted as domain events.

## Agent Integration

The existing planner, executor, reflector, quality gate, memory, budget, and tool registry remain the execution core.

They gain an optional event sink interface. Components emit structured events at meaningful boundaries without depending on FastAPI or SSE. The existing file tracer consumes the same events, avoiding duplicate observability logic.

The run manager builds a registry filtered by `enabled_tools`. Publish and Feishu tools remain unavailable unless explicitly enabled. Even when enabled, the approval guard intercepts them before execution.

The frontend never renders model chain-of-thought. Planner rationales and reflector reasons are treated as concise operational summaries.

## Persistence And Recovery

Each run is persisted to `data/agent/runs/<run_id>.json` throughout execution using atomic replacement. The file contains metadata, current state, events, plan, outputs, usage, tool-call counts, pending approval, and errors.

On application startup:

- Completed, stopped, and failed runs remain unchanged.
- Runs persisted as queued, running, stopping, or waiting for approval are marked failed with an interruption reason because the original Python task no longer exists.
- The console restores the last selected run through the run detail endpoint.
- A browser reconnect uses `Last-Event-ID` to recover events missed during a page refresh or temporary disconnect.

Only the latest 50 runs are returned in history. Older files are retained until manually removed; the API does not delete them automatically.

## Error Handling

- Recoverable tool failures continue through the existing retry or replan behavior.
- Every failed tool call emits `step_failed` with a user-safe error summary.
- Irrecoverable errors emit `run_failed` and persist partial outputs.
- Invalid run IDs return `404`.
- Conflicting approval requests return `409`.
- A disconnected SSE client does not stop the Agent task.
- Slow SSE consumers receive persisted missed events after reconnect.
- Malformed or unsupported tool selections return `422` before task creation.

## Frontend Behavior

The console uses the existing static HTML approach and does not introduce a frontend framework.

JavaScript responsibilities are separated into small modules or focused objects for:

- API requests
- SSE connection and reconnection
- Local run state reduction
- Toolbox rendering
- Conversation rendering
- Timeline rendering
- Artifact rendering
- History rendering
- Approval dialog

The UI uses Lucide icons if added as a local or CDN dependency consistent with deployment constraints. Buttons use icons for stop, history, tool controls, and detail expansion.

The console is linked from the main index as the primary autonomous-Agent entry. Existing crawl, generate, publish, and automatic workflow pages remain accessible as manual workflow tools.

## Testing

Backend tests cover:

- Run creation and validation
- Event ordering and sequence IDs
- SSE replay through `Last-Event-ID`
- Successful completion
- Cooperative stop
- Publish and Feishu approval
- Approval rejection and stale approval IDs
- Enabled-tool filtering
- Retry and replan event emission
- Persistence and startup interruption handling
- History ordering and limit

Frontend verification covers:

- Desktop and mobile layout
- Long Chinese goals and output text
- Live event updates without layout shifts
- SSE reconnect behavior
- Tool detail expansion
- Stop and approval controls
- Empty, running, completed, stopped, and failed states

Mock backend tests provide deterministic execution. One focused real-backend smoke test is optional and must not publish or synchronize data.

## Acceptance Criteria

The feature is complete when:

- A user can open the Agent console and create an open-ended goal.
- Planning and execution events appear incrementally without waiting for completion.
- Tool states update in the approved workflow order.
- Reflection, replanning, quality evaluation, failures, and stopping are visible when they occur.
- Publish and Feishu actions cannot execute without explicit confirmation.
- Refreshing the page restores the selected run and missed events.
- The final artifacts and metrics are inspectable in the console.
- The latest 50 runs are available as read-only history.
- Existing workflow endpoints and pages continue to operate.
