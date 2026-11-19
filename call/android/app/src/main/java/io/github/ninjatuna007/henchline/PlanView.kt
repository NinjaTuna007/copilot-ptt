package io.github.ninjatuna007.henchline

import android.app.Activity
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.Html
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject

/**
 * The Plan tab (the design review, 2026-11-17: "not visually very appealing" before): a header card with the
 * plan's name, overview and progress; a steps card where done, in progress and to-do each look their own (done
 * ones folded away when there are many); then the document, laid out one view per block from the laptop's
 * blocks: headings, paragraphs, lists, code on a rounded panel, diagrams as a card, tables as stacked cards.
 */
class PlanView(private val activity: Activity) {
    private val draw = UsageDraw(activity)
    private val density = activity.resources.displayMetrics.density
    private fun dp(v: Int) = (v * density).toInt()
    private fun colour(id: Int) = activity.getColor(id)
    private fun rounded(fill: Int, radius: Int, stroke: Int = 0, strokeColour: Int = 0) = GradientDrawable().apply {
        setColor(fill); cornerRadius = radius * density
        if (stroke > 0) setStroke(dp(stroke), strokeColour)
    }
    private fun wide(top: Int = 0, bottom: Int = 0) =
        LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
            topMargin = dp(top); bottomMargin = dp(bottom)
        }
    private val reading = 0xFFD5DDEB.toInt()
    private var showDone = false
    private var seededFile: String? = null  // null until the first plan: an empty path is still a plan

    fun fill(list: LinearLayout, plan: JSONObject) {
        list.removeAllViews()
        val todos = plan.optJSONArray("todos") ?: JSONArray()
        val steps = (0 until todos.length()).mapNotNull { todos.optJSONObject(it) }
        if (seededFile != plan.optString("file")) {
            seededFile = plan.optString("file")
            showDone = false  // finished steps start folded away
        }
        list.addView(header(plan, steps), wide(8))
        if (steps.isNotEmpty()) {
            list.addView(draw.label("STEPS"), wide(20, 8))
            list.addView(stepsCard(steps, list, plan), wide())
        }
        val blocks = plan.optJSONArray("blocks")
        if (blocks != null && blocks.length() > 0) {
            list.addView(draw.label("DOCUMENT"), wide(28, 4))
            for (i in 0 until blocks.length()) blocks.optJSONObject(i)?.let { block(list, it, i) }
        }
    }

    private fun card() = LinearLayout(activity).apply {
        orientation = LinearLayout.VERTICAL
        background = rounded(colour(R.color.surface), 18)
    }

    private fun header(plan: JSONObject, steps: List<JSONObject>) = card().apply {
        setPadding(dp(18), dp(16), dp(18), dp(16))
        addView(draw.label("PLAN"))
        addView(TextView(activity).apply {
            text = plan.optString("name"); textSize = 20f; setTextColor(colour(R.color.text))
            typeface = Typeface.DEFAULT_BOLD; setLineSpacing(0f, 1.2f)
        }, wide(4))
        plan.optString("overview").takeIf { it.isNotEmpty() }?.let { overview ->
            addView(TextView(activity).apply {
                text = overview; textSize = 14f; setTextColor(colour(R.color.muted)); setLineSpacing(0f, 1.3f)
                maxLines = 3; ellipsize = android.text.TextUtils.TruncateAt.END
                setOnClickListener { maxLines = if (maxLines == 3) Int.MAX_VALUE else 3 }
            }, wide(6))
        }
        if (steps.isNotEmpty()) {
            val done = steps.count { it.optString("status") == "completed" }
            val going = steps.count { it.optString("status") == "in_progress" }
            val total = steps.size
            addView(draw.heading("Progress", "$done of $total", colour(R.color.cyan)), wide(14))
            addView(draw.bar(listOf(done * 100.0 / total to colour(R.color.cyan),
                going * 100.0 / total to (colour(R.color.cyan) and 0x66FFFFFF))),
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(6)).apply { topMargin = dp(8) })
            addView(draw.small(listOfNotNull(if (going > 0) "$going in progress" else null,
                if (total - done - going > 0) "${total - done - going} to go" else null).joinToString(" · ")
                .ifEmpty { "All done" }))
        }
    }

    /** A step's mark: done (a soft cyan disc with a tick), in progress (a dot in a ring), to do (an empty ring). */
    private fun mark(status: String, size: Int = 20) = TextView(activity).apply {
        gravity = Gravity.CENTER; textSize = size * 0.55f
        when (status) {
            "completed" -> { text = "✓"; setTextColor(colour(R.color.navy)); typeface = Typeface.DEFAULT_BOLD
                background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(colour(R.color.cyan) and 0x4DFFFFFF) } }
            "in_progress" -> { text = "●"; setTextColor(colour(R.color.cyan)); textSize = size * 0.45f
                background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setStroke((1.5f * density).toInt(), colour(R.color.cyan)) } }
            else -> background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setStroke((1.5f * density).toInt(), colour(R.color.muted)) }
        }
    }.also { it.layoutParams = LinearLayout.LayoutParams(dp(size), dp(size)) }

    private fun stepsCard(steps: List<JSONObject>, list: LinearLayout, plan: JSONObject): View {
        val card = card().apply { setPadding(0, dp(4), 0, dp(4)) }
        val done = steps.filter { it.optString("status") == "completed" }
        val canFold = done.isNotEmpty()
        val fold = canFold && !showDone
        var first = true
        fun divider() {
            if (!first) card.addView(View(activity).apply { setBackgroundColor(colour(R.color.navy)) },
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)).apply { marginStart = dp(48) })
            first = false
        }
        if (canFold) {
            divider()
            card.addView(LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                minimumHeight = dp(48); setPadding(dp(16), dp(10), dp(16), dp(10))
                addView(mark("completed"))
                addView(TextView(activity).apply { text = "${done.size} completed"; textSize = 15f; setTextColor(colour(R.color.muted)) },
                    LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { marginStart = dp(12) })
                addView(TextView(activity).apply {
                    text = if (fold) "Show" else "Hide"; textSize = 14f; setTextColor(colour(R.color.cyan))
                })
                setOnClickListener { showDone = fold; fill(list, plan) }
            })
        }
        for (step in steps) {
            val status = step.optString("status")
            if (fold && status == "completed") continue
            divider()
            val going = status == "in_progress"
            val row = LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.TOP
                minimumHeight = dp(48); setPadding(dp(16), dp(12), dp(16), dp(12))
                if (going) background = rounded(colour(R.color.surface_high), 0)
            }
            row.addView(mark(status))
            row.addView(LinearLayout(activity).apply {
                orientation = LinearLayout.VERTICAL
                addView(TextView(activity).apply {
                    text = step.optString("content"); textSize = 15f; setLineSpacing(0f, 1.25f)
                    setTextColor(colour(if (status == "completed") R.color.muted else R.color.text))
                    if (going) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                })
                if (going) addView(TextView(activity).apply {
                    text = "IN PROGRESS"; textSize = 11f; setTextColor(colour(R.color.cyan)); letterSpacing = 0.08f
                    setPadding(0, dp(4), 0, 0)
                })
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { marginStart = dp(12) })
            card.addView(if (going) FrameLayout(activity).apply {  // a cyan edge down its left side
                addView(row)
                addView(View(activity).apply { setBackgroundColor(colour(R.color.cyan)) },
                    FrameLayout.LayoutParams(dp(3), ViewGroup.LayoutParams.MATCH_PARENT))
            } else row)
        }
        return card
    }

    private fun html(words: String) = Html.fromHtml(words, Html.FROM_HTML_MODE_COMPACT)

    private fun block(list: LinearLayout, b: JSONObject, index: Int) {
        when (b.optString("t")) {
            "h" -> {
                val level = b.optInt("level", 2)
                if (level <= 2 && index > 0) list.addView(View(activity).apply { setBackgroundColor(colour(R.color.surface_high)) },
                    LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)).apply { topMargin = dp(24) })
                list.addView(TextView(activity).apply {
                    text = html(b.optString("html"))
                    if (level <= 2) { textSize = 19f; setTextColor(colour(R.color.text)); typeface = Typeface.DEFAULT_BOLD }
                    else { textSize = 16f; setTextColor(colour(R.color.cyan)); typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL) }
                }, wide(if (level <= 2) 16 else 20, if (level <= 2) 8 else 6))
            }
            "p" -> list.addView(TextView(activity).apply {
                text = html(b.optString("html")); textSize = 15f; setTextColor(reading); setLineSpacing(0f, 1.45f)
                setLinkTextColor(colour(R.color.cyan)); setTextIsSelectable(true)
            }, wide(0, 12))
            "list" -> {
                val items = b.optJSONArray("items") ?: return
                val box = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
                for (i in 0 until items.length()) {
                    val item = items.optJSONObject(i) ?: continue
                    val check = if (item.isNull("check")) null else item.optString("check")
                    box.addView(LinearLayout(activity).apply {
                        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.TOP
                        setPadding(dp(18 * item.optInt("depth")), 0, 0, 0)
                        addView(if (check != null) mark(when (check) { "x" -> "completed"; "~" -> "in_progress"; else -> "pending" }, 14).apply {
                            (layoutParams as LinearLayout.LayoutParams).topMargin = dp(4) }
                            else TextView(activity).apply {
                                text = if (b.optBoolean("ordered")) "${i + 1}." else "•"
                                textSize = 15f; setTextColor(colour(R.color.muted))
                            })
                        addView(TextView(activity).apply {
                            text = html(item.optString("html")); textSize = 15f; setTextColor(reading); setLineSpacing(0f, 1.35f)
                            setLinkTextColor(colour(R.color.cyan)); setTextIsSelectable(true)
                        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { marginStart = dp(8) })
                    }, wide(0, 6))
                }
                list.addView(box, wide(0, 8))
            }
            "code" -> list.addView(HorizontalScrollView(activity).apply {
                background = rounded(colour(R.color.surface_high), 12)
                isHorizontalScrollBarEnabled = false
                addView(TextView(activity).apply {
                    text = b.optString("text"); typeface = Typeface.MONOSPACE; textSize = 12.5f
                    setTextColor(colour(R.color.text)); setLineSpacing(0f, 1.35f); setTextIsSelectable(true)
                    setPadding(dp(14), dp(12), dp(14), dp(12))
                })
            }, wide(4, 12))
            "diagram" -> list.addView(card().apply {
                background = rounded(colour(R.color.surface), 12, 1, colour(R.color.surface_high))
                setPadding(dp(14), dp(14), dp(14), dp(14))
                gravity = Gravity.CENTER_HORIZONTAL
                addView(TextView(activity).apply {
                    text = "Diagram"; textSize = 12f; setTextColor(colour(R.color.cyan))
                    letterSpacing = 0.08f
                })
                val nodes = b.optJSONArray("nodes")
                for (n in 0 until (nodes?.length() ?: 0)) {
                    if (n > 0) addView(TextView(activity).apply {
                        text = "↓"; textSize = 16f; gravity = Gravity.CENTER; setTextColor(colour(R.color.cyan))
                    }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(4) })
                    addView(TextView(activity).apply {
                        text = nodes!!.optString(n); textSize = 14f; gravity = Gravity.CENTER
                        setTextColor(colour(R.color.text)); setPadding(dp(12), dp(10), dp(12), dp(10))
                        background = rounded(colour(R.color.surface_high), 12)
                    }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(4) })
                }
            }, wide(4, 12))
            "table" -> {
                val head = b.optJSONArray("head") ?: JSONArray()
                val rows = b.optJSONArray("rows") ?: return
                for (r in 0 until rows.length()) {
                    val cells = rows.optJSONArray(r) ?: continue
                    list.addView(card().apply {
                        background = rounded(colour(R.color.surface), 12)
                        setPadding(dp(12), dp(12), dp(12), dp(12))
                        for (c in 0 until cells.length()) {
                            if (c == 0) addView(TextView(activity).apply {
                                text = html(cells.optString(0)); textSize = 14f; setTextColor(colour(R.color.text)); typeface = Typeface.DEFAULT_BOLD
                            }) else addView(LinearLayout(activity).apply {
                                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.TOP
                                addView(draw.label(Html.fromHtml(head.optString(c), Html.FROM_HTML_MODE_COMPACT).toString().uppercase()).apply {
                                    setPadding(0, dp(2), 0, 0) }, LinearLayout.LayoutParams(dp(72), ViewGroup.LayoutParams.WRAP_CONTENT))
                                addView(TextView(activity).apply {
                                    text = html(cells.optString(c)); textSize = 13f; setTextColor(colour(R.color.muted)); setTextIsSelectable(true)
                                }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                            }, wide(6))
                        }
                    }, wide(0, 8))
                }
            }
            "rule" -> list.addView(View(activity).apply { setBackgroundColor(colour(R.color.surface_high)) },
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)).apply { topMargin = dp(16); bottomMargin = dp(16) })
        }
    }
}
