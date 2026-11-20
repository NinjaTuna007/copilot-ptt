package io.github.ninjatuna007.henchline

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.Editable
import android.text.TextUtils
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.animation.DecelerateInterpolator
import android.view.animation.OvershootInterpolator
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject

/**
 * Bottom sheets in the share sheet's style (the user's wish, 2026-11-16: the plain dialogs looked "from 2008"):
 * a panel that slides up over the chat list, dimming it, closed by a tap outside it or Cancel.
 */
/** The keyboard put away, so a sheet or menu opening shows in full (it came up behind the keyboard, 2026-11-16). */
fun hideKeyboard(activity: Activity) {
    val focus = activity.currentFocus ?: return
    activity.getSystemService(android.view.inputmethod.InputMethodManager::class.java)
        ?.hideSoftInputFromWindow(focus.windowToken, 0)
    focus.clearFocus()
}

/**
 * A sheet that a downward drag pulls down, and past a quarter of its height (or flung) closes: what its grey handle
 * promises (the user's catch, 2026-11-17: the handle did nothing). A drag inside a list that can still scroll up
 * scrolls the list instead; taps, holds and sideways drags stay with what's in the sheet.
 */
class DragSheet(context: android.content.Context, private val onDismiss: () -> Unit) : LinearLayout(context) {
    private val slop = android.view.ViewConfiguration.get(context).scaledTouchSlop
    private var startX = 0f
    private var startY = 0f
    private var dragging = false
    private var velocity: android.view.VelocityTracker? = null

    /** Whether something scrollable under that point could still scroll up: then the drag is the list's. */
    private fun listCanScrollUp(group: android.view.ViewGroup, x: Float, y: Float): Boolean {
        val hit = android.graphics.Rect()
        for (i in 0 until group.childCount) {
            val child = group.getChildAt(i)
            if (child.visibility != View.VISIBLE || !child.getGlobalVisibleRect(hit) || !hit.contains(x.toInt(), y.toInt())) continue
            if (child.canScrollVertically(-1)) return true
            if (child is android.view.ViewGroup && listCanScrollUp(child, x, y)) return true
        }
        return false
    }

    override fun onInterceptTouchEvent(event: android.view.MotionEvent): Boolean {
        when (event.actionMasked) {
            android.view.MotionEvent.ACTION_DOWN -> { startX = event.rawX; startY = event.rawY; dragging = false }
            android.view.MotionEvent.ACTION_MOVE -> {
                val dy = event.rawY - startY
                if (!dragging && dy > slop && dy > 2 * kotlin.math.abs(event.rawX - startX) &&
                    !listCanScrollUp(this, startX, startY)) {
                    dragging = true
                    velocity = android.view.VelocityTracker.obtain()
                }
            }
        }
        return dragging
    }

    @android.annotation.SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: android.view.MotionEvent): Boolean {
        // A touch on the sheet itself (its handle, its title, between rows) comes here, not through the intercept
        when (event.actionMasked) {
            android.view.MotionEvent.ACTION_DOWN -> { startX = event.rawX; startY = event.rawY; dragging = false }
            android.view.MotionEvent.ACTION_MOVE -> if (!dragging) {
                val dy = event.rawY - startY
                if (dy > slop && dy > 2 * kotlin.math.abs(event.rawX - startX)) {
                    dragging = true
                    velocity = android.view.VelocityTracker.obtain()
                }
            }
        }
        if (!dragging) return super.onTouchEvent(event)
        velocity?.addMovement(event)
        when (event.actionMasked) {
            android.view.MotionEvent.ACTION_MOVE -> translationY = (event.rawY - startY).coerceAtLeast(0f)
            android.view.MotionEvent.ACTION_UP, android.view.MotionEvent.ACTION_CANCEL -> {
                velocity?.computeCurrentVelocity(1000)
                val flung = (velocity?.yVelocity ?: 0f) > 1500 * resources.displayMetrics.density
                velocity?.recycle(); velocity = null
                dragging = false
                if (event.actionMasked == android.view.MotionEvent.ACTION_UP && (translationY > height / 4f || flung)) onDismiss()
                else animate().translationY(0f).setDuration(180).start()
            }
        }
        return true
    }
}

open class BottomPanel(protected val activity: Activity) {
    companion object {
        private val shown = ArrayDeque<BottomPanel>()  // open sheets, the newest last: back closes that one

        /** The sheet on top, or null when none is open. */
        fun top(): BottomPanel? = shown.lastOrNull { it.isOpen }

        /** Every open sheet, the top one first: leaving a screen must not leave one drawn over the next. */
        fun closeAll() {
            while (shown.isNotEmpty()) shown.last().close(animate = false)
        }
    }

    protected val dp = activity.resources.displayMetrics.density
    protected fun px(v: Int) = (v * dp).toInt()
    protected fun colour(id: Int) = activity.getColor(id)
    protected fun rounded(fill: Int, radius: Float, stroke: Int = 0, strokeColour: Int = 0) = GradientDrawable().apply {
        setColor(fill); cornerRadius = radius * dp
        if (stroke > 0) setStroke(px(stroke), strokeColour)
    }

    private var root: FrameLayout? = null
    private var sheet: LinearLayout? = null
    protected var subtitleView: TextView? = null  // the line under the title, for sheets that update it
    val isOpen get() = root?.parent != null

    /** A fresh panel with its handle, title and a line under it; what goes in it is added to the returned layout. */
    protected fun open(title: String, subtitle: String): LinearLayout {
        close(animate = false)
        hideKeyboard(activity)  // every sheet above it, not behind it (the model picker was, 2026-11-16)
        val root = FrameLayout(activity).apply { setBackgroundColor(Color.parseColor("#99000000")); alpha = 0f }
        val sheet = DragSheet(activity) { close() }.apply {
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
            text = title; setTextColor(colour(R.color.text)); textSize = 22f
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        })
        subtitleView = if (subtitle.isNotEmpty()) TextView(activity).apply {
            text = subtitle; setTextColor(colour(R.color.muted)); textSize = 14f; setPadding(0, px(4), 0, 0)
        }.also { sheet.addView(it) } else null
        root.addView(sheet, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))
        root.setOnClickListener { close() }
        // Above the keyboard when one is up (the user's catch, 2026-11-17: typing a new name, the keyboard hid it):
        // the window's own insets are read on every layout, since the app's root view takes them for itself
        val decor = activity.window.decorView
        val lift = android.view.ViewTreeObserver.OnGlobalLayoutListener {
            val insets = decor.rootWindowInsets ?: return@OnGlobalLayoutListener
            val keyboard = if (android.os.Build.VERSION.SDK_INT >= 30)
                insets.getInsets(android.view.WindowInsets.Type.ime() or android.view.WindowInsets.Type.navigationBars()).bottom
                else @Suppress("DEPRECATION") insets.systemWindowInsetBottom
            // By how much the keyboard actually covers the panel: on Android 10-14 the window is already resized
            // above it (adjustResize), and only Android 15's drawing behind the bars leaves the panel under it
            val at = IntArray(2).also { root.getLocationInWindow(it) }
            // (the root's height already holds its padding: adding the padding again grew it on every layout,
            // and the sheet was squeezed away, 2026-11-17)
            val cover = maxOf(0, at[1] + root.height - (decor.height - keyboard))
            if (root.paddingBottom != cover) root.setPadding(0, 0, 0, cover)
        }
        decor.viewTreeObserver.addOnGlobalLayoutListener(lift)
        root.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(v: View) {}
            override fun onViewDetachedFromWindow(v: View) { decor.viewTreeObserver.removeOnGlobalLayoutListener(lift) }
        })
        activity.addContentView(root, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        shown.addLast(this)  // above any sheet already open: back closes this one first
        root.animate().alpha(1f).setDuration(180).start()
        sheet.translationY = 800 * dp
        sheet.animate().translationY(0f).setDuration(300).setInterpolator(DecelerateInterpolator(1.6f)).start()
        this.root = root
        this.sheet = sheet
        return sheet
    }

    fun close(animate: Boolean = true) {
        shown.remove(this)
        val r = root ?: return
        val s = sheet
        root = null; sheet = null
        val done = { (r.parent as? ViewGroup)?.removeView(r) }
        if (!animate || s == null) return done().let {}
        r.animate().alpha(0f).setDuration(200).start()
        s.animate().translationY(s.height.toFloat()).setDuration(200).withEndAction { done() }.start()
    }

    /** A row with an icon, a title and a line under it, as the chat's and a message's long-press sheets show. */
    protected fun actionRow(icon: String, title: String, detail: String, colourId: Int, then: () -> Unit) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        setPadding(px(14), px(14), px(14), px(14))
        background = rounded(colour(R.color.navy), 16f)
        addView(TextView(activity).apply { text = icon; textSize = 20f }, LinearLayout.LayoutParams(px(38), ViewGroup.LayoutParams.WRAP_CONTENT))
        addView(LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(activity).apply { text = title; setTextColor(colour(colourId)); textSize = 16f; typeface = Typeface.DEFAULT_BOLD })
            if (detail.isNotEmpty()) addView(TextView(activity).apply { text = detail; setTextColor(colour(R.color.muted)); textSize = 12f })
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        setOnClickListener { pop(this); then() }
    }.also { it.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(8) } }

    /** Asked again in the same sheet, in red, before deleting: with the files saved from it (Downloads/Henchline)
     *  offered too, ticked, when there are some (the user's wish, 2026-11-16). */
    protected fun deleteConfirm(rows: LinearLayout, question: String, files: Int, button: String, then: (alsoFiles: Boolean) -> Unit) {
        rows.removeAllViews()
        rows.addView(TextView(activity).apply { text = question; setTextColor(colour(R.color.text)); textSize = 15f })
        val also = android.widget.CheckBox(activity).apply {
            text = if (files == 1) "Also delete its saved file from Downloads/Henchline"
                   else "Also delete its $files saved files from Downloads/Henchline"
            setTextColor(colour(R.color.text)); textSize = 14f; isChecked = true
            buttonTintList = android.content.res.ColorStateList.valueOf(colour(R.color.cyan))
        }
        if (files > 0) rows.addView(also, wide(12))
        val go = primary(button, colour(R.color.hang), Color.WHITE)
        go.setOnClickListener { close(); then(files > 0 && also.isChecked) }
        rows.addView(go, wide(16, px(52)))
        pop(go)
    }

    protected fun label(words: String) = TextView(activity).apply {
        text = words; setTextColor(colour(R.color.muted)); textSize = 12f; letterSpacing = 0.08f; typeface = Typeface.DEFAULT_BOLD
    }

    protected fun primary(words: String, fill: Int = colour(R.color.cyan), ink: Int = colour(R.color.navy)) = Button(activity).apply {
        text = words; isAllCaps = false; textSize = 16f; setTextColor(ink)
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        background = rounded(fill, 24f); stateListAnimator = null
    }

    protected fun quiet(words: String, then: () -> Unit) = Button(activity).apply {
        text = words; isAllCaps = false; textSize = 16f; setTextColor(colour(R.color.cyan))
        setBackgroundColor(Color.TRANSPARENT); stateListAnimator = null
        setOnClickListener { then() }
    }

    protected fun wide(topMargin: Int = 0, height: Int = ViewGroup.LayoutParams.WRAP_CONTENT) =
        LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, height).apply { this.topMargin = px(topMargin) }

    /** A view popping in, a little springy: something is happening. */
    protected fun pop(view: View) {
        view.scaleX = 0.92f; view.scaleY = 0.92f
        view.animate().scaleX(1f).scaleY(1f).setDuration(220).setInterpolator(OvershootInterpolator()).start()
    }
}

/**
 * New chat (the user's wish, 2026-11-16): the agents as henchmen to tap, the folders the laptop offers as cards
 * (newest first), one box that both filters them and takes a typed path, and a button saying exactly what will
 * start where. start() does the asking; it calls back with null when the chat opened, or why not.
 */
class NewChatSheet(
    activity: Activity,
    private val folders: List<JSONObject>,  // {path, name}
    private val agents: List<String>,
    private val loadModels: (agent: String, then: (JSONObject) -> Unit) -> Unit,  // {models} or {menu}
    private val cursorChats: (() -> Unit)? = null,  // a Cursor chat you already have, instead of a new agent
    private val start: (folder: String, agent: String, model: String, params: Map<String, String>, typed: Boolean,
                        done: (String?) -> Unit) -> Unit,
) : BottomPanel(activity) {
    private var model = ""  // "" the agent's own default
    private var modelName = "Default"
    private val settings = mutableMapOf<String, String>()  // its effort, context… from the agent's menu
    private var agent = agents.first()
    private var folder: JSONObject? = folders.firstOrNull()
    private var typed = ""  // a path in the box, when it is one
    private var failure = ""  // why the last start didn't happen, shown until something else is picked
    private var modelRefresh: () -> Unit = {}

    fun show() {
        val sheet = open("New chat", "Starts an agent on your laptop, in its own tmux window")
        cursorChats?.let { show -> sheet.addView(actionRow("💬", "Open a Cursor chat you already have",
            "Any chat in Cursor's list, quiet ones too", R.color.cyan) { close(); show() }, wide(12)) }
        sheet.addView(label("AGENT"), wide(20))
        val chips = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
        val go = primary("")
        val warning = TextView(activity).apply {
            textSize = 12f; gravity = Gravity.CENTER; visibility = View.GONE
        }
        val cards = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        val box = EditText(activity)
        val home = folders.firstOrNull { it.optString("name") == "home" }?.optString("path")
            ?: folders.firstOrNull()?.optString("path")?.let { Regex("^(/home/[^/]+)").find(it)?.value }

        fun pretty(path: String) = if (home != null && path.startsWith(home)) "~" + path.removePrefix(home) else path
        fun refresh() {
            modelRefresh()
            for (i in 0 until chips.childCount) {
                val chip = chips.getChildAt(i)
                val on = chip.tag == agent
                chip.background = rounded(if (on) colour(R.color.surface_high) else Color.TRANSPARENT, 20f, if (on) 2 else 1,
                    if (on) colour(R.color.cyan) else colour(R.color.surface_high))
                chip.alpha = if (on) 1f else 0.7f
            }
            for (i in 0 until cards.childCount) {
                val card = cards.getChildAt(i)
                val on = typed.isEmpty() && card.tag == folder?.optString("path") || typed.isNotEmpty() && card.tag == "typed"
                card.background = rounded(colour(if (on) R.color.surface_high else R.color.navy), 16f, if (on) 2 else 0, colour(R.color.cyan))
            }
            val where = if (typed.isNotEmpty()) typed.trimEnd('/').substringAfterLast('/') else folder?.optString("name") ?: ""
            go.text = if (where.isEmpty()) "Pick a folder" else "Start ${agent.replaceFirstChar { it.uppercase() }} in ${where.replace("_", " ")}"
            if (where.isNotEmpty() && model.isNotEmpty()) go.text = "${go.text} · $modelName"
            go.isEnabled = where.isNotEmpty()
            go.alpha = if (go.isEnabled) 1f else 0.5f
            when {
                failure.isNotEmpty() -> { warning.text = failure; warning.setTextColor(colour(R.color.hang)); warning.visibility = View.VISIBLE }
                typed.isNotEmpty() -> {
                    warning.text = "A folder the agent hasn't worked in: starting there trusts it to read and change its files."
                    warning.setTextColor(colour(R.color.muted)); warning.visibility = View.VISIBLE
                }
                else -> warning.visibility = View.GONE
            }
        }
        fun listFolders(query: String) {
            cards.removeAllViews()
            failure = ""
            val q = query.trim()
            typed = if (q.startsWith("/") || q.startsWith("~")) q else ""
            if (typed.isNotEmpty()) cards.addView(card("typed", "✏️", "Use this path", typed) { refresh() })
            val shown = if (typed.isNotEmpty() || q.isEmpty()) folders
                else folders.filter { q.lowercase() in it.optString("name").lowercase() || q.lowercase() in it.optString("path").lowercase() }
            for (f in shown) cards.addView(card(f.optString("path"), "📁", f.optString("name").replace("_", " "),
                pretty(f.optString("path")).substringBeforeLast("/").ifEmpty { "/" }) {
                folder = f
                failure = ""
                if (typed.isNotEmpty()) box.setText("")  // a picked card wins over a half-typed path
                refresh()
            })
            if (shown.isEmpty() && typed.isEmpty()) cards.addView(TextView(activity).apply {
                text = "No folder matches. Type a path starting with ~ or /."
                setTextColor(colour(R.color.muted)); textSize = 13f; setPadding(px(4), px(12), px(4), px(12))
            })
            refresh()
        }

        for (kind in agents) {
            chips.addView(LinearLayout(activity).apply {
                tag = kind
                orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_HORIZONTAL
                setPadding(px(12), px(10), px(12), px(8))
                addView(ImageView(activity).apply { setImageResource(MainActivity.avatarOf(kind)) }, LinearLayout.LayoutParams(px(52), px(52)))
                addView(TextView(activity).apply {
                    text = if (kind == "cursor") "Cursor CLI" else kind.replaceFirstChar { it.uppercase() }
                    setTextColor(colour(R.color.text)); textSize = 13f; gravity = Gravity.CENTER; setPadding(0, px(6), 0, 0)
                })
                setOnClickListener { agent = kind; failure = ""; model = ""; modelName = "Default"; settings.clear(); pop(this); refresh() }
            }, LinearLayout.LayoutParams(px(96), ViewGroup.LayoutParams.WRAP_CONTENT).apply { marginEnd = px(10) })
        }
        sheet.addView(HorizontalScrollView(activity).apply { isHorizontalScrollBarEnabled = false; addView(chips) }, wide(10))
        // The model: a pill saying which, a list to pick from (loaded for the agent picked)
        val modelPill = TextView(activity).apply {
            setTextColor(colour(R.color.cyan)); textSize = 14f
            background = rounded(colour(R.color.navy), 18f, 1, colour(R.color.surface_high))
            setPadding(px(16), px(10), px(16), px(10))
            setOnClickListener {
                text = "Loading models…"
                loadModels(agent) { answer ->
                    refresh()
                    val menu = answer.optJSONArray("menu")
                    if (menu != null && menu.length() > 0) {  // the same menu as a chat's: model, effort, context…
                        val entries = (0 until menu.length()).map { menu.getJSONObject(it) }
                        if (model.isEmpty()) {
                            model = entries[0].optString("id")
                        }
                        CursorModelMenu(activity, entries, model, settings, title = "${agent.replaceFirstChar { it.uppercase() }}'s model",
                            subtitle = "For the new chat") { picked, params, done ->
                            model = picked
                            settings.clear(); settings.putAll(params)
                            val name = entries.firstOrNull { it.optString("id") == picked }?.optString("name") ?: picked
                            val effort = params["effort"] ?: params["reasoning_effort"]
                            modelName = listOfNotNull(name, effort, params["context"]?.takeIf { it == "1m" }?.let { "1M" }).joinToString(" · ")
                            refresh(); done(null)
                        }.show()
                        return@loadModels
                    }
                    val list = answer.optJSONArray("models")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList()
                    ModelPicker(activity, "${agent.replaceFirstChar { it.uppercase() }}'s model", list, withDefault = true) { id, name ->
                        model = id; modelName = name; settings.clear(); refresh()
                    }.show()
                }
            }
        }
        sheet.addView(modelPill, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = px(10) })
        modelRefresh = { modelPill.text = "✦ Model: $modelName  ▾" }

        sheet.addView(label("FOLDER"), wide(18))
        box.apply {
            hint = "Search, or type a path (~/…)"
            setHintTextColor(colour(R.color.muted)); setTextColor(colour(R.color.text)); textSize = 15f; setSingleLine()
            background = rounded(colour(R.color.navy), 16f, 1, colour(R.color.surface_high))
            setPadding(px(16), px(12), px(16), px(12))
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun afterTextChanged(s: Editable?) = listFolders(s?.toString() ?: "")
            })
        }
        sheet.addView(box, wide(10))
        // The list scrolls inside the sheet, at most about half the screen
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(cards) },
            wide(10, minOf(px(76) * maxOf(folders.size, 1), (activity.resources.displayMetrics.heightPixels * 0.38).toInt())))
        sheet.addView(warning, wide(12))
        val buttons = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL or Gravity.END }
        buttons.addView(quiet("Cancel") { close() })
        buttons.addView(go, LinearLayout.LayoutParams(0, px(52), 1f).apply { marginStart = px(8) })
        go.setOnClickListener {
            val path = if (typed.isNotEmpty()) (if (typed.startsWith("~") && home != null) home + typed.removePrefix("~") else typed)
                       else folder?.optString("path") ?: return@setOnClickListener
            go.isEnabled = false
            go.text = "Starting ${agent.replaceFirstChar { it.uppercase() }}…"
            go.animate().alpha(0.7f).setDuration(150).start()
            start(path, agent, model, settings.toMap(), typed.isNotEmpty()) { why ->
                if (why == null) close()  // this sheet's own close: never a newer sheet's
                else { failure = why; refresh() }
            }
        }
        sheet.addView(buttons, wide(16))
        listFolders("")
    }

    private fun card(tag: String, icon: String, title: String, detail: String, picked: () -> Unit) = LinearLayout(activity).apply {
        this.tag = tag
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        setPadding(px(14), px(12), px(14), px(12))
        addView(TextView(activity).apply { text = icon; textSize = 22f }, LinearLayout.LayoutParams(px(36), ViewGroup.LayoutParams.WRAP_CONTENT))
        addView(LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(activity).apply {
                text = title; setTextColor(colour(R.color.text)); textSize = 16f; typeface = Typeface.DEFAULT_BOLD
                maxLines = 1; ellipsize = TextUtils.TruncateAt.END
            })
            addView(TextView(activity).apply {
                text = detail; setTextColor(colour(R.color.muted)); textSize = 12f; maxLines = 1; ellipsize = TextUtils.TruncateAt.MIDDLE
            })
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        setOnClickListener { pop(this); picked() }
    }.also { it.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(8) } }
}

/**
 * Long-press on a chat: what can be done with it, as rows with an icon; ending an agent asks again in the same
 * sheet, in red, before it happens.
 */
class ChatActionsSheet(
    activity: Activity,
    private val title: String,
    private val subtitle: String,
    private val canEnd: Boolean,
    private val gone: Boolean,  // its agent is no longer on the laptop's crew list
    private val savedFiles: Int,  // files saved from this chat that no other chat has
    private val onClear: (alsoFiles: Boolean) -> Unit,
    private val onRemove: () -> Unit,
    private val onEnd: (done: (String?) -> Unit) -> Unit,
    private val onRename: ((String) -> Unit)? = null,  // the agent's own /rename, with the name typed here
    private val pinned: Boolean = false,
    private val onPin: ((Boolean) -> Unit)? = null,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open(title, subtitle)
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        sheet.addView(rows, wide(16))
        onPin?.let { pin ->
            rows.addView(row(if (pinned) "📌" else "📌", if (pinned) "Unpin chat" else "Pin chat",
                if (pinned) "It goes back with the others" else "Stays at the top of the list",
                R.color.text) { pin(!pinned); close() })
        }
        if (!gone) onRename?.let { rename -> rows.addView(row("✏️", "Rename chat", "As /rename does: the agent's own name for it",
                R.color.text) { askName(rows, rename) }) }
        if (!gone) rows.addView(row("🧹", "Clear chat", "On this phone only: the agent keeps its conversation", R.color.text) {
            deleteConfirm(rows, "Clear the chat with $title on this phone? The agent keeps its conversation.",
                savedFiles, "Clear chat", onClear)
        })
        // Always there: an agent ended on the laptop may still be on the phone's last list (2026-11-16)
        rows.addView(row("🗑", "Remove chat", "From this phone's list; it comes back only if the agent is still running", R.color.text) {
            onRemove(); close()
        })
        if (canEnd && !gone) rows.addView(row("⏹", "End agent", "Closes its tmux window on the laptop", R.color.hang) { confirm(rows) })
        else if (!gone) rows.addView(TextView(activity).apply {
            text = "Started on the laptop, so it can't be ended from here."
            setTextColor(colour(R.color.muted)); textSize = 12f; setPadding(px(4), px(8), px(4), 0)
        })
        sheet.addView(quiet("Cancel") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }

    /** Straight to clearing (the chat's /flush): the same question as from the menu. */
    fun showClear() {
        val sheet = open(title, subtitle)
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        sheet.addView(rows, wide(16))
        deleteConfirm(rows, "Clear the chat with $title on this phone? The agent keeps its conversation.",
            savedFiles, "Clear chat", onClear)
    }

    /** The new name, typed in place of the rows (the keyboard comes up for it). */
    private fun askName(rows: LinearLayout, rename: (String) -> Unit) {
        rows.removeAllViews()
        val box = EditText(activity).apply {
            hint = "New name"; setSingleLine()
            setHintTextColor(colour(R.color.muted)); setTextColor(colour(R.color.text)); textSize = 16f
            background = rounded(colour(R.color.navy), 16f, 1, colour(R.color.surface_high))
            setPadding(px(16), px(12), px(16), px(12))
        }
        rows.addView(box, wide(4))
        val go = primary("Rename").apply {
            setOnClickListener {
                val name = box.text.toString().trim()
                if (name.isNotEmpty()) { hideKeyboard(activity); close(); rename(name) }
            }
        }
        rows.addView(go, wide(12, px(52)))
        box.requestFocus()
        box.postDelayed({ activity.getSystemService(android.view.inputmethod.InputMethodManager::class.java)
            ?.showSoftInput(box, 0) }, 200)
    }

    private fun confirm(rows: LinearLayout) {
        rows.removeAllViews()
        rows.addView(TextView(activity).apply {
            text = "End $title? Its tmux window closes and its chat leaves the list."
            setTextColor(colour(R.color.text)); textSize = 15f
        })
        val end = primary("End agent", colour(R.color.hang), Color.WHITE)
        val note = TextView(activity).apply { setTextColor(colour(R.color.hang)); textSize = 12f; visibility = View.GONE }
        end.setOnClickListener {
            end.isEnabled = false; end.text = "Ending…"
            onEnd { why -> if (why == null) close() else { note.text = why; note.visibility = View.VISIBLE; end.isEnabled = true; end.text = "End agent" } }
        }
        rows.addView(end, wide(16, px(52)))
        rows.addView(note, wide(8))
        pop(end)
    }

    private fun row(icon: String, title: String, detail: String, colourId: Int, then: () -> Unit) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        setPadding(px(14), px(14), px(14), px(14))
        background = rounded(colour(R.color.navy), 16f)
        addView(TextView(activity).apply { text = icon; textSize = 20f }, LinearLayout.LayoutParams(px(38), ViewGroup.LayoutParams.WRAP_CONTENT))
        addView(LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(activity).apply { text = title; setTextColor(colour(colourId)); textSize = 16f; typeface = Typeface.DEFAULT_BOLD })
            addView(TextView(activity).apply { text = detail; setTextColor(colour(R.color.muted)); textSize = 12f })
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        setOnClickListener { pop(this); then() }
    }.also { it.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(8) } }
}

/**
 * A model to pick (the user's wish, 2026-11-16): the agent's models as rows, a box filtering them (the Cursor CLI
 * lists hundreds), and "Default" first when starting (the agent's own choice).
 */
class ModelPicker(
    activity: Activity,
    private val title: String,
    private val models: List<JSONObject>,  // {id, name}
    private val withDefault: Boolean,
    private val picked: (id: String, name: String) -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open(title, if (withDefault) "Default is the agent's own setting" else "Switches it now, mid-chat")
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        val box = EditText(activity).apply {
            hint = "Filter models"
            setHintTextColor(colour(R.color.muted)); setTextColor(colour(R.color.text)); textSize = 15f; setSingleLine()
            background = rounded(colour(R.color.navy), 16f, 1, colour(R.color.surface_high))
            setPadding(px(16), px(12), px(16), px(12))
        }
        fun list(query: String) {
            rows.removeAllViews()
            val q = query.trim().lowercase()
            val shown = (if (withDefault) listOf(JSONObject().put("id", "").put("name", "Default")) else emptyList()) +
                models.filter { q.isEmpty() || q in it.optString("name").lowercase() || q in it.optString("id").lowercase() }
            for (m in shown.take(80)) rows.addView(TextView(activity).apply {
                text = m.optString("name")
                setTextColor(colour(R.color.text)); textSize = 16f
                setPadding(px(16), px(14), px(16), px(14))
                background = rounded(colour(R.color.navy), 14f)
                setOnClickListener { pop(this); picked(m.optString("id"), m.optString("name")); close() }
            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(6) })
        }
        box.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun afterTextChanged(s: Editable?) = list(s?.toString() ?: "")
        })
        if (models.size > 8) sheet.addView(box, wide(14))
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(rows) },
            wide(10, (activity.resources.displayMetrics.heightPixels * 0.5).toInt()))
        sheet.addView(quiet("Cancel") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
        list("")
    }
}

/** The + in the message box (the Claude app's "Add context"): Camera, Photos, Files, as tiles. */
class AddSheet(activity: Activity, private val picked: (String) -> Unit) : BottomPanel(activity) {
    fun show() {
        val sheet = open("Add", "For the agent whose chat this is")
        val tiles = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
        for ((icon, name) in listOf("📷" to "Camera", "🖼" to "Photos", "📄" to "Files")) {
            tiles.addView(LinearLayout(activity).apply {
                orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER
                setPadding(px(8), px(18), px(8), px(18))
                background = rounded(colour(R.color.navy), 18f, 1, colour(R.color.surface_high))
                addView(TextView(activity).apply { text = icon; textSize = 26f; gravity = Gravity.CENTER })
                addView(TextView(activity).apply {
                    text = name; setTextColor(colour(R.color.text)); textSize = 15f; gravity = Gravity.CENTER
                    setPadding(0, px(6), 0, 0)
                })
                setOnClickListener { pop(this); close(); picked(name) }
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply {
                marginEnd = if (name != "Files") px(10) else 0
            })
        }
        sheet.addView(tiles, wide(18))
        sheet.addView(quiet("Cancel") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }
}

/**
 * A Cursor IDE chat's model, as Cursor's own menu has it (the user's wish, 2026-11-16, shown from his screen): rows
 * for Model, Effort, Context and a Fast switch, each choice from Cursor's catalog for that model. Each change is
 * sent at once, the whole setting, and the rows follow what the chat is then on.
 */
class CursorModelMenu(
    activity: Activity,
    private val menu: List<JSONObject>,  // {id, name, params: [{id, name, kind, values: [{value, name}]}]}
    private var model: String,
    private val settings: MutableMap<String, String>,
    private val title: String = "Model",
    private val subtitle: String = "As in Cursor: the chat switches as you pick",
    private val apply: (model: String, params: Map<String, String>, done: (String?) -> Unit) -> Unit,
) : BottomPanel(activity) {
    private lateinit var rows: LinearLayout
    private lateinit var note: TextView

    fun show() {
        val sheet = open(title, subtitle)
        rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        sheet.addView(rows, wide(14))
        note = TextView(activity).apply { setTextColor(colour(R.color.muted)); textSize = 13f; gravity = Gravity.CENTER }
        sheet.addView(note, wide(10))
        sheet.addView(quiet("Done") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(4) })
        draw()
    }

    private fun entry() = menu.firstOrNull { it.optString("id") == model }

    private fun params(): List<JSONObject> =
        entry()?.optJSONArray("params")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList()

    private fun values(param: JSONObject): List<JSONObject> =
        param.optJSONArray("values")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList()

    private fun draw() {
        rows.removeAllViews()
        rows.addView(row("Model", entry()?.optString("name") ?: model) {
            ModelPicker(activity, "Model", menu.map { JSONObject().put("id", it.optString("id")).put("name", it.optString("name")) },
                withDefault = false) { id, _ ->
                model = id
                // Its own settings: kept where the new model has the same choice, else its first
                val kept = mutableMapOf<String, String>()
                for (p in params()) {
                    val options = values(p).map { it.optString("value") }
                    kept[p.optString("id")] = settings[p.optString("id")]?.takeIf { it in options } ?: options.firstOrNull() ?: ""
                }
                settings.clear(); settings.putAll(kept)
                send()
            }.show()
        })
        // Fast last, as in Cursor
        for (p in params().sortedBy { it.optString("kind") == "bool" }) {
            val id = p.optString("id")
            val options = values(p)
            if (p.optString("kind") == "bool") {
                val on = settings[id] == "true"
                rows.addView(row(p.optString("name"), if (on) "On" else "Off") {
                    settings[id] = if (on) "false" else "true"; send()
                })
            } else {
                val shown = options.firstOrNull { it.optString("value") == settings[id] }?.optString("name") ?: settings[id] ?: ""
                rows.addView(row(p.optString("name"), shown) {
                    ModelPicker(activity, p.optString("name"), options.map { JSONObject().put("id", it.optString("value")).put("name", it.optString("name")) },
                        withDefault = false) { value, _ -> settings[id] = value; send() }.show()
                })
            }
        }
    }

    private fun send() {
        draw()
        note.text = "Switching…"
        apply(model, settings.toMap()) { why ->
            note.text = why ?: "Done."
            note.setTextColor(colour(if (why == null || why.startsWith("Switched")) R.color.muted else R.color.hang))
        }
    }

    private fun row(name: String, value: String, then: () -> Unit) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        setPadding(px(16), px(14), px(16), px(14))
        background = rounded(colour(R.color.navy), 14f)
        addView(TextView(activity).apply { text = name; setTextColor(colour(R.color.text)); textSize = 16f },
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        addView(TextView(activity).apply { text = "$value  ›"; setTextColor(colour(R.color.cyan)); textSize = 15f })
        setOnClickListener { pop(this); then() }
    }.also { it.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(6) } }
}

/**
 * Long-press on a message (the user's wish, 2026-11-16): copy its words, open its file in another app, or delete it
 * from this phone, with the file it saved. There is no deleting for everyone: words typed into an agent are in its
 * own conversation at once, and the sheet says so.
 */
class MessageSheet(
    activity: Activity,
    private val title: String,
    private val subtitle: String,
    private val words: String?,  // something to copy
    private val onOpen: (() -> Unit)?,  // a file: saved, and opened in another app
    private val mine: Boolean,
    private val savedFiles: Int,  // its saved copies that no other message has
    private val onSelect: (() -> Unit)? = null,  // pick more messages, to delete or copy them together
    private val onDelete: (alsoFiles: Boolean) -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open(title, subtitle)
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        sheet.addView(rows, wide(16))
        onSelect?.let { select -> rows.addView(actionRow("☑", "Select", "Pick more messages, then delete or copy them together",
            R.color.text) { close(); select() }) }
        if (!words.isNullOrEmpty()) rows.addView(actionRow("📋", "Copy", "", R.color.text) {
            val clip = activity.getSystemService(android.content.ClipboardManager::class.java)
            clip.setPrimaryClip(android.content.ClipData.newPlainText("Henchline", words))
            close()
        })
        onOpen?.let { open -> rows.addView(actionRow("📂", "Open in another app", "Saved to Downloads/Henchline first", R.color.text) {
            close(); open()
        }) }
        rows.addView(actionRow("🗑", "Delete for me", if (mine) "From this phone: the agent already has your words"
                else "From this phone only", R.color.hang) {
            deleteConfirm(rows, "Delete this from the chat on this phone?", savedFiles, "Delete", onDelete)
        })
        sheet.addView(quiet("Cancel") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }
}

/**
 * /later (the user's idea, 2026-11-17): when the words should reach the agent. A few quick choices, or a time
 * picked; for a wake-up, the laptop can be kept from sleeping until then. Below, this chat's scheduled messages, each
 * with Cancel.
 */
class LaterSheet(
    activity: Activity,
    private val words: String,
    private val pending: List<org.json.JSONObject>,
    private val onPick: (at: Long, keepAwake: Boolean) -> Unit,
    private val onCancel: (id: String) -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open(if (words.isEmpty()) "Scheduled messages" else "Send later",
            if (words.isEmpty()) "Type /later and your words to schedule one" else "\u201c${words.take(80)}\u201d")
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        sheet.addView(rows, wide(16))
        if (words.isNotEmpty()) {
            val awake = android.widget.CheckBox(activity).apply {
                text = "Keep the laptop awake until then (a wake-up)"
                setTextColor(colour(R.color.text)); textSize = 14f
                buttonTintList = android.content.res.ColorStateList.valueOf(colour(R.color.cyan))
            }
            val now = java.util.Calendar.getInstance()
            fun at(hour: Int, minute: Int, tomorrow: Boolean): Long = (now.clone() as java.util.Calendar).apply {
                set(java.util.Calendar.HOUR_OF_DAY, hour); set(java.util.Calendar.MINUTE, minute); set(java.util.Calendar.SECOND, 0)
                set(java.util.Calendar.MILLISECOND, 0)
                if (tomorrow || timeInMillis <= System.currentTimeMillis()) add(java.util.Calendar.DAY_OF_MONTH, 1)
            }.timeInMillis
            val choices = listOf(
                "In 15 minutes" to { System.currentTimeMillis() + 15 * 60_000L },
                "In 1 hour" to { System.currentTimeMillis() + 60 * 60_000L },
                "Tomorrow 08:30" to { at(8, 30, true) },
            )
            for ((label, time) in choices) rows.addView(actionRow("⏰", label, "", R.color.text) {
                close(); onPick(time(), awake.isChecked)
            })
            rows.addView(actionRow("🕘", "Pick a time…", "Today, or tomorrow if it has passed", R.color.text) {
                android.app.TimePickerDialog(activity, R.style.Clock, { _, hour, minute ->
                    close(); onPick(at(hour, minute, false), awake.isChecked)
                }, now.get(java.util.Calendar.HOUR_OF_DAY), now.get(java.util.Calendar.MINUTE),
                    android.text.format.DateFormat.is24HourFormat(activity)).show()
            })
            rows.addView(awake, wide(4))
        }
        if (pending.isNotEmpty()) {
            rows.addView(label("SCHEDULED IN THIS CHAT"), wide(16))
            for (item in pending) {
                val time = android.text.format.DateFormat.getTimeFormat(activity).format(java.util.Date((item.optDouble("at") * 1000).toLong()))
                val day = if (android.text.format.DateUtils.isToday((item.optDouble("at") * 1000).toLong())) "" else "tomorrow or later · "
                val row = actionRow("⏰", "$day$time", item.optString("words").take(60) + "  · tap to cancel", R.color.text) {
                    onCancel(item.optString("id")); close()
                }
                rows.addView(row)
            }
        } else if (words.isEmpty()) {
            rows.addView(TextView(activity).apply {
                text = "Nothing scheduled in this chat."; setTextColor(colour(R.color.muted)); textSize = 14f
            })
        }
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }
}

/**
 * Every Cursor chat the laptop has (the user's wish, 2026-11-17: a quiet chat in Cursor's list, "Lolo-tuper action
 * update", couldn't be reached from the phone), newest first, with a search: picking one opens it in Cursor (its
 * window too, when it's closed) and puts it on the crew.
 */
class CursorChatsSheet(
    activity: Activity,
    private val chats: List<JSONObject>,  // {session, name, where, updated, status, open}
    private val pick: (session: String, done: (String?) -> Unit) -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open("Cursor chats", "Opens it in Cursor on the laptop, then you can talk to it here")
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        val status = TextView(activity).apply { textSize = 13f; setTextColor(colour(R.color.muted)); visibility = View.GONE }
        fun ago(ms: Long): String {
            val minutes = (System.currentTimeMillis() - ms) / 60_000
            return when {
                ms <= 0 -> ""
                minutes < 60 -> "${minutes.coerceAtLeast(1)} min ago"
                minutes < 48 * 60 -> "${minutes / 60} h ago"
                else -> "${minutes / (24 * 60)} days ago"
            }
        }
        fun list(query: String) {
            rows.removeAllViews()
            val q = query.trim().lowercase()
            val shown = chats.filter { q.isEmpty() || q in it.optString("name").lowercase() || q in it.optString("where").lowercase() }
            for (chat in shown.take(80)) {
                val busy = chat.optString("status") == "generating"
                val detail = listOf(chat.optString("where").replace("_", " "), ago(chat.optLong("updated")),
                    if (busy) "working" else if (chat.optBoolean("open")) "window open" else "").filter { it.isNotEmpty() }.joinToString(" · ")
                rows.addView(actionRow(if (busy) "⚡" else "💬", chat.optString("name"), detail, R.color.text) {
                    status.text = "Opening “${chat.optString("name")}” in Cursor…"
                    status.setTextColor(colour(R.color.muted)); status.visibility = View.VISIBLE
                    pick(chat.optString("session")) { why ->
                        if (why == null) close()
                        else { status.text = why; status.setTextColor(colour(R.color.hang)) }
                    }
                })
            }
            if (shown.isEmpty()) rows.addView(TextView(activity).apply {
                text = if (chats.isEmpty()) "No Cursor chats found on the laptop." else "No chat matches."
                setTextColor(colour(R.color.muted)); textSize = 13f; setPadding(px(4), px(12), px(4), px(12))
            })
        }
        val box = EditText(activity).apply {
            hint = "Search by name or project"
            setHintTextColor(colour(R.color.muted)); setTextColor(colour(R.color.text)); textSize = 15f; setSingleLine()
            background = rounded(colour(R.color.navy), 16f, 1, colour(R.color.surface_high))
            setPadding(px(16), px(12), px(16), px(12))
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun afterTextChanged(s: Editable?) = list(s?.toString() ?: "")
            })
        }
        sheet.addView(box, wide(14))
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(rows) },
            wide(10, (activity.resources.displayMetrics.heightPixels * 0.5).toInt()))
        sheet.addView(status, wide(8))
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
        list("")
    }
}

/**
 * Usage drawn as bars (the user's wish, 2026-11-17: not a text dump), for the /usage sheet and the Usage tab: each
 * plan limit with when it resets, the context with what fills it. Green while there's room, amber past 70 %, red past
 * 90 %.
 */
class UsageDraw(private val activity: Activity) {
    private val dp = activity.resources.displayMetrics.density
    private fun px(v: Int) = (v * dp).toInt()
    private fun colour(id: Int) = activity.getColor(id)
    private fun rounded(fill: Int, radius: Float) = GradientDrawable().apply { setColor(fill); cornerRadius = radius * dp }
    private fun wide(top: Int = 0, height: Int = ViewGroup.LayoutParams.WRAP_CONTENT) =
        LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, height).apply { topMargin = px(top) }
    private val palette = listOf(0xFF2BC4D9.toInt(), 0xFF8E7CFF.toInt(), 0xFFF5A623.toInt(), 0xFF3DBE6B.toInt(),
        0xFFE5484D.toInt(), 0xFF5B9BFF.toInt(), 0xFFD77BD0.toInt(), 0xFF9AA3B0.toInt())

    fun level(percent: Double) = when {
        percent >= 90 -> colour(R.color.hang)
        percent >= 70 -> 0xFFF5A623.toInt()
        else -> 0xFF3DBE6B.toInt()
    }

    fun tokens(n: Long) = when {
        n >= 1_000_000 -> String.format(java.util.Locale.US, "%.1fM", n / 1e6).replace(".0M", "M")
        n >= 1_000 -> "${n / 1000}K"
        else -> "$n"
    }

    fun resets(at: Long): String {
        if (at <= 0) return ""
        val minutes = (at * 1000 - System.currentTimeMillis()) / 60_000
        return when {
            minutes <= 0 -> "Resets now"
            minutes < 60 -> "Resets in $minutes min"
            minutes < 24 * 60 -> "Resets in ${minutes / 60} h ${minutes % 60} min"
            minutes < 7 * 24 * 60 -> "Resets " + android.text.format.DateFormat.format("EEE HH:mm", at * 1000) +
                " · in ${minutes / (24 * 60)} d ${minutes % (24 * 60) / 60} h"
            else -> "Resets " + android.text.format.DateFormat.format("d MMM", at * 1000) + " · in ${minutes / (24 * 60)} days"
        }
    }

    /** A rounded track, filled to the share given, in segments when there are several. */
    fun bar(segments: List<Pair<Double, Int>>) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        background = rounded(colour(R.color.surface_high), 6f)
        clipToOutline = true
        outlineProvider = android.view.ViewOutlineProvider.BACKGROUND
        var used = 0.0
        for ((share, tint) in segments) {
            val part = share.coerceIn(0.0, 100.0 - used)
            if (part <= 0) continue
            used += part
            addView(View(activity).apply { setBackgroundColor(tint) },
                LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, part.toFloat()))
        }
        if (used < 100) addView(View(activity), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, (100 - used).toFloat()))
    }

    fun heading(title: String, right: String, rightColour: Int) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.BOTTOM
        addView(TextView(activity).apply { text = title; setTextColor(colour(R.color.text)); textSize = 15f },
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        addView(TextView(activity).apply { text = right; setTextColor(rightColour); textSize = 15f; typeface = Typeface.DEFAULT_BOLD })
    }

    fun small(words: String) = TextView(activity).apply {
        text = words; setTextColor(colour(R.color.muted)); textSize = 12f; setPadding(0, px(4), 0, 0)
    }

    fun label(words: String) = TextView(activity).apply {
        text = words; setTextColor(colour(R.color.muted)); textSize = 12f; letterSpacing = 0.08f; typeface = Typeface.DEFAULT_BOLD
    }

    /** Each limit: its name and how much is used, the bar, when it resets. */
    fun limits(into: LinearLayout, limits: org.json.JSONArray?) {
        for (i in 0 until (limits?.length() ?: 0)) {
            val limit = limits!!.getJSONObject(i)
            val percent = limit.optDouble("percent")
            into.addView(heading(limit.optString("label"), "${percent.toInt()}% used", level(percent)), wide(12))
            into.addView(bar(listOf(percent to level(percent))), wide(6, px(12)))
            resets(limit.optLong("resets")).takeIf { it.isNotEmpty() }?.let { into.addView(small(it)) }
        }
    }

    /** How full the context is; with what fills it, when that's known. */
    fun context(into: LinearLayout, context: JSONObject) {
        val percent = context.optDouble("percent")
        val size = context.optLong("size")
        val used = context.optLong("used")
        into.addView(heading(if (size > 0) "${tokens(used)} of ${tokens(size)} tokens" else "Context",
            "${percent.toInt()}%", level(percent)), wide(12))
        val parts = context.optJSONArray("parts")
        if (parts != null && parts.length() > 0 && size > 0) {
            val list = (0 until parts.length()).map { parts.getJSONObject(it) }
            into.addView(bar(list.mapIndexed { i, p -> 100.0 * p.optLong("tokens") / size to palette[i % palette.size] }), wide(6, px(14)))
            for ((i, part) in list.withIndex()) into.addView(LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                addView(View(activity).apply { background = rounded(palette[i % palette.size], 3f) }, LinearLayout.LayoutParams(px(10), px(10)))
                addView(TextView(activity).apply {
                    text = part.optString("label"); setTextColor(colour(R.color.text)); textSize = 13f; setPadding(px(8), 0, 0, 0)
                }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                addView(TextView(activity).apply { text = tokens(part.optLong("tokens")); setTextColor(colour(R.color.muted)); textSize = 13f })
            }, wide(6))
        } else into.addView(bar(listOf(percent to level(percent))), wide(6, px(12)))
        if (size > 0) into.addView(small("Free: ${tokens((size - used).coerceAtLeast(0))}"))
    }

    fun notes(into: LinearLayout, notes: org.json.JSONArray?) {
        for (i in 0 until (notes?.length() ?: 0)) into.addView(small(notes!!.getString(i)))
    }
}

/** /usage and /context in a chat: that agent's plan limits and its context, as bars. */
class UsageSheet(activity: Activity, private val usage: JSONObject) : BottomPanel(activity) {
    fun show() {
        val draw = UsageDraw(activity)
        val agent = usage.optString("agent").replaceFirstChar { it.uppercase() }
        val sheet = open("$agent usage", usage.optString("model"))
        val body = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        val limits = usage.optJSONArray("limits")
        if (limits != null && limits.length() > 0) {
            body.addView(label("PLAN LIMITS"), wide(16))
            draw.limits(body, limits)
        }
        usage.optJSONObject("context")?.let { context ->
            body.addView(label("CONTEXT"), wide(20))
            draw.context(body, context)
        }
        body.addView(View(activity), wide(10, px(1)))
        draw.notes(body, usage.optJSONArray("notes"))
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(body) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }
}

/** One delegated job: every child and its ask, reports, and user corrections in their recorded order. */
class MinionSheet(
    activity: Activity,
    private val chain: JSONObject,
    private val onOpen: (String) -> Unit,
    private val onDecide: (String, String) -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open("Delegated agents", "${chain.optInt("used")} of ${chain.optInt("slots")} slots in use")
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        val minions = chain.optJSONArray("minions")
        for (i in 0 until (minions?.length() ?: 0)) {
            val item = minions!!.getJSONObject(i)
            val session = item.optString("session")
            val title = listOf(item.optString("agent").replaceFirstChar { it.uppercase() },
                item.optString("where").replace("_", " ")).filter { it.isNotEmpty() }.joinToString(" · ")
            rows.addView(actionRow(if (item.optString("state") == "ended") "✓" else "●", title,
                if (session.isEmpty()) "Starting…" else if (item.optString("state") == "ended") "Ended" else "Open chat",
                R.color.text) { if (session.isNotEmpty()) { close(); onOpen(session) } }, wide(if (i == 0) 12 else 18))
            if (item.has("end_waiting") && session.isNotEmpty()) {
                val choices = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.END }
                choices.addView(quiet("Keep") { close(); onDecide(session, "keep") })
                choices.addView(primary("End", colour(R.color.hang), Color.WHITE).apply {
                    setOnClickListener { close(); onDecide(session, "end") }
                }, LinearLayout.LayoutParams(px(120), px(50)).apply { marginStart = px(8) })
                rows.addView(choices, wide(10))
            }
        }
        val handoffs = chain.optJSONArray("handoffs")
        if (handoffs != null && handoffs.length() > 0) rows.addView(label("HAND-OFFS IN ORDER"), wide(20))
        for (i in 0 until (handoffs?.length() ?: 0)) {
            val handoff = handoffs!!.getJSONObject(i)
            val kind = when (handoff.optString("kind")) {
                "ask" -> "ASK"; "report" -> "REPORT"; else -> "YOUR CORRECTION"
            }
            val who = listOf(handoff.optString("agent").replaceFirstChar { it.uppercase() },
                handoff.optString("where").replace("_", " ")).filter { it.isNotEmpty() }.joinToString(" · ")
            rows.addView(label(listOf(kind, who).filter { it.isNotEmpty() }.joinToString(" · ")), wide(8))
            rows.addView(TextView(activity).apply {
                text = handoff.optString("text"); setTextColor(colour(R.color.text)); textSize = 14f
                setTextIsSelectable(true); setPadding(px(12), px(8), px(12), px(10))
                background = rounded(colour(R.color.navy), 14f)
            }, wide(4))
        }
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(rows) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
                height = minOf(px(560), (activity.resources.displayMetrics.heightPixels * 0.68).toInt()) })
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }
}

/** A child is still open or has fresh words: ask before its pane is closed. */
class MinionEndSheet(activity: Activity, private val item: JSONObject, private val decide: (String) -> Unit) : BottomPanel(activity) {
    fun show() {
        val who = listOf(item.optString("agent").replaceFirstChar { it.uppercase() },
            item.optString("where").replace("_", " ")).filter { it.isNotEmpty() }.joinToString(" · ")
        val sheet = open("End delegated chat?", who)
        sheet.addView(TextView(activity).apply {
            text = "This chat is open or has words that have not been handed back yet. End sends those words to the boss first; Keep leaves it running."
            setTextColor(colour(R.color.text)); textSize = 15f; setPadding(0, px(18), 0, px(12))
        })
        val choices = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.END }
        choices.addView(quiet("Keep") { close(); decide("keep") })
        choices.addView(primary("End", colour(R.color.hang), Color.WHITE).apply {
            setOnClickListener { close(); decide("end") }
        }, LinearLayout.LayoutParams(px(120), px(52)).apply { marginStart = px(8) })
        sheet.addView(choices, wide(8))
    }
}

/**
 * A workflow's phases and agents (the design review's pick, 2026-11-17): each phase with a bar of its agents (done
 * green, failed red, running cyan) and a row per agent; a tap shows what the agent found, and "Ask Claude about
 * this" starts a message about it. Kept up to date every 2 s while open and running.
 */
class WorkflowSheet(
    activity: Activity,
    private var run: JSONObject,
    private val refresh: (then: (JSONObject) -> Unit) -> Unit,
    private val askClaude: (String) -> Unit,
) : BottomPanel(activity) {
    private val draw = UsageDraw(activity)
    private lateinit var rows: LinearLayout
    private var showing: JSONObject? = null  // the agent opened, or null for the phases
    private val handler = android.os.Handler(android.os.Looper.getMainLooper())
    private val poll = object : Runnable {
        override fun run() {
            if (!isOpen) return
            refresh { fresh -> if (isOpen && fresh.optString("id") == run.optString("id")) { run = fresh; redraw() } }
            if (run.optString("state") == "running") handler.postDelayed(this, 2000)
        }
    }

    private fun stateText() = when (run.optString("state")) {
        "running" -> "Running · ${run.optInt("done")}/${run.optInt("total")} agents done"
        "failed" -> "Failed · ${run.optInt("done")}/${run.optInt("total")} agents done"
        "stopped" -> "Stopped · ${run.optInt("done")}/${run.optInt("total")} agents done"
        else -> "Finished · ${run.optInt("total")} agents"
    }

    fun show() {
        val sheet = open("⚙ " + run.optString("name"), stateText())
        rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(rows) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
                height = minOf(px(520), (activity.resources.displayMetrics.heightPixels * 0.6).toInt()) })
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
        redraw()
        if (run.optString("state") == "running") handler.postDelayed(poll, 2000)
    }

    private fun glyph(status: String) = when (status) {
        "done", "empty" -> "✓"; "failed" -> "✗"; "running" -> "●"; else -> "○"
    }

    private fun redraw() {
        subtitleView?.text = stateText()
        rows.removeAllViews()
        showing?.let { agent -> return detail(agent) }
        val phases = run.optJSONArray("phases") ?: return
        for (i in 0 until phases.length()) {
            val phase = phases.getJSONObject(i)
            val agents = phase.optJSONArray("agents")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList()
            val done = agents.count { it.optString("status") in setOf("done", "empty") }
            val failed = agents.count { it.optString("status") == "failed" }
            val running = agents.count { it.optString("status") == "running" }
            rows.addView(draw.heading("${i + 1}  ${phase.optString("title")}", "$done/${agents.size}",
                if (failed > 0) colour(R.color.hang) else colour(R.color.muted)), wide(if (i == 0) 12 else 18))
            if (agents.isNotEmpty()) {
                val each = 100.0 / agents.size
                rows.addView(draw.bar(listOf(done * each to 0xFF3DBE6B.toInt(), failed * each to colour(R.color.hang),
                    running * each to 0xFF2BC4D9.toInt())), wide(6, px(8)))
            }
            // Failed ones first: what needs looking at
            for (agent in agents.sortedBy { if (it.optString("status") == "failed") 0 else 1 }) {
                val result = agent.optString("result").lineSequence().firstOrNull { it.isNotBlank() }?.take(70) ?: ""
                rows.addView(actionRow(glyph(agent.optString("status")), agent.optString("label"),
                    result.ifEmpty { if (agent.optString("status") == "running") "working…" else agent.optString("status") },
                    if (agent.optString("status") == "failed") R.color.hang else R.color.text) {
                    showing = agent; redraw()
                })
            }
        }
        val logs = run.optJSONArray("logs")
        if (logs != null && logs.length() > 0) {
            rows.addView(label("LOG"), wide(16))
            for (i in 0 until logs.length()) rows.addView(draw.small(logs.getString(i).take(200)))
        }
    }

    /** One agent: what it found, in full, to read and copy; ask Claude about it; back to the phases. */
    private fun detail(agent: JSONObject) {
        rows.addView(draw.heading("${glyph(agent.optString("status"))}  ${agent.optString("label")}",
            agent.optString("phase"), colour(R.color.muted)), wide(8))
        rows.addView(TextView(activity).apply {
            text = agent.optString("result").ifEmpty { if (agent.optString("status") == "running") "Still working." else "No result." }
            setTextColor(colour(R.color.text)); textSize = 14f; setTextIsSelectable(true); setPadding(0, px(10), 0, px(6))
        })
        rows.addView(actionRow("💬", "Ask Claude about this", "Starts a message about this agent", R.color.cyan) {
            close(); askClaude("About the workflow's agent \u201c${agent.optString("label")}\u201d (${agent.optString("phase")}): ")
        }, wide(8))
        rows.addView(quiet("‹ Back") { showing = null; redraw() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT))
    }
}

/** Holding your avatar (the design review, 2026-11-17): you, Available or Away, and the way into Settings. */
class YouSheet(
    activity: Activity,
    private val name: String,
    private val away: Boolean,
    private val onAway: (Boolean) -> Unit,
    private val onSettings: () -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open(name, if (away) "Away: agents text you, the laptop isn't listening" else "Available")
        val track = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            background = rounded(colour(R.color.surface), 20f); setPadding(px(3), px(3), px(3), px(3))
        }
        for ((label, on) in listOf("Available" to false, "Away" to true)) track.addView(TextView(activity).apply {
            text = label; gravity = Gravity.CENTER; textSize = 14f
            setTextColor(colour(if (on == away) R.color.text else R.color.muted))
            if (on == away) background = rounded(colour(R.color.surface_high), 17f)
            setOnClickListener { close(); onAway(on) }
        }, LinearLayout.LayoutParams(0, px(38), 1f))
        sheet.addView(track, wide(16))
        sheet.addView(actionRow("⚙", "Settings", "Calls, chats, your laptop", R.color.text) { close(); onSettings() }, wide(14))
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }
}

/** How the agents sound (the user's wish, 2026-11-17): each its own, one voice, or a surprise that stays. */
class VoicesSheet(
    activity: Activity,
    private var setup: JSONObject,
    private val save: (JSONObject, (JSONObject) -> Unit) -> Unit,
    private val preview: (String) -> Unit,
) : BottomPanel(activity) {
    private val agents = listOf("claude" to "Claude", "cursor" to "Cursor", "codex" to "Codex")

    fun show() {
        val sheet = open("Voices", "How the agents sound")
        val body = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        draw(body)
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(body) }, wide(8))
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }

    private fun draw(body: LinearLayout) {
        body.removeAllViews()
        val mode = setup.optString("mode", "own")
        val track = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            background = rounded(colour(R.color.surface), 20f); setPadding(px(3), px(3), px(3), px(3))
        }
        for ((id, label) in listOf("own" to "Each its own", "one" to "One voice", "surprise" to "Surprise me")) {
            track.addView(TextView(activity).apply {
                text = label; gravity = Gravity.CENTER; textSize = 12.5f
                setTextColor(colour(if (id == mode) R.color.text else R.color.muted))
                if (id == mode) background = rounded(colour(R.color.surface_high), 17f)
                setOnClickListener {
                    val change = JSONObject().put("mode", id)
                    if (id == "surprise") change.put("again", true)
                    save(change) { setup = it; draw(body) }
                }
            }, LinearLayout.LayoutParams(0, px(40), 1f))
        }
        body.addView(track, wide(4))
        val showing = setup.optJSONObject("showing")
        val choices = setup.optJSONArray("choices")
        for ((id, name) in agents) {
            val voice = showing?.optJSONObject(id)
            val voiceId = voice?.optString("id").orEmpty()
            val voiceName = voice?.optString("name").orEmpty().ifEmpty { voiceId }
            body.addView(LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                setPadding(0, px(10), 0, px(4))
                addView(ImageView(activity).apply { setImageResource(MainActivity.avatarOf(id)) },
                    LinearLayout.LayoutParams(px(40), px(40)))
                addView(LinearLayout(activity).apply {
                    orientation = LinearLayout.VERTICAL
                    addView(TextView(activity).apply { text = name; setTextColor(colour(R.color.text)); textSize = 15f })
                    addView(TextView(activity).apply {
                        text = voiceName; setTextColor(colour(R.color.cyan)); textSize = 13f
                        if (mode != "surprise" && choices != null) setOnClickListener {
                            val next = nextVoice(choices, voiceId)
                            val change = JSONObject().put("mode", mode).put("voice", next)
                            if (mode == "own") change.put("agent", id)
                            save(change) { setup = it; draw(body) }
                        }
                    })
                }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { marginStart = px(12) })
                addView(TextView(activity).apply {
                    text = "▶"; textSize = 18f; setTextColor(colour(R.color.cyan)); setPadding(px(12), px(8), px(4), px(8))
                    contentDescription = "Play $name"
                    setOnClickListener { if (voiceId.isNotEmpty()) preview(voiceId) }
                })
            })
        }
    }

    private fun nextVoice(choices: org.json.JSONArray, current: String): String {
        val ids = (0 until choices.length()).map { choices.getJSONObject(it).optString("id") }.filter { it.isNotEmpty() }
        if (ids.isEmpty()) return current
        val at = ids.indexOf(current)
        return ids[(at + 1) % ids.size]
    }
}

/** /show: this terminal, the whole screen, or one window. A window is grabbed by its id, so a locked screen still shows it. */
class ShowSheet(
    activity: Activity,
    private val windows: List<JSONObject>,
    private val onPick: (String, String) -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open("Show", "A picture into this chat")
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        rows.addView(actionRow("▭", "This terminal", "Drawn from the terminal, locked or not", R.color.cyan) {
            close(); onPick("pane", "")
        })
        rows.addView(actionRow("▣", "The screen", "The whole screen. Locked, this is the lock screen", R.color.text) {
            close(); onPick("screen", "")
        }, wide(8))
        if (windows.isEmpty()) rows.addView(TextView(activity).apply {
            text = "No other windows."; textSize = 13f; setTextColor(colour(R.color.muted))
        }, wide(12))
        else {
            rows.addView(label("WINDOWS"), wide(16))
            for (window in windows) rows.addView(actionRow("▢", window.optString("app").ifEmpty { "Window" },
                window.optString("title").ifEmpty { window.optString("id") }, R.color.text) {
                close(); onPick("window", window.optString("id"))
            })
        }
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(rows) }, wide(8))
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }
}
