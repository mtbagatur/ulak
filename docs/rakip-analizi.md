# HerdrChat and T3Code source review for Ulak

Reviewed locally on 2026-11-22. HerdrChat is at `f7e00c6` (Apache-2.0); T3Code is at `e6d52101` (MIT). This is a source-level product and interaction review, not a runtime security audit. No source was copied. Paths below are relative to each repository root.

## HerdrChat

HerdrChat is an Expo iOS/Android client that connects to the user's machine over SSH. Its main mental model is workspace → chat/pane → transcript, with terminal, files, workflows, and delegated agents as adjacent surfaces. The mobile client owns the presentation and local state; the remote host remains the source of truth for agent sessions.

### Sessions and transcript

- `src/features/chats/ChatsList.tsx`, `ChatRow.tsx`, `SpaceCard.tsx`, `PaneRow.tsx`, and `chatListRows.ts` compose a workspace-oriented list. Status, unread/attention, pinned/muted state, workspace identity, and child panes are distinct signals. A whole row gets one accessible label rather than exposing every decorative subview to VoiceOver/TalkBack.
- `src/features/thread/ThreadScreen.tsx` binds a workspace or pane to its host, transcript, composer, terminal, overlays, and delegation scope. It retains per-chat drafts outside the screen so navigation does not discard partially written input.
- `src/features/thread/useTranscript.ts` seeds from a local SQLite cache, reads a bounded recent window, follows a byte-offset tail while work is live, appends unseen message IDs, and catches up once the agent stops. If the stream drops (often after the phone sleeps), it surfaces a reconnect message and resumes from the last consumed offset. An absent subagent transcript is retried only while the parent remains live.
- `src/features/thread/ThreadMessages.tsx` virtualizes the transcript with FlashList. Older pages are fetched after the reader interacts and approaches the top. Visible rows stay anchored when history is prepended; a stable “older history” row avoids bottom-alignment jumps. A failed send gets a retry control, and live output/working status are separate footer elements.
- `src/features/thread/useThreadScroll.ts` explicitly distinguishes reader-driven scrolling from list measurement/re-anchoring. It follows new content only when the reader is following the end and shows a jump control when they are away. `JumpToBottom.tsx` strengthens the cue when unread output exists.
- `src/features/thread/Composer.tsx` uses a floating, reachable composer, preserves selection/caret behavior, supports attachments and hardware keyboard shortcuts, and disables sending during upload/send. `ThreadScreen.tsx` reserves measured room for the floating composer and handles iOS keyboard dismissal separately from Android.

### Approval and choice prompts

- `src/features/thread/useBlockedPrompt.ts` reads the visible terminal buffer instead of guessing from a status flag: some menus await input while the agent still reports idle. It distinguishes Claude trust prompts, Codex's file picker, and slash-command overlays.
- `src/lib/transcript/blockedPrompt.ts` parses choices and produces a signature for each prompt. `BlockedBar.tsx` displays actual option text, not just numbers; when parsing cannot safely identify choices it falls back to generic key controls. Unsupported choices remain visible but disabled so the terminal's numbering is not silently changed. Multi-select has an explicit Continue action.
- `src/features/thread/PromptDetails.tsx` keeps the command/diff readable, folds long details with a line count, and supports copying the full command. Details are keyed to the prompt signature so a new question does not inherit the prior question's collapsed state. It warns when the relevant command begins off-screen.
- `useBlockedPrompt.ts` marks an answer pending synchronously before sending keys, preventing a second tap before React rerenders. It waits for the next observed prompt/state, checks again soon after a reply (a follow-up prompt can appear immediately), and reports a timeout instead of implying the input succeeded.
- The prompt comes from the host's live terminal screen. This makes parsing inherently provider/version-sensitive; Herdr's tests in `src/features/thread/__tests__/BlockedBar.test.tsx` and transcript parser tests are important regression assets.

### Notifications, navigation, multi-agent, and security

- `src/features/notifications/push.ts`, `useWatcher.ts`, and `useNotificationRouting.ts` implement remote notifications and open the corresponding workspace/pane. The push path includes an external relay; treat it as a trust boundary and verify exactly which payload fields are sent before adopting the pattern. Ulak should not route transcript text, commands, or private project names through a third party.
- `src/features/thread/AgentsBar.tsx`, `SubagentCard.tsx`, and `SubagentScreen.tsx` show sibling agents and delegation as a navigable hierarchy; `WorkflowCard.tsx`/`WorkflowScreen.tsx` give longer-running workflows a distinct presentation. The transcript scope is tied to the actual open session paths rather than inferred from a directory name.
- `src/features/newChat/NewChatScreen.tsx` starts chats from a host/workspace flow; `src/features/thread/StopButton.tsx` and `ThreadControls.tsx` expose stop and session controls. A missing host is a separate read-only/error state, not a composer that looks enabled but cannot send.
- `src/lib/hostkey.ts` normalizes OpenSSH SHA-256 fingerprints and preserves a pin across cosmetic host edits; endpoint changes require a new observed fingerprint. `src/state` stores device credentials through secure storage, and its tests check device-only keychain accessibility and interrupted migrations. The transport is direct SSH, not an app-owned transcript relay. This improves data locality but makes host reachability, SSH configuration, and key-change recovery user-visible concerns.
- `src/features/thread/useThreadConnection.ts`, `useThread.ts`, `useTranscript.ts`, and `src/lib/poll.ts` separate connection/poll failures from transcript content. `src/lib/poll.ts` backs off and stops repetitive failed SSH attempts for conditions such as missing credentials or a changed host key.

## T3Code

T3Code is a much broader multi-platform agent client. Its mobile app is an Expo client over a shared typed client-runtime/contracts layer; the web app has a richer timeline and composer. The shared runtime normalizes provider requests and connection state, while platform surfaces choose compact or expanded presentation.

### Sessions, timeline, and multi-agent

- Mobile entry points include `apps/mobile/src/features/home/` (thread list), `features/threads/ThreadRouteScreen.tsx`, `ThreadDetailScreen.tsx`, `ThreadFeed.tsx`, and `ThreadComposer.tsx`. `NewTaskRouteScreen.tsx` and `NewTaskDraftScreen.tsx` cover task/session creation. The route binds a selected environment and thread; it does not assume one local provider or one session format.
- `ThreadDetailScreen.tsx` renders a mixed activity feed and owns run controls. `thread-feed-scroll-position.ts` preserves a work-group row plus its intra-row offset and chooses an anchor when a message is submitted. This is a useful invariant for keeping the reader's place while asynchronous events arrive.
- `PendingApprovalCard.tsx` presents request type, app, detail, reason, provider warning, and provider-supplied options. A request whose provider process is gone is explicitly non-actionable; inability to operate the thread is also shown. `ThreadAgentsSheet.tsx`, `SubagentRow.tsx`, and `thread-subagent-group.tsx` surface child work without flattening every child into a separate top-level thread.
- On web, `TimelinePendingApprovalRow.tsx` renders approvals in the timeline, while `ComposerPendingApprovalActions.tsx` places the common decisions inline and moves less common decisions into a “More” menu. Warnings are attached to their corresponding choice; the source label is preserved for provider-authored options. This keeps the frequent decision visible without hiding the exceptional choices.
- `ThreadDetailScreen.tsx` distinguishes a graceful stop request from force-stop. Force stop is described as terminating the agent processes and has a confirmation dialog, especially because processes may be shared by other working threads.

### Offline delivery, connection state, and notifications

- `apps/mobile/src/state/thread-outbox-manager.ts` persists queued messages and serializes mutations. It handles partial recovery, optimistic enqueue rollback, revision races, and storage failures. `threadOutboxDelivery.ts` models delivery as stages with retryable transport failures separated from decode/logic failures; attachments and settings have their own recovery paths. This is a strong model for a durable offline send queue, but it is substantially more machinery than Ulak needs unless users are losing composed messages during outages.
- `state/workspaceModel.ts` separates `offline`, `reconnecting`, `connecting`, `error`, and `connected`, and can retain a shell snapshot while accurately saying that the environment is not ready. `features/connection/EnvironmentConnectionNotice.tsx` and `useRetryEnvironment.ts` expose recovery rather than turning stale content into a false “connected” state.
- `features/agent-awareness/notificationPayload.ts` validates and normalizes thread deep links, rejects malformed paths, and deduplicates tapped notification IDs. `notificationResponseConsumer.ts` handles both cold-start and live responses and clears the stored response only after routing succeeds. `foregroundNotificationBehavior.ts` suppresses a notification for the thread already on screen and otherwise permits banner/list/sound.
- This notification system is mobile OS push infrastructure and thread deep-link routing; it is not evidence of a tailnet-only deployment or of a no-third-party notification path. Ulak's local Web Push deployment has a different trust model and should state it plainly.

### Security model and limits

- The code is organized around paired environments, typed server/client contracts, scoped thread operations, provider capabilities, and explicit response capability. Approval availability is modeled as data (`live` vs unavailable) rather than inferred from a button's appearance. Relevant code is split across `packages/contracts`, `packages/client-runtime`, `apps/mobile/src/connection/`, and `apps/mobile/src/features/threads/`.
- This review did not audit server authentication, encryption-at-rest, cloud retention, or the full permissions model. Do not treat the UI client as proof of those properties. T3's broad environment/provider integration is valuable for portability; Ulak can remain simpler and more private by staying local/Tailscale-first.

## Feature × competitor × Ulak

| Capability | HerdrChat | T3Code | Ulak today / opportunity |
|---|---|---|---|
| Session overview | Workspace → panes, per-agent status/unread | Environment → thread list, broad filters | Unified Claude/Codex/agy/terminal cards; retain the single-pane simplicity and sharpen attention/unread state |
| Transcript | SQLite cache + recent window + byte-tail; virtualized history | Shared typed activity feed; rich web and mobile timeline | Compact server transcript with tool rows; add stable paging/anchor only if history limits become a real pain |
| Approval | Parses actual terminal prompt; duplicate-answer guard and timeout | Normalized typed request, live capability, explicit provider warning and choice labels | Strong destructive-command guard and confirmation token; in-flight duplicate guard and truthful “reply sent” feedback added locally; agent-side acceptance confirmation remains open |
| Notifications | External relay in the push path; deep links to host/workspace | OS push with validated thread deep links, cold-start dedupe and foreground suppression | Local PWA Web Push and event stream; keep payload minimal and make relay/provider boundaries explicit |
| Mobile navigation | Native workspace/pane screens, swipe/list actions | Native environment/thread navigation and deep links | PWA, home screen, sheets; latest pass adds keyboard semantics, visible focus and safe-area support |
| Start/stop | Host/workspace new-chat flow; stop controls | Environment-scoped new task; graceful stop vs confirmed force stop | Create session and guarded close; keep confirmation command-visible and make the distinction between turn stop and session close explicit |
| Multi-agent | Workspace pane bar, subagent cards/screens, workflows | Child threads, grouped agents, shared work/run concepts | All tool sessions in one list with Main orchestration; make parent/child links visible only when they help act |
| Offline/reconnect | SSH polling/backoff and transcript-tail resume | Retained snapshots, typed reconnect state, persisted outbox | SSE retry and current network/error feedback; draft persistence exists, but no durable send outbox |
| Security model | Direct SSH, pinned host keys, device secure storage; external push relay is a boundary | Paired environments, typed capabilities, provider/runtime boundary; broader cloud/security scope not reviewed | Tailnet-only operator deployment, local Python backend, the user-specific guard/guard, minimal Web Push payload |
| Accessibility | Native accessibility labels/states, touch targets, haptics | Native/web roles, request capability and choice warning | Web semantics; modal sheet dialog/focus handling was missing and is now fixed locally in this review |

## Where Ulak can be better than both

1. **One honest operator surface.** Keep Claude, Codex, agy, and terminal sessions visible together; preserve each tool's actual pending request and do not convert a parser guess into an approval action.
2. **Safety that is visible at decision time.** Show the complete command, why a hook asked, destructive status, and a second confirmation token in the same flow. Prevent repeat answers while a request is pending and say when delivery could not be confirmed.
3. **Privacy by deployment.** Keep the backend and session content on the user's machine behind Tailscale; send only the minimum routing/urgency fields needed for Web Push. Do not adopt Herdr's relay topology without an explicit privacy review.
4. **the user's policies as product behavior.** Guard/guard and local approval rules can be first-class, understandable decision cues rather than another generic provider permission screen.
5. **Turkish-first, small-screen speed.** Preserve the quick list and one-hand action model. Use targeted patterns from the competitors; do not copy their larger product surface or turn Ulak into an agent IDE.

## Recommended Ulak work, ordered by value

1. **Finish verification for the current design pass.** Run project tests and static checks; the user still needs to verify the most recent contrast, keyboard focus, reduced-motion, and horizontal safe-area changes on iPhone. Desktop device emulation is not a substitute for that step.
2. **Approval feedback state.** The synchronous per-pane guard now blocks duplicate approval, choice, and key actions while an HTTP request is in flight, marks controls `aria-disabled` without dropping keyboard focus, and reports “Reply sent” instead of claiming the agent accepted it. Still open: observe prompt change and report a bounded timeout if the agent does not react. Herdr's ref-based guard is directly relevant; T3's live/unavailable capability model is a good state vocabulary.
3. **History resilience only when needed.** If Ulak begins limiting transcript history, add stable anchors and explicit older-history paging. Herdr demonstrates the approach; T3 shows row-level anchors. Avoid adding a full offline outbox until a real lost-send case justifies its complexity.
4. **Accessibility semantics for every transient surface.** This review fixed dialog role, focus entry/return, Escape, and Tab containment for sheets. Next verify the interaction on Safari/VoiceOver and test its large-content scrolling on phone.
5. **Privacy and connection copy.** Document where push is delivered and what fields it carries; distinguish stale cached content, disconnected state, and active connection without implying readiness.

No `NOTES.md` roadmap file exists in this checkout, so this report holds the prioritized v2 recommendations until a roadmap is deliberately added. No UI/code from either repository was copied; the review records patterns and source locations only.
