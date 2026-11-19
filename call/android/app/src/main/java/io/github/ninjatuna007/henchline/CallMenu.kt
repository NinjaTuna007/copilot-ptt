package io.github.ninjatuna007.henchline

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.ViewGroup
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The call's own menu, as a calling app's (the user's wish, 2026-11-16): a tap on the call bar in the call's chat
 * slides it up. Who the call is with, the crew to move it to (a tap switches), how it sounds, how the line
 * behaves (open line, stay on, cut in or let it wait), and hang up. Each choice shows as two halves, the one on lit.
 */
class CallMenu(
    activity: Activity,
    private val who: String,
    private val crew: List<Pair<String, String>>,  // session, name
    private val current: String,
    private val speaker: Boolean,
    private val where: String = if (speaker) "speaker" else "earpiece",  // "headset" when earbuds carry the call
    private val earbuds: Boolean = false,  // earbuds connected: they're a third choice
    private val onRoute: (String) -> Unit = {},
    private val clear: Boolean,
    private val clearAllowed: Boolean = true,  // on the earpiece: call sound only, the choice greyed out
    private val buttonOnly: Boolean,
    private val stayOn: Boolean = false,
    private val onSwitch: (String) -> Unit,
    private val onSpeaker: (Boolean) -> Unit,
    private val onClear: (Boolean) -> Unit,
    private val onButtonOnly: (Boolean) -> Unit,
    private val onStayOn: (Boolean) -> Unit = {},
    private val cutIn: Boolean = false,
    private val onCutIn: (Boolean) -> Unit = {},
    private val onVoices: () -> Unit = {},
    private val onHangUp: () -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open(who, "On a call · tap a henchman to move the call to it")
        if (crew.size > 1) {
            sheet.addView(label("TALKING TO"), wide(18))
            val row = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
            for ((i, pair) in crew.withIndex()) row.addView(henchman(pair.first, pair.second, i))
            sheet.addView(HorizontalScrollView(activity).apply { isHorizontalScrollBarEnabled = false; addView(row) }, wide(10))
        }
        sheet.addView(label("SOUND"), wide(18))
        if (earbuds) sheet.addView(choices(listOf("🎧  Earbuds" to "headset", "🔊  Speaker" to "speaker",
            "👂  Earpiece" to "earpiece"), where) { onRoute(it); close() }, wide(10, px(48)))
        else sheet.addView(choice("🔊  Speaker", "👂  Earpiece", speaker) { onSpeaker(it); close() }, wide(10, px(48)))
        sheet.addView(choice("Clear sound", "Call sound", clear) { onClear(it); close() }.apply {
            if (!clearAllowed) { alpha = 0.4f; for (i in 0 until childCount) getChildAt(i).isEnabled = false }
        }, wide(8, px(48)))
        sheet.addView(label("THE LINE"), wide(18))
        sheet.addView(choice("Open line", "Hold to talk", !buttonOnly) { onButtonOnly(!it); close() }, wide(8, px(48)))
        // Stay on: no "are you still there?" and no hang-up when quiet; with Hold to talk, a walkie-talkie
        sheet.addView(choice("Stay on", "Check on me", stayOn) { onStayOn(it); close() }, wide(8, px(48)))
        sheet.addView(choice("Cut in", "Let it wait", cutIn) { onCutIn(it); close() }, wide(8, px(48)))
        sheet.addView(actionRow("🔊", "Voices", "How the agents sound", R.color.text) { close(); onVoices() }, wide(14))
        val hang = primary("Hang up", colour(R.color.hang), Color.WHITE).apply { setOnClickListener { close(); onHangUp() } }
        sheet.addView(hang, wide(20, px(52)))
    }

    /** A crew member to move the call to: its face (ringed when it's the one on the call), its name and project.
     *  The others hop in turn while the menu is open, eager to take over (the user's wish, 2026-11-16). */
    private fun henchman(session: String, label: String, place: Int) = LinearLayout(activity).apply {
        orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_HORIZONTAL
        setPadding(px(6), px(10), px(6), px(4))
        clipChildren = false; clipToPadding = false
        val on = session == current
        val name = label.substringBefore(",")
        val project = label.substringAfter(", in ", "").substringBefore(" · ").replace("_", " ")
        // A round window that stays put; the henchman lives inside it, clipped by its rim (the user's wish,
        // 2026-11-16: the whole circle moved, as if the henchman were stuck in a pill)
        val window = android.widget.FrameLayout(activity).apply {
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL; setColor(colour(R.color.surface_high))
                if (on) setStroke(px(2), colour(R.color.call_live))
            }
            outlineProvider = android.view.ViewOutlineProvider.BACKGROUND
            clipToOutline = true
        }
        // The henchman itself, without its backdrop (the window is that), looking about, blinking and moving its own
        // way as the README's do; the one on the call as it always looks
        val face = ImageView(activity).apply { setImageResource(if (on) MainActivity.avatarOf(session) else aliveOf(session)) }
        window.addView(face, android.widget.FrameLayout.LayoutParams(px(if (on) 48 else 58), px(if (on) 48 else 58), Gravity.CENTER))
        addView(window, LinearLayout.LayoutParams(px(60), px(60)))
        addView(TextView(activity).apply {
            text = name; textSize = 12.5f; maxLines = 1; gravity = Gravity.CENTER
            ellipsize = android.text.TextUtils.TruncateAt.END
            setTextColor(colour(if (on) R.color.call_live else R.color.text))
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        }, LinearLayout.LayoutParams(px(84), ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(6) })
        if (project.isNotEmpty()) addView(TextView(activity).apply {
            text = project; textSize = 11f; maxLines = 2; gravity = Gravity.CENTER
            ellipsize = android.text.TextUtils.TruncateAt.END
            setTextColor(colour(R.color.muted))
        }, LinearLayout.LayoutParams(px(84), ViewGroup.LayoutParams.WRAP_CONTENT))
        contentDescription = if (on) "$name in $project, on the call" else "Move the call to $name in $project"
        setOnClickListener { if (!on) { pop(this); onSwitch(session) }; close() }
        if (!on) alive(face, session, place)
    }

    /** The henchman inside its window, eager to take over, each in its own way and never in step: Claude peeks up
     *  from below the rim and tilts its head at you, Cursor sneaks in from the side, Codex bobs and leans in;
     *  every move and pause picked at random. Only while the window is on screen (the menu closed, it stops). */
    private fun alive(face: ImageView, session: String, place: Int) {
        val random = java.util.Random(session.hashCode().toLong() xor System.nanoTime())
        val d = px(1).toFloat()
        fun anim(prop: android.util.Property<android.view.View, Float>, vararg values: Float) =
            android.animation.ObjectAnimator.ofFloat(face, prop, *values)
        val moves: List<() -> android.animation.Animator> = listOf(
            { android.animation.AnimatorSet().apply {  // peek: duck below the rim, rise slowly, look at you
                playSequentially(anim(android.view.View.TRANSLATION_Y, 0f, 56 * d).apply { duration = 300 },
                    anim(android.view.View.TRANSLATION_Y, 56 * d, 6 * d, 0f).apply { duration = 900
                        interpolator = android.view.animation.DecelerateInterpolator() },
                    anim(android.view.View.ROTATION, 0f, -12f, -12f, 0f).apply { duration = 1100 }) } },
            { android.animation.AnimatorSet().apply {  // sneak: out one side, in quickly from the other
                val side = if (random.nextBoolean()) 1f else -1f
                playSequentially(anim(android.view.View.TRANSLATION_X, 0f, 56 * d * side).apply { duration = 220 },
                    anim(android.view.View.TRANSLATION_X, -56 * d * side, 0f).apply { duration = 420
                        interpolator = android.view.animation.OvershootInterpolator(1.4f) }) } },
            { android.animation.AnimatorSet().apply {  // lean in: closer, a nod, back
                playTogether(anim(android.view.View.SCALE_X, 1f, 1.18f, 1.18f, 1f),
                    anim(android.view.View.SCALE_Y, 1f, 1.18f, 1.18f, 1f),
                    anim(android.view.View.TRANSLATION_Y, 0f, 4 * d, -3 * d, 0f))
                duration = 1000 } },
            { android.animation.AnimatorSet().apply {  // bob: a couple of eager little hops in place
                playTogether(anim(android.view.View.TRANSLATION_Y, 0f, -7 * d, 0f, -4 * d, 0f),
                    anim(android.view.View.ROTATION, 0f, 5f, -5f, 0f))
                duration = 700 } },
        )
        val agent = session.substringBefore("-")
        val likes = when (agent) {  // its favourite moves come up most
            "claude" -> listOf(0, 0, 2, 3)
            "cursor" -> listOf(1, 1, 3, 2)
            "codex" -> listOf(3, 3, 2, 0)
            else -> listOf(0, 1, 2, 3)
        }
        var running: android.animation.Animator? = null
        var stopped = true
        fun next(delay: Long) {
            if (stopped) return
            running = moves[likes[random.nextInt(likes.size)]]().apply {
                startDelay = delay
                addListener(object : android.animation.AnimatorListenerAdapter() {
                    private var cancelled = false
                    override fun onAnimationCancel(animation: android.animation.Animator) { cancelled = true }
                    override fun onAnimationEnd(animation: android.animation.Animator) {
                        if (!cancelled) next(2500L + random.nextInt(4000))
                    }
                })
                start()
            }
        }
        face.addOnAttachStateChangeListener(object : android.view.View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(v: android.view.View) {
                (face.drawable as? android.graphics.drawable.Animatable)?.start()  // its eyes, blinks and sway
                stopped = false; next(1500L + place * 700L + random.nextInt(1500))
            }
            override fun onViewDetachedFromWindow(v: android.view.View) {
                (face.drawable as? android.graphics.drawable.Animatable)?.stop()
                stopped = true; running?.cancel(); running = null
            }
        })
    }

    private fun aliveOf(session: String): Int = when {
        session.startsWith("claude") -> R.drawable.avatar_claude_alive
        session.startsWith("cursor") -> R.drawable.avatar_cursor_alive
        session.startsWith("codex") -> R.drawable.avatar_codex_alive
        else -> R.drawable.avatar_handsfree_alive
    }

    /** Parts of a pill, the one that's on lit in cyan; a tap on another picks it (its key). */
    private fun choices(options: List<Pair<String, String>>, on: String, picked: (String) -> Unit) =
        LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            background = rounded(colour(R.color.navy), 24f)
            setPadding(px(4), px(4), px(4), px(4))
            for ((words, key) in options) {
                val lit = key == on
                addView(TextView(activity).apply {
                    text = words; textSize = 13.5f; gravity = Gravity.CENTER; maxLines = 1
                    setTextColor(colour(if (lit) R.color.navy else R.color.text))
                    typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                    background = if (lit) rounded(colour(R.color.cyan), 20f) else null
                    contentDescription = if (lit) "$words, on" else words
                    setOnClickListener { if (!lit) picked(key) }
                }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f))
            }
        }

    /** Two halves of a pill, the one that's on lit in cyan; a tap on the other picks it (true: the first). */
    private fun choice(first: String, second: String, firstOn: Boolean, picked: (Boolean) -> Unit) =
        LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            background = rounded(colour(R.color.navy), 24f)
            setPadding(px(4), px(4), px(4), px(4))
            for ((i, words) in listOf(first, second).withIndex()) {
                val on = (i == 0) == firstOn
                addView(TextView(activity).apply {
                    text = words; textSize = 14f; gravity = Gravity.CENTER
                    setTextColor(colour(if (on) R.color.navy else R.color.text))
                    typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                    background = if (on) rounded(colour(R.color.cyan), 20f) else null
                    contentDescription = if (on) "$words, on" else words
                    setOnClickListener { if (!on) picked(i == 0) }
                }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f))
            }
        }
}

/** With earbuds connected, the speaker button's list (the user's wish, 2026-11-16): where the call's sound goes. */
class RoutePicker(activity: Activity, private val where: String, private val picked: (String) -> Unit) : BottomPanel(activity) {
    fun show() {
        val sheet = open("Sound", "Where you hear the call")
        for ((icon, words, key) in listOf(Triple("🎧", "Earbuds", "headset"), Triple("🔊", "Speaker", "speaker"),
                Triple("👂", "Phone earpiece", "earpiece"))) {
            val on = key == where
            sheet.addView(LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                setPadding(px(14), px(14), px(14), px(14))
                background = rounded(colour(if (on) R.color.surface_high else R.color.navy), 16f,
                    if (on) 2 else 0, colour(R.color.cyan))
                addView(TextView(activity).apply { text = icon; textSize = 20f }, LinearLayout.LayoutParams(px(38), ViewGroup.LayoutParams.WRAP_CONTENT))
                addView(TextView(activity).apply {
                    text = words; textSize = 16f; setTextColor(colour(if (on) R.color.cyan else R.color.text))
                    typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                if (on) addView(TextView(activity).apply { text = "✓"; textSize = 18f; setTextColor(colour(R.color.cyan)) })
                contentDescription = if (on) "$words, on" else words
                setOnClickListener { if (!on) picked(key); close() }
            }, wide(10))
        }
    }
}
