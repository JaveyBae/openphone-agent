# Focus-isolated workspace

## Why the previous display was insufficient

The ordinary APP's MediaProjection display had neither independent focus nor
top-focus-stealing suppression. `input -d ID` selects a destination but does not
itself establish resource isolation. Two Back actions during the 2026-10-04 JD
regression changed InputDispatcher's global `FocusedDisplayId` from 0 to 4.
Automatically switching back would hide, not fix, the conflict.

## ADB-assisted backend

`tools/workspace/WorkspaceServer.java` is built into a small DEX JAR by
`scripts/workspace_runtime.py`, uploaded to a fixed shell-owned directory, then
started as UID 2000 via `app_process`. It uses permissions already held by the
authorized shell; it grants nothing to the APP, starts no network command
listener, and never captures the owner display.

The Android 15 display requests PUBLIC, PRESENTATION, OWN_CONTENT_ONLY, TOUCH,
TRUSTED, OWN_FOCUS, STEAL_TOP_FOCUS_DISABLED and DESTROY_CONTENT_ON_REMOVAL.
The host verifies `removeMode 1` so losing the display destroys its private
tasks rather than moving them onto the owner's main screen. The Java helper checks actual
`Display.getFlags()`. Before every action the host independently matches the
live display ID/name/shell UID and all isolation flags in `dumpsys display`.
`DisplayInfo.toString()` omits the focus flags, so checking `cmd display` alone
is insufficient. The values and permission checks follow the
[Android 15 DisplayManager source](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/android15-release/core/java/android/hardware/display/DisplayManager.java)
and [Display source](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/android15-release/core/java/android/view/Display.java).

ImageReader produces session-specific, atomically replaced private PNGs. A
coalescing capture reads the newest frame instead of dropping a static page's
final render. Stale IDs, missing flags, unknown content and malformed sessions
fail closed; the viewer never falls back to the owner's frame. The local
`--both` preview is read-only. No automatic owner-focus restoration is used.

The workspace cannot show an IME with these flags. ASCII is injected as
display-targeted key events only when both the owner and private display have
focused windows and global focus remains 0. Unicode uses a short-lived shell
`UiAutomation` helper and `ACTION_SET_TEXT`, not key simulation. It enumerates
window metadata by display but retrieves **only private-display roots**, then
requires one focused, visible, enabled, non-password editable node belonging to
the private top-resumed allowlisted package. The private session and owner focus
are rechecked on-device before writing and during exact text readback. Failure
never falls back to the owner, an IME, clipboard or key injection.
Existing accessibility services are not suppressed; the connection is removed
after every attempt. The text helper JAR is built/pushed separately without
restarting the display or its app tasks. Owner Gboard/Chinese composition is not
reused. Global Home/Power keys are
forbidden. Task-progress scrolling does not move the chat input's focus.

## Regression evidence (2026-10-04)

- API 35 ARM64 emulator, shell workspace Display 5: actual flags `6280` include
  trusted/own-focus/no-steal. Session IDs and display IDs are dynamic.
- Private browser tap and ASCII `5090`: before/after global focus 0, focused
  windows on both 0 and 5. No owner activity switch was issued by the controller.
  `runs/20261004-051129-focus-check` also contains the before/after PNGs proving
  the actual address field received `5090`; no address submission was performed.
- Run `runs/20261004-050548`: explicit JD setup successfully placed JD on
  Display 5; model saw JD loading, then JD security verification and requested
  human takeover. No repeated Back loop, login, agreement, purchase or payment.
  This proves opening/isolation, **not** completed price comparison.
- Repeat launch resumes the existing private JD task rather than restarting its
  entry activity. `runs/20261004-051139` verified JD was resumed and focus stayed
  0. The model suggested a verification tap this time; the host safety guard
  rejected it before execution. Model instructions alone are not a safety boundary.
- Unit regressions cover identity/flag loss, display 0, focused-window scoping,
  verified Unicode input/unsupported-field refusal, global-key refusal, stale
  frame paths, target routing and no automatic
  focus restoration. The debug APK builds with Gradle.

## Compatibility and lifecycle

Android 15+ and a ROM permitting the shell's trusted virtual displays are
required for this backend; only the emulator has been verified so far. The
ordinary APP/MediaProjection path remains legacy read-only. Third-party task
reuse is still a compatibility limitation, not solved by changing focus flags.

The host bridge ensures the helper exists before each new task and broadcasts
its verified ID to the APP. Installing an APP update does not stop the separate
shell display. Reboots/disconnection/process death can invalidate it; new tasks
recreate it, but failed tasks are never automatically replayed. For an explicit
stop use `python3 scripts/workspace_runtime.py stop`; it checks the exact helper
PID/cmdline and preserves logs/build/source artifacts.

### Lifecycle follow-up

An attached host-side ADB shell received SIGHUP and lost Display 5 on 2026-10-04.
Android's previous `removeMode 0` migrated the private browser to Display 0.
The runtime now starts `nohup app_process` **on the device** with detached
stdin/stdout/stderr; the live helper is UID 2000, parent PID 1 and ignores HUP.
This is not a boot-persistent service and grants no new permission.

The removal regression created Display 6 with only the private test browser,
then stopped its verified helper. The private activity was destroyed, the
owner launcher stayed resumed on Display 0 and global focus remained 0. The
already logged-in JD task was left untouched during this test, then moved by
explicit user approval to the restored Display 7. Its top-resumed activity was
verified there before any model task. This approved handoff is separate from
normal automatic launch; the launch guard still refuses owner-display tasks.

### Unicode input follow-up

The model already emitted `Type(text="5090显卡")`; the previous adapter stopped
because of its ASCII-only transport. Unicode now passes through strict literal
parsing to the bounded text helper. Control characters and lone surrogates are
rejected, and Unicode uses replacement (not appending or an implicit submit).
On the API 35 emulator, writing `5090显卡` to JD's Display 7 search field succeeded
with exact accessibility readback; the private screenshot also shows the full
query. Global input focus stayed 0 and both displays retained focused windows.
Live negative tests also rejected display 0, a stale session and an unsupported
package before connecting or writing. Real-model runs `runs/20261004-150427` and
`runs/20261004-150603` executed native Chinese Type actions successfully; their
final completion strings were rejected for malformed quotes / duplicate calls.
A bounded completion-only formatting retry now asks the model for one valid
finish call; it cannot introduce or replay a UI action and retains both outputs.
Final run `runs/20261004-151051` completed normally: the real model emitted
`Type(text="5090显卡")`, the helper verified the exact text, and the next private
screenshot led to one valid finish response. Before/after global focus was 0
with focused windows on both 0 and 7. No search submission occurred. The 74 unit
tests pass, including failed readback/identity, control characters, no input
fallback, and the completion retry's no-action / one-retry limits.
This tests that field, not every custom WebView/secure editor or concurrent
human composition. Passwords, verification, agreements and transactions still
require user action. Unsupported editors stop instead of borrowing the main IME.

API references: [SET_TEXT](https://developer.android.com/reference/android/view/accessibility/AccessibilityNodeInfo.AccessibilityAction#ACTION_SET_TEXT)
and [per-display windows](https://developer.android.com/reference/android/app/UiAutomation#getWindowsOnAllDisplays()).

### Messages draft regression (2026-10-05)

The APP task `给183492发短信，内容是：晚会儿到。` exposed two independent
operator/adapter failures: the Save-contact card's unlabeled clickable wrapper
was rejected, and the model kept tapping a focused composer while waiting for
an IME that intentionally never appears on the private display.

Unknown SMS controls still never receive a coordinate fallback. Only a verified
private-session rejection with `executed=false` permits at most two replans.
The supported Save-card close adapter requires the exact title/explanation,
Google Messages package, a small top-right child, and its `Close` icon plus
anonymous Button children. Neighboring buttons and the whole card remain
rejected. Fresh node clicks and Unicode writes retain session/window/focus checks.
Successful editor clicks now report verified focus to the planner, so it can
Type Chinese directly rather than trying to summon the owner's keyboard.

`runs/20261005-010056` records the real model closing the card, safely replanning
an unknown Back control into system Back, starting a conversation and selecting
the literal test recipient. `runs/20261005-010247` records a verified composer
click followed by native Chinese Type `晚会儿到。`; `step-02.png` shows the unsent
body and recipient. Global focus remained 0, with focused windows on 0 and 7.
That run then failed on unescaped quotation marks in the model's completion;
its in-context formatting retry repeated the same malformed conclusion.

For other tasks the single completion-only retry may return only a parsed
finish call, never a UI action. SMS instead converts malformed finish-only
output to a static manual-inspection pause, without trusting its success claim
or replaying an action. Valid SMS finish is also converted to manual-owner handoff.
`runs/20261005-010431` verified the existing draft without rewriting it and
returned `awaiting_user` to the APP. The final-code regression
`runs/20261005-010750` reproduced a malformed finish and verified the static
`sms_unparsed_completion_handoff` pause with no second API call or phone action;
it does not label an unparsed response as successful completion.
`runs/messages-183492-unsent.png` is the
final private-screen evidence. A read-only check still rejected the actual Send
button. No SMS was sent; the six-digit recipient is a literal draft test, not
evidence of a valid deliverable phone number. The 99 unit tests pass. A separate
read-only formatting experiment (`runs/sms-completion-format-readonly.model.json`)
replayed an earlier malformed response and attempted a real text-only repair;
the model returned no text, so that unproven approach was not retained.

This is a version-specific Google Messages adapter on the API 35 emulator,
not a claim of universal SMS-app support or real cellular delivery. Main-display
Messages must be closed before launch to avoid task migration; existing drafts
are preserved, and all actual sending remains manual.
