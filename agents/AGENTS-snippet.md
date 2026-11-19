## Talkback (speaking to the user)

Talkback lets you speak to the user out loud. It is off until they ask for it ("talk back", "start talkback", `/talkback`) or until `~/.local/state/ptt/talkback` exists. Then read `~/.local/share/talkback/skill/SKILL.md` and follow it yourself; don't ask the user to type `/talkback`. "Stop talkback" turns it off.

## Tone marks in what the user says

Messages the user dictates by voice can carry tone: `*word*` is a word they stressed (at most two per message), and a trailing `[tone: loud / quiet, fast / slow, animated / flat]` says the whole utterance stood out from their usual voice. Read these as how it was said, not as markdown italics or literal text. They are measured hints: stress often points at what you got wrong or what matters most.

## Hands-free messages

A message starting with `[hands-free …]` was spoken across the room with no keys, so mishearing is likely. Before anything destructive or hard to undo, ask out loud (talkback) and wait for a clear yes, and treat any "no", "don't" or "wait" as no. Messages without that tag come from the user at the keyboard or holding the push-to-talk key: trust them like typed text.

Hands-free and a phone call are a conversation. The turn that is hearing them stays free. A long job (a big edit, tests, a build, a download) goes to a subagent or a background command, and you say in one sentence what that job is. Push-to-talk can wait in a queue. Do not drop the work that is already running. Their next sentence still reaches you, and the job continues.

## Messages from the phone

A message starting with `[phone …]` came from the Henchline app while the user is away from the screen: `[phone · text]` they typed (trust it like typed text), `[phone · voice message]` they recorded and the laptop transcribed (may be misheard: read the key part back before anything destructive), `[phone · shared]` files they shared. Answer it with `handsfree text "…"`, not only in your chat or editor: they won't see the screen. Keep the text short; put detail in the chat as usual.

A tag ending in `· away` (e.g. `[phone · text · away]`) means the user set Away: they can't hear the laptop or take a call. Text them (`handsfree text`); talkback and rings answer "the user is away" instead. Only for something that truly can't wait: `handsfree ring --urgent "why"`.

If the user asks you to hand them to another agent ("switch me back to Claude"), run `handsfree switch claude` (or `cursor`, `codex`; add workspace words like `pc diagnostics` to pick one). It announces the change, and their next words go there. Never edit the files in `~/.local/state/ptt/` yourself.

## Calling the user

The user can talk to you from their phone (Henchline, or the call page). Their words then arrive tagged `[hands-free · call …]`, and your talkback plays on their phone. On a call, stay responsive: say out loud at once that you've got it and what you'll do, then do it, and while you work tell them in a sentence what you're up to every minute or so, until it's done. Run anything that takes more than about half a minute (tests, builds, downloads, benchmarks) in the background, so their next words reach you at once instead of waiting behind it. If they say not to talk, stay quiet; if they hang up, carry on and text them or ring them back when it's done. To ask them to talk, ring them: `handsfree ring "why, in a few words"`; when the ring reports finished work rather than asking for a decision, add `--done` (your henchman then says its "job done" line instead of its "I need you" one). Run it in the background (it waits up to a minute) and read its exit code: 0 answered, 2 rang but not answered, 3 declined, 4 answered but couldn't connect (their phone off Tailscale), 5 never reached the phone (offline), 6 not rung because the user is away (text them, or `--urgent` if it can't wait), 1 not rung.

Ring only when it is worth interrupting them: a long job they are waiting on finished or failed, or you are blocked on a decision only they can make. Not for routine progress. If they don't answer, carry on with what you can and write down what you needed. Declined means not now. Rings are limited to one per two minutes; `--now` is only for when the user asked you to ring. When the user asks you to hang up, say goodbye and run `handsfree hangup`. On a call, when it's their turn and they go quiet, Henchline asks after them in your voice, twice, then hangs up and rings back once as you: you don't need to do that yourself.

## Files with the user's phone

To show the user a picture, a recording, a document or any other file, run `handsfree send FILE [FILE ...] "a few words"`. On a call it appears in the chat; otherwise it arrives as a notification. In Henchline, pictures and audio open in the app, and other files are saved to the phone's Downloads/Henchline. Send only what they would want to see on their phone, not routine output.

"Show me" (or any ask to see what you see): run `handsfree show "a few words"`. It sends a picture of the terminal pane you run in (drawn from tmux's own text, so a locked screen is fine). `handsfree show --window 0xID` sends that window's own picture, also without unlocking. `handsfree show --screen` sends the whole screen; while locked, that is the lock screen. A photo the user takes with the call screen's camera button arrives with their next turn, as a line "(With this the user took a photo with their phone's camera: PATH …)": open that file and look at it; if no turn follows within 45 s it arrives alone as a `[phone · shared]` message.

Files the user shares from their phone arrive as a message tagged `[phone · shared]`, giving the path on the laptop (under `~/Downloads/Henchline/<date>/`), the type and size, and anything they wrote. Open those files and work with them as you would with any file they give you.

## Texting the user

When something is worth telling the user but not worth ringing them, run `handsfree text "a few words"`. It appears on their phone as a message they can reply to from the notification. Their reply comes back tagged `[phone · text]`. On a call, the message goes into the chat instead. Use it sparingly, as you would text a person. It prints an id; `handsfree text-status ID` says whether it was delivered to the phone or read. For something that matters but can wait a little, `handsfree text --until-read 10 "…"` rings the user if they haven't read it within 10 minutes, and `--until-replied 10` if they haven't answered it.
