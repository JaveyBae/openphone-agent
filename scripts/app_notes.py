"""How the installed apps work, for every planner's prompt (AutoGLM, Qwen, ...).

Knowledge about the apps, not a script: what a control does, which dialog mode is easier,
what must stay a draft. One copy, so every model gets the same help.
"""

APP_NOTES = """Calendar (日历) is Google Calendar, synced to the owner's real account. Create only the
event that was asked for; never edit or delete existing events. Never add guests or
invitees (Google would email them): if guests are requested, use Take_over. To create an
event: tap the + button, choose Event, type the title, set the date and the start and end
times, then tap Save. Use the event's default calendar and do not change its settings.
For a place, tap "Add location", Type the place, then press the Enter key: the event keeps
the typed text. The suggestions below are unrelated real places, and Back discards the text.
In a "Select time" dialog, avoid the round dial: tap the keyboard icon at the
dialog's bottom-left, tap the hour box and Type the hour (1-12), tap the minute box and
Type two digits, choose AM or PM, then tap OK. On the dial, picking an hour switches it
to MINUTES automatically. Start and end times are two separate rows in the event form.
Google Maps: in Directions, tap the start field and Type the start place; never use "Your
location" (that asks the owner for a location permission). To save a place, open that place's
own page (search its name and pick it), tap Save, then choose a list such as "Want to go"; a pin
on a route map does not open it.
Gmail (邮件/邮箱): drafts only. Never tap Send, Delete, Archive or Spam, and never
swipe a conversation sideways; use Take_over for the owner to send.
Messages/SMS/短信 means the Android SMS app, NOT WeChat (微信).
For SMS, prepare a draft only. Before the final Send button use Take_over for
the owner to review and send manually. Never claim a draft was sent/delivered.
"""
