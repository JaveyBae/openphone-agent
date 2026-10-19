# Offline calendar UI demo

## What is implemented

The main APP has a **日程演示** button and a confirmation dialog showing the
full preset. Tomorrow is computed in `Europe/Zurich`, not from the host's
implicit timezone. The event is `OpenPhone 项目演示 · YYYY-MM-DD`, 15:00–15:30,
location `线上会议`, with no participants or invitation. The default is **no
notification reminder**. A checkbox requests a 10-minute reminder only after
Fossify notification permission has already been granted.

This mode is a deterministic, state-checked UI skill. It does **not** call a
model, claim to stream reasoning, parse arbitrary natural-language dates, or
implement bookings with external businesses. The 9B service/tunnel is not
restarted. Ordinary tasks in `--provider preset` are explicitly rejected rather
than silently running a mock or unavailable model.

## Setup

- Android 15/API 35, authorized ADB, existing isolated shell workspace.
- [Official Fossify Calendar 1.12.0 release](https://github.com/FossifyOrg/Calendar/releases/tag/1.12.0),
  package `org.fossify.calendar`, APK `calendar-23-foss-release.apk`.
- Verified APK SHA-256: `d43b2df05d5702bea760a7ab03402977ba72e446e5932038efbdab248a5de987`.
  Signature certificate SHA-256: `affdb124d3f4720c2f98dbca9eacba0514fba4306e20a2786c861c3c0d6ff292`.
- The tested calendar UI is English; Chinese event text is supported. Unknown
  translated controls or unexpected fields pause safely.
- For a fresh installation, Calendar → Settings → disable **Use the last
  event's reminders as the default for new events** → set all **Default
  reminder** fields to **No reminder**. This avoids requiring notification
  permission just to save an event. Do not change personal/synced calendars.
- Keep the emulator timezone `Europe/Zurich`; a mismatch stops the preset rather
  than changing system settings or assuming the current UTC offset.
- Close Fossify on the main display before using it in the private workspace;
  same-app task duplication is not guaranteed.

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r runs/calendar-23-foss-release.apk
python3 scripts/chat_bridge.py --provider preset
# A separate terminal, if desired:
python3 scripts/display_viewer.py --both --interval 0.8
```

Only one bridge may own `runs/chat-bridge.lock`. Stop the foreground bridge with
Ctrl+C. No model API, new cloud resources, account login or persistent scheduler
is needed for this preset. Calendar notification permission is a separate user
choice: this implementation does not silently grant it.

## UI workflow and evidence

1. Validate the exact preset and acquire a single-run lock.
2. Check the live private session, isolation flags and main-screen focus.
3. Search the date-specific title. Multiple matches or uncertain empty results
   pause; existing matching events are inspected instead of duplicated.
4. For a new event, Android's INSERT intent opens the editor with explicit start
   and end timestamps; **the intent does not save a calendar record**.
5. Scoped accessibility SET_TEXT fills the title, location and description with
   exact readback. UI fields, all-day status, reminder and the non-repeating
   `Local calendar / Offline (never synced)` target are checked before Save.
6. Click the real calendar Save button, then search, reopen and verify the saved
   fields. A failed/uncertain Save is recorded and is never automatically replayed.
7. Return to the event list and report the verified result to the APP.

Runs and screenshots are under `runs/calendar-*`; each audit explicitly labels
`mode=deterministic_ui_preset`. Private UI calls revalidate package, session,
display, focused window and owner focus. Dialog navigation may briefly replace
the private focused window; the helper waits for it without changing global
focus or replaying the click. It never reads the owner's UI as planner input.

The first notification-enabled test was blocked by Fossify's notification
permission requirement and was not falsely reported saved. The conservative
no-reminder preset subsequently saved a real event. Reminders' eventual OS
delivery has not been tested; saving a reminder setting is not proof of delivery.

## Demonstration

Show the owner screen and private screen side by side. In OpenPhone, tap
**日程演示 → 创建日程**, then continue typing/scrolling on the main screen while
the private calendar is inspected. The result list shows the date-specific
Chinese title and 15:00–15:30 interval. Clicking again verifies the existing
event rather than silently creating duplicates. This is a UI skill demonstration,
not yet a full natural-language, model-driven calendar agent.
