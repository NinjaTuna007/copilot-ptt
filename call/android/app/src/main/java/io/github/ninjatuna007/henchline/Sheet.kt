package io.github.ninjatuna007.henchline

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.text.TextUtils
import android.util.Size
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.ViewTreeObserver
import android.view.animation.DecelerateInterpolator
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The share screen as a bottom sheet, in Material's way (the user's wish, 2026-11-15: the old dialog looked
 * dated, and couldn't pick the agent): it slides up over the app that shared, shows what is being shared, the
 * crew as a row of henchmen to tap (the one whose chat is open picked first), a note, and Send to the one picked.
 */
class Sheet(
    private val activity: Activity,
    private val uris: List<Uri>,
    private val names: List<String>,
    private val text: String,
    private val crew: List<Pair<String, String>>,  // session, name
    private var picked: String,
    private val over: Boolean = false,  // over the app's own screen (the paperclip), not a share screen of its own
    private val note0: String = "",  // words already typed in the chat's box
    private val onSent: (String) -> Unit = {},  // the agent it went to
    private val onClosed: () -> Unit = {},  // the chat behind it is in view again
) {
    private val dp = activity.resources.displayMetrics.density
    private fun px(v: Int) = (v * dp).toInt()
    private fun colour(id: Int) = activity.getColor(id)
    private fun rounded(fill: Int, radius: Float, stroke: Int = 0, strokeColour: Int = 0) = GradientDrawable().apply {
        setColor(fill); cornerRadius = radius * dp
        if (stroke > 0) setStroke(px(stroke), strokeColour)
    }

    fun show() {
        hideKeyboard(activity)  // above the keyboard, not behind it
        val root = FrameLayout(activity).apply { setBackgroundColor(Color.parseColor("#99000000")) }
        val sheet = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            background = GradientDrawable().apply {
                setColor(colour(R.color.surface))
                cornerRadii = floatArrayOf(28 * dp, 28 * dp, 28 * dp, 28 * dp, 0f, 0f, 0f, 0f)
            }
            setPadding(px(24), px(12), px(24), px(24))
            isClickable = true  // a tap on the sheet isn't a tap outside it
        }
        sheet.addView(View(activity).apply { background = rounded(colour(R.color.muted), 2f) },
            LinearLayout.LayoutParams(px(36), px(4)).apply { gravity = Gravity.CENTER_HORIZONTAL; bottomMargin = px(16) })
        sheet.addView(TextView(activity).apply {
            this.text = if (over) "Send to ${crew.firstOrNull { it.first == picked }?.second?.substringBefore(",") ?: "this agent"}"
                        else "Share with your crew"
            setTextColor(colour(R.color.text)); textSize = 22f; typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        })
        sheet.addView(preview(), LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = px(14) })
        // From a chat's paperclip it goes to that chat's agent: no one to pick (the user's wish, 2026-11-15)
        if (!over) sheet.addView(TextView(activity).apply {
            this.text = "SEND TO"; setTextColor(colour(R.color.muted)); textSize = 12f; letterSpacing = 0.08f
            typeface = Typeface.DEFAULT_BOLD
        }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(20) })
        val send = Button(activity).apply {
            isAllCaps = false; textSize = 16f; setTextColor(colour(R.color.navy))
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            background = rounded(colour(R.color.cyan), 24f); stateListAnimator = null
        }
        val chips = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
        fun draw() {
            for (i in 0 until chips.childCount) {
                val chip = chips.getChildAt(i)
                val on = chip.tag == picked
                chip.background = rounded(if (on) colour(R.color.surface_high) else Color.TRANSPARENT, 20f, if (on) 2 else 1,
                    if (on) colour(R.color.cyan) else colour(R.color.surface_high))
            }
            send.text = "Send to " + (crew.firstOrNull { it.first == picked }?.second?.substringBefore(",") ?: "your crew")
        }
        for ((session, name) in crew) {
            chips.addView(LinearLayout(activity).apply {
                tag = session
                orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_HORIZONTAL
                setPadding(px(10), px(10), px(10), px(8))
                addView(ImageView(activity).apply { setImageResource(MainActivity.avatarOf(session)) }, LinearLayout.LayoutParams(px(52), px(52)))
                addView(TextView(activity).apply {
                    this.text = name.substringBefore(","); setTextColor(colour(R.color.text)); textSize = 13f; gravity = Gravity.CENTER
                    setPadding(0, px(6), 0, 0)
                })
                addView(TextView(activity).apply {
                    this.text = name.substringAfter(", ", "").removePrefix("in ").substringBefore(" · ")
                    setTextColor(colour(R.color.muted)); textSize = 11f
                    gravity = Gravity.CENTER; maxLines = 1; ellipsize = TextUtils.TruncateAt.END; maxWidth = px(96)
                })
                setOnClickListener { picked = session; draw() }
            }, LinearLayout.LayoutParams(px(104), ViewGroup.LayoutParams.WRAP_CONTENT).apply { marginEnd = px(10) })
        }
        if (!over) sheet.addView(HorizontalScrollView(activity).apply { isHorizontalScrollBarEnabled = false; addView(chips) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(10) })
        val note = EditText(activity).apply {
            hint = "Add a few words (optional)"; setText(note0); setHintTextColor(colour(R.color.muted)); setTextColor(colour(R.color.text))
            background = rounded(colour(R.color.navy), 16f, 1, colour(R.color.surface_high))
            setPadding(px(16), px(14), px(16), px(14)); textSize = 15f
            gravity = Gravity.TOP or Gravity.START; minLines = 2; maxLines = 6
            isSingleLine = false; setHorizontallyScrolling(false); isVerticalScrollBarEnabled = true
            imeOptions = EditorInfo.IME_ACTION_NONE
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES or
                android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE
        }
        sheet.addView(note, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = px(18) })
        val buttons = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL or Gravity.END }
        buttons.addView(Button(activity).apply {
            this.text = "Cancel"; isAllCaps = false; textSize = 16f; setTextColor(colour(R.color.cyan))
            setBackgroundColor(Color.TRANSPARENT); stateListAnimator = null
            setOnClickListener { close() }
        })
        buttons.addView(send, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, px(48)).apply { marginStart = px(8) })
        send.setPadding(px(24), 0, px(24), 0)
        send.setOnClickListener {
            activity.startForegroundService(UploadService.intent(activity, uris, names, text, note.text.toString(), picked)
                .putExtra(UploadService.INTO_CHAT, true))
            onSent(picked)
            // In that agent's chat too, as the paperclip's are (2026-11-15: a share reached the agent but never
            // showed in the app's chat)
            close()
        }
        sheet.addView(buttons, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = px(20) })
        draw()
        root.addView(sheet, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))
        root.setOnClickListener { close() }
        if (over) activity.addContentView(root, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT)) else activity.setContentView(root)
        liftAboveKeyboard(root)
        this.root = root
        sheet.translationY = 800 * dp
        sheet.animate().translationY(0f).setDuration(280).setInterpolator(DecelerateInterpolator()).start()
        this.sheet = sheet
    }

    /** Keep the caption and its Send button above the IME, including edge-to-edge Android versions where the
     * activity's resized content still draws under the keyboard. */
    private fun liftAboveKeyboard(root: FrameLayout) {
        val decor = activity.window.decorView
        val lift = ViewTreeObserver.OnGlobalLayoutListener {
            val insets = decor.rootWindowInsets ?: return@OnGlobalLayoutListener
            val keyboard = if (android.os.Build.VERSION.SDK_INT >= 30)
                insets.getInsets(android.view.WindowInsets.Type.ime() or android.view.WindowInsets.Type.navigationBars()).bottom
                else @Suppress("DEPRECATION") insets.systemWindowInsetBottom
            val at = IntArray(2).also { root.getLocationInWindow(it) }
            val cover = maxOf(0, at[1] + root.height - (decor.height - keyboard))
            if (root.paddingBottom != cover) root.setPadding(0, 0, 0, cover)
        }
        decor.viewTreeObserver.addOnGlobalLayoutListener(lift)
        root.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(v: View) {}
            override fun onViewDetachedFromWindow(v: View) {
                decor.viewTreeObserver.removeOnGlobalLayoutListener(lift)
            }
        })
    }

    private var sheet: View? = null

    private var root: View? = null

    val isOpen get() = root?.parent != null

    fun close() {
        val done: () -> Unit = { if (over) { (root?.parent as? ViewGroup)?.removeView(root); onClosed() } else activity.finish() }
        val s = sheet ?: return done()
        s.animate().translationY(s.height.toFloat()).setDuration(200).withEndAction { done() }.start()
    }

    /** What is being shared: a picture's thumbnail, else the names (or the text). */
    private fun preview(): View {
        val row = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            background = rounded(colour(R.color.navy), 16f); setPadding(px(12), px(12), px(12), px(12))
        }
        val first = uris.firstOrNull()
        if (first != null && activity.contentResolver.getType(first)?.startsWith("image/") == true) {
            val picture = try { activity.contentResolver.loadThumbnail(first, Size(px(64), px(64)), null) } catch (_: Exception) { null }
            if (picture != null) row.addView(ImageView(activity).apply {
                setImageBitmap(picture); scaleType = ImageView.ScaleType.CENTER_CROP; clipToOutline = true
                background = rounded(Color.TRANSPARENT, 12f)
            }, LinearLayout.LayoutParams(px(64), px(64)).apply { marginEnd = px(14) })
        }
        row.addView(TextView(activity).apply {
            this.text = when {
                uris.isEmpty() -> text.take(200)
                uris.size == 1 -> names[0]
                else -> "${uris.size} files: " + names.joinToString(", ")
            }
            setTextColor(colour(R.color.text)); textSize = 14f; maxLines = 3; ellipsize = TextUtils.TruncateAt.END
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        return row
    }
}
