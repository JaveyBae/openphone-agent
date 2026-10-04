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
TRUSTED, OWN_FOCUS and STEAL_TOP_FOCUS_DISABLED. The Java helper checks actual
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
focused windows and global focus remains 0. Unicode stops for human input;
owner Gboard/Chinese composition is not reused. Global Home/Power keys are
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
  Unicode/global-key refusal, stale frame paths, target routing and no automatic
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
