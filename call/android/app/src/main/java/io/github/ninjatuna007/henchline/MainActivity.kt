package io.github.ninjatuna007.henchline

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Paint
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * The call screen: the same messages to and from the call server as the call page. Your crew and the settings
 * live in a drawer from the left; each agent is a techno henchman of its own flavour (avatars.py). The call
 * itself runs in CallService; this screen watches it, and can be closed while the call goes on.
 */
class MainActivity : Activity() {

    private lateinit var listScreen: View
    private lateinit var chatScreen: View
    private lateinit var settingsScreen: View
    private lateinit var refresh: SwipeRefreshLayout
    private lateinit var quoteBar: TextView
    private lateinit var search: EditText
    private var quote = ""  // the message being replied to (swiped right), sent along with the answer
    private lateinit var callButton: ImageButton
    private lateinit var speakerButton: ImageButton
    private lateinit var talk: Button
    private lateinit var who: TextView
    private lateinit var avatar: ImageView
    private lateinit var state: TextView
    private lateinit var chat: LinearLayout
    private lateinit var chatScroll: ScrollView
    private lateinit var text: EditText
    private lateinit var send: ImageButton
    private lateinit var attach: ImageButton
    private lateinit var crew: LinearLayout
    private lateinit var clear: Switch

    private val main = Handler(Looper.getMainLooper())
    private var buttonOnly = false  // a noisy place: only holding the button gets in
    private var stayOn = true  // a call stays up while the agent works; check-on-me is for a wake-up or when an answer is needed
    private var cutIn: Boolean? = null  // null: Cursor cuts in, Claude and Codex let it wait
    private var current = ""  // the chat shown: texts and voice messages go to its agent
    // The call and the chats are apart, as a messaging app's (the user's wish, 2026-11-16): the call is with callSession;
    // any other chat can be read and texted meanwhile, and a bar leads back to the call
    private var callSession = ""
    private var callFrom = ""  // the chat a call was started from: the call goes to its agent
    private var switchedAt = 0L  // when you moved the call yourself: the laptop's older word doesn't pull it back
    private var followRing = false  // a ring answered: the screen goes to the ringer's chat once the laptop says who

    /** The chat shown is the call's: the talk button, hang-up and the call's state belong to it. */
    private fun onCall() = CallService.Bus.connected && (callSession.isEmpty() || current == callSession)
    private var names = mapOf<String, String>()  // session: the name the server gives it
    private var typed = 0  // ids for typed lines, so a "not sent" note strikes the right one
    private var talking = false
    private var pendingReply: String? = null  // an answered ring's report-back address, kept across set-up steps

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.main)
        listScreen = findViewById(R.id.listScreen)
        chatScreen = findViewById(R.id.chatScreen)
        settingsScreen = findViewById(R.id.settingsScreen)
        refresh = findViewById(R.id.refresh)
        quoteBar = findViewById(R.id.quote)
        search = findViewById(R.id.search)
        search.addTextChangedListener(object : android.text.TextWatcher {
            // After a short pause in typing, not on every key (2026-11-16 review)
            override fun afterTextChanged(e: android.text.Editable?) {
                main.removeCallbacks(searchSoon)
                main.postDelayed(searchSoon, 250)
            }
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
        })
        findViewById<TextView>(R.id.sort).apply {
            fun label() = mapOf("recent" to "Recent", "project" to "By project", "agent" to "By agent")[sortMode()]
            text = label()
            setOnClickListener {
                val next = mapOf("recent" to "project", "project" to "agent", "agent" to "recent")[sortMode()]
                prefs().edit().putString("sort", next).apply()
                text = label()
                showCrew()
            }
        }
        findViewById<SwipeRefreshLayout>(R.id.refresh).apply {
            setColorSchemeColors(getColor(R.color.cyan))
            setProgressBackgroundColorSchemeColor(getColor(R.color.surface))
        }
        findViewById<SwipeRefreshLayout>(R.id.planRefresh).apply {
            setColorSchemeColors(getColor(R.color.cyan))
            setProgressBackgroundColorSchemeColor(getColor(R.color.surface))
            setOnRefreshListener { refreshPlan() }
        }
        refresh.setOnRefreshListener { if (tab == "usage") showUsage(fresh = true) else findCrew() }
        quoteBar.setOnClickListener { setQuote("") }  // tap it to stop replying
        findViewById<View>(R.id.settingsButton).visibility = View.GONE  // the header keeps only the title and you
        findViewById<View>(R.id.profile).setOnClickListener { toggleAway() }
        findViewById<View>(R.id.profile).setOnLongClickListener {
            it.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS); youSheet(); true }
        tidyHeaderAndSettings()
        showAway(prefs().getBoolean("away", false), animate = false)
        findViewById<View>(R.id.settingsBack).setOnClickListener { show(listScreen) }
        findViewById<TextView>(R.id.myName).apply {
            text = prefs().getString("my_name", "Boss")
            setOnClickListener { askName(this) }
        }
        callButton = findViewById(R.id.call)
        speakerButton = findViewById(R.id.speaker)
        speakerButton.setOnClickListener {  // loudspeaker ⇄ earpiece, mid-call; with earbuds, a list of the three
            val service = CallService.Bus.service ?: return@setOnClickListener
            // The list on Android 12 and later, where the call can be put on the earbuds by name (on 10 and 11 picking
            // them couldn't bring up their call channel)
            if (android.os.Build.VERSION.SDK_INT >= 31 && service.headsetAvailable()) {
                panel = RoutePicker(this, service.where) { to -> route(to) }.also { it.show() }
                return@setOnClickListener
            }
            val on = !service.speaker
            val wasClear = service.clear
            if (!service.setSpeaker(on)) return@setOnClickListener  // still switching from the last tap
            prefs().edit().putBoolean("speaker", on).apply()
            afterMove(service, wasClear)
            showSpeaker(on)
        }
        findViewById<View>(R.id.callCamera).setOnClickListener {  // a photo for the agent, on a call: it goes with the next turn
            if (CallService.Bus.connected && photo == null) takePhoto(snap = true)  // not twice: one camera at a time
        }
        talk = findViewById(R.id.talk)
        who = findViewById(R.id.who)
        avatar = findViewById(R.id.avatar)
        state = findViewById(R.id.state)
        chat = findViewById(R.id.chat)
        chatScroll = findViewById(R.id.chatScroll)
        text = findViewById(R.id.text)
        send = findViewById(R.id.send)
        attach = findViewById(R.id.attach)
        crew = findViewById(R.id.crew)
        clear = findViewById(R.id.clear)
        showMode("waiting")
        current = prefs().getString("current", "") ?: ""
        names = try {  // the crew as last seen on a call: each agent's chat can be read and texted off a call
            val list = org.json.JSONArray(prefs().getString("crew", "[]"))
            (0 until list.length()).associate { list.getJSONObject(it).getString("session") to list.getJSONObject(it).getString("name") }
        } catch (_: Exception) { emptyMap() }
        showCrew()

        findViewById<ImageButton>(R.id.menu).setOnClickListener { show(listScreen) }
        findViewById<View>(R.id.compose).setOnClickListener { newChat() }
        findViewById<Switch>(R.id.danceSwitch).apply {
            isChecked = prefs().getBoolean("dance", true)
            setOnCheckedChangeListener { _, on -> prefs().edit().putBoolean("dance", on).apply(); workingKind = "" }
        }
        findViewById<Switch>(R.id.thoughtsSwitch).apply {
            isChecked = prefs().getBoolean("thoughts", true)
            setOnCheckedChangeListener { _, on -> prefs().edit().putBoolean("thoughts", on).apply() }
        }
        findViewById<View>(R.id.callBar).apply {
            // In the call's chat a tap opens the call's menu; anywhere else it goes back to the call first
            setOnClickListener { if (chatScreen.visibility == View.VISIBLE && current == callSession) callMenu() else openCall() }
            setOnLongClickListener { callMenu(); true }
        }
        findViewById<View>(R.id.callBarHang).setOnClickListener { hangUp() }
        getSystemService(android.media.AudioManager::class.java).registerAudioDeviceCallback(devicesWatch, main)
        // The window's edges by hand: the status bar's height to a strip of our own (green on a call), the sides and
        // the bottom (gesture bar, keyboard) as padding, so only the top turns green
        findViewById<View>(R.id.root).setOnApplyWindowInsetsListener { root, insets ->
            val (left, top, right, bottom) = if (android.os.Build.VERSION.SDK_INT >= 30) insets.getInsets(
                android.view.WindowInsets.Type.systemBars() or android.view.WindowInsets.Type.ime()).let { listOf(it.left, it.top, it.right, it.bottom) }
            else @Suppress("DEPRECATION") listOf(insets.systemWindowInsetLeft, insets.systemWindowInsetTop,
                insets.systemWindowInsetRight, insets.systemWindowInsetBottom)  // Android 10
            findViewById<View>(R.id.statusSpace).layoutParams.height = top
            findViewById<View>(R.id.statusSpace).requestLayout()
            root.setPadding(left, 0, right, bottom)
            android.view.WindowInsets.CONSUMED
        }
        findViewById<View>(R.id.callPulse).background =  // the "live" ring behind the avatar (pulsed by showCallBar)
            GradientDrawable().apply { shape = GradientDrawable.OVAL; setStroke(dp(2), getColor(R.color.call_live)) }
        findViewById<ImageView>(R.id.callAvatar).background =
            GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(0x0FFFFFFF); setStroke(dp(2), 0x66FFFFFF) }
        findViewById<View>(R.id.modelChip).setOnClickListener { switchModel() }
        findViewById<View>(R.id.planChip).setOnClickListener { togglePlan() }
        findViewById<View>(R.id.tabChats).setOnClickListener { showTab("chats") }
        findViewById<View>(R.id.tabCalls).setOnClickListener { showTab("calls") }
        findViewById<View>(R.id.tabUsage).setOnClickListener { showTab("usage") }
        callButton.setOnClickListener {
            when {
                onCall() -> hangUp()
                CallService.Bus.connected -> openCall()  // one call at a time: back to it
                else -> call()
            }
        }
        val pickForCall = View.OnClickListener { if (onCall()) switchCall() }
        who.setOnClickListener(pickForCall)
        avatar.setOnClickListener(pickForCall)
        findViewById<View>(R.id.serverRow).setOnClickListener { askServer { } }
        showClear(prefs().getBoolean("clear", true))
        setUpTalkButton()
        send.setOnClickListener { sendTyped() }
        setUpVoiceNotes()
        setUpCommands()
        attach.setOnClickListener {
            if (locked) endVoiceNote(keep = false, why = "Dropped")
            else panel = AddSheet(this) { what ->
                when (what) { "Camera" -> takePhoto(); "Photos" -> pickPhotos(); else -> pickFiles() }
            }.also { it.show() }
        }
        findViewById<View>(R.id.stop).setOnClickListener { stopAgent() }
        text.setOnEditorActionListener { _, action, _ -> (action == EditorInfo.IME_ACTION_SEND).also { if (it) sendTyped() } }
        requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS,
            Manifest.permission.BLUETOOTH_CONNECT), 1)
        showHistory()
        if (CallService.Bus.connected) openCall() else show(listScreen)  // home: the chat list
        RingService.register(this)  // so agents can ring this phone
        askFullScreen()
        DoNotDisturb.ask(this)
        answer(intent)
    }

    override fun onRequestPermissionsResult(code: Int, permissions: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, permissions, results)
        if (code != MIC_FOR_CALL) return
        val reply = pendingReply.also { pendingReply = null }
        if (results.firstOrNull() == PackageManager.PERMISSION_GRANTED) call(reply)
        else {
            Reply.send(reply, "unreachable")  // answered, but no microphone: the call can't be made
            say("", "The call needs the microphone: allow it, then call again.", note = true)
        }
    }

    /** The paperclip: pictures or any files, for the agent whose chat is showing (the picker offers Photos too). */
    private fun pickFiles() {
        startActivityForResult(Intent(Intent.ACTION_GET_CONTENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE)
            .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true), PICK_FILES)
    }

    /** Photos: the phone's photo picker (no permission needed), several at once. */
    private fun pickPhotos() {
        val intent = if (android.os.Build.VERSION.SDK_INT >= 33)
            Intent(android.provider.MediaStore.ACTION_PICK_IMAGES).putExtra(android.provider.MediaStore.EXTRA_PICK_IMAGES_MAX, 20)
        else Intent(Intent.ACTION_GET_CONTENT).setType("image/*").putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        startActivityForResult(intent, PICK_FILES)
    }

    private var photo: android.net.Uri? = null  // where the camera puts the picture it takes
    private var snapping = false  // that picture is the call bar's camera button's: it goes with the next turn, no sheet

    /** Camera: a picture taken now, into the phone's Pictures/Henchline, then shared like any picked file. */
    private fun takePhoto(snap: Boolean = false) {
        val values = android.content.ContentValues().apply {
            put(android.provider.MediaStore.Images.Media.DISPLAY_NAME, "henchline-${System.currentTimeMillis()}.jpg")
            put(android.provider.MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
            put(android.provider.MediaStore.Images.Media.RELATIVE_PATH, "Pictures/Henchline")
        }
        photo = contentResolver.insert(android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values) ?: return
        snapping = snap
        try {
            startActivityForResult(Intent(android.provider.MediaStore.ACTION_IMAGE_CAPTURE)
                .putExtra(android.provider.MediaStore.EXTRA_OUTPUT, photo), TAKE_PHOTO)
        } catch (_: android.content.ActivityNotFoundException) {
            contentResolver.delete(photo!!, null, null); photo = null; snapping = false
            android.widget.Toast.makeText(this, "No camera app", android.widget.Toast.LENGTH_SHORT).show()
        }
    }

    @Deprecated("the platform Activity has no result API besides this")
    override fun onActivityResult(code: Int, result: Int, data: Intent?) {
        super.onActivityResult(code, result, data)
        if (code == TAKE_PHOTO) {
            val taken = photo.also { photo = null } ?: return
            val snap = snapping.also { snapping = false }
            if (result != RESULT_OK) contentResolver.delete(taken, null, null)
            else if (snap && CallService.Bus.connected) snapPhoto(taken)
            else attachUris(listOf(taken))
            return
        }
        if (code != PICK_FILES || result != RESULT_OK || data == null) return
        val uris = data.clipData?.let { clip -> (0 until clip.itemCount).map { clip.getItemAt(it).uri } }
            ?: listOfNotNull(data.data)
        attachUris(uris)
    }

    /** The call bar's camera: the picture goes up and waits on the laptop for what is said next (or goes alone after
     *  a short while); it shows in the chat at once, and the call never stopped. */
    private fun snapPhoto(uri: android.net.Uri) {
        val name = Files.nameOf(this, uri)
        startForegroundService(UploadService.intent(this, listOf(uri), listOf(name), "", "", current)
            .putExtra(UploadService.INTO_CHAT, true).putExtra(UploadService.SNAP, true))
    }

    private fun attachUris(uris: List<android.net.Uri>) {
        if (uris.isEmpty()) return
        // The same sheet as sharing from another app (the user's wish, 2026-11-15): what it is, who gets it (this
        // chat's agent first), a caption; anything already typed in the box comes along as the caption
        val words = text.text.toString().trim()
        text.setText("")
        val crew = names.toList().ifEmpty { listOf(current to (prefs().getString("current_name", "") ?: "")) }
        sheet?.close()  // one at a time
        sheet = Sheet(this, uris, uris.map { Files.nameOf(this, it) }, "", crew, current, over = true, note0 = words,
            onSent = { to ->  // off a call, the chat follows where it went (on one, the laptop says who's current)
                if (!CallService.Bus.connected && to != current) {
                    current = to
                    prefs().edit().putString("current", current).putString("current_name", names[current] ?: "").apply()
                    showHistory(); showCrew()
                }
            }, onClosed = { showHistory() }).also { it.show() }  // closed: what came meanwhile is read now
    }

    /** What you attached, on your side of the chat: a picture small, anything else its name. */
    /** A picture you sent, from its small copy: shown on your side of the chat, its name under it. */
    private fun picture(thumb: String, caption: String, session: String, at: Long = 0) {
        val bubble = say("you", caption, mine = true, session = session, keep = false, at = at)
        val width = dp(220)
        val cached = pictures.get(thumb)
        if (cached != null) {
            bubble.setCompoundDrawablesWithIntrinsicBounds(null, android.graphics.drawable.BitmapDrawable(resources, cached), null, null)
            bubble.compoundDrawablePadding = dp(8)
            return
        }
        decoder.execute {  // decoded small, two at a time, off the main thread: a chat may hold many pictures
            val shown = try {
                val bounds = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
                android.graphics.BitmapFactory.decodeFile(thumb, bounds)
                var sample = 1
                while (bounds.outWidth / (sample * 2) >= width) sample *= 2
                val bitmap = android.graphics.BitmapFactory.decodeFile(thumb,
                    android.graphics.BitmapFactory.Options().apply { inSampleSize = sample })
                bitmap?.let {
                    android.graphics.Bitmap.createScaledBitmap(it, width, (it.height * width / it.width.coerceAtLeast(1)).coerceAtLeast(1), true)
                }
            } catch (_: Exception) {
                null
            } ?: return@execute
            pictures.put(thumb, shown)
            main.post {
                bubble.setCompoundDrawablesWithIntrinsicBounds(null, android.graphics.drawable.BitmapDrawable(resources, shown), null, null)
                bubble.compoundDrawablePadding = dp(8)
            }
        }
    }

    private var sheet: Sheet? = null  // the paperclip's share sheet, while it is up

    /** A swipe in from the left edge of a chat goes back to the list, as the old drawer's swipe did. */
    private var edgeStart = -1f
    private var tabSwipeX = -1f
    private var tabSwipeY = 0f
    private val tabs = listOf("usage", "chats", "calls")  // as the bottom bar shows them: swiped between

    private var selectDownX = 0f
    private var selectDownY = 0f

    override fun dispatchTouchEvent(event: MotionEvent): Boolean {
        // Selecting: a tap on a message picks it (or unpicks it), instead of opening it
        if (selected != null && chatScreen.visibility == View.VISIBLE && BottomPanel.top() == null) {
            val bar = selectBar
            val onBar = bar != null && android.graphics.Rect().also { bar.getGlobalVisibleRect(it) }.contains(event.rawX.toInt(), event.rawY.toInt())
            if (!onBar) when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> { selectDownX = event.rawX; selectDownY = event.rawY }
                // A quick tap only: a hold has already picked it through its long-press (twice would undo it)
                MotionEvent.ACTION_UP -> if (kotlin.math.abs(event.rawX - selectDownX) < dp(12) && kotlin.math.abs(event.rawY - selectDownY) < dp(12)
                    && event.eventTime - event.downTime < android.view.ViewConfiguration.getLongPressTimeout()) {
                    val hit = android.graphics.Rect()
                    for (k in 0 until chat.childCount) {
                        val row = chat.getChildAt(k)
                        @Suppress("UNCHECKED_CAST") val ats = row.getTag(R.id.ats) as? List<Long> ?: continue
                        if (row.visibility == View.VISIBLE && row.getGlobalVisibleRect(hit) && hit.contains(event.rawX.toInt(), event.rawY.toInt())) {
                            toggleSelected(ats)
                            break
                        }
                    }
                    val cancel = MotionEvent.obtain(event).apply { action = MotionEvent.ACTION_CANCEL }
                    return super.dispatchTouchEvent(cancel).also { cancel.recycle() }
                }
            }
        }
        // On the list screen, a sideways swipe moves to the tab beside (the user's wish, 2026-11-17)
        if (listScreen.visibility == View.VISIBLE && BottomPanel.top() == null && sheet?.isOpen != true) when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> { tabSwipeX = event.x; tabSwipeY = event.y }
            MotionEvent.ACTION_UP -> {
                val dx = event.x - tabSwipeX
                val dy = event.y - tabSwipeY
                if (tabSwipeX >= 0 && kotlin.math.abs(dx) > dp(90) && kotlin.math.abs(dx) > 2 * kotlin.math.abs(dy)) {
                    tabSwipeX = -1f
                    val next = tabs.indexOf(tab) + if (dx < 0) 1 else -1
                    if (next in tabs.indices) {
                        showTab(tabs[next])
                        // Not a tap on whatever row the finger ended on
                        val cancel = MotionEvent.obtain(event).apply { action = MotionEvent.ACTION_CANCEL }
                        return super.dispatchTouchEvent(cancel).also { cancel.recycle() }
                    }
                }
                tabSwipeX = -1f
            }
        }
        if (chatScreen.visibility == View.VISIBLE) when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> edgeStart = if (event.x < dp(24)) event.x else -1f
            MotionEvent.ACTION_UP -> {
                if (edgeStart >= 0 && event.x - edgeStart > dp(100)) {
                    edgeStart = -1f
                    // The top menu first, then the one under it, and only then the chat (a second menu used to
                    // stay drawn while this went back to the list, 2026-11-19)
                    when {
                        sheet?.isOpen == true -> sheet?.close()
                        BottomPanel.top() != null -> BottomPanel.top()?.close()
                        else -> show(listScreen)
                    }
                    return true
                }
                edgeStart = -1f
            }
        }
        return super.dispatchTouchEvent(event)
    }

    @Deprecated("the platform Activity's back handling, kept for Android 10-12")
    override fun onBackPressed() {
        when {  // the top menu, then the one under it, then a chat or settings; only the list leaves the app
            sheet?.isOpen == true -> sheet?.close()
            BottomPanel.top() != null -> BottomPanel.top()?.close()
            selected != null -> stopSelecting()
            listScreen.visibility != View.VISIBLE -> show(listScreen)
            else -> super.onBackPressed()
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        answer(intent)
    }

    /** Opened from henchline://call (an agent rang): ask, as a phone does. Any app or web page can open the
     *  link, so it never turns the mic on by itself: only Answer connects. */
    private fun answer(intent: Intent?) {
        // The Quick Settings tile. Any app can open this screen, so only the tile's own key turns the mic on
        if (intent?.action == CALL_NOW) {
            intent.action = null
            if (intent.getStringExtra(CALL_KEY) == callKey(this)) call()
            return
        }
        if (intent?.action == OPEN_FILE) {
            openFromNotification(intent)
            return
        }
        if (intent?.action == ANSWER) {  // Answer on IncomingActivity or the ring's banner: the user's own tap
            intent.action = null
            Ringing.end(this)  // stop ringing, everywhere
            call(intent.getStringExtra(IncomingActivity.REPLY))
            return
        }
        if (intent?.data?.scheme == "henchline" && intent.data?.host == "call" && CallService.Bus.service == null) {
            intent.data = null  // once: rotating the screen must not ask again
            AlertDialog.Builder(this)
                .setIcon(R.mipmap.ic_launcher)
                .setTitle("Your crew is calling")
                .setMessage("An agent wants to talk.")
                .setPositiveButton("Answer") { _, _ -> say("", "Answering…", note = true); call() }
                .setNegativeButton("Decline", null)
                .show()
        }
    }

    private var lifecycleSeen = false  // the app on screen (between onResume and onPause)

    override fun onResume() {
        super.onResume()
        lifecycleSeen = true
        CallService.Bus.listener = { message -> main.post { fromServer(message) } }
        RingService.share(this)  // where texts reach this app: the laptop kept an old address after a reinstall
        main.removeCallbacks(textsPoll); main.post(textsPoll)
        if (Outbox.all(this).isNotEmpty()) {  // messages that waited: now, maybe, and on any change of network
            Outbox.watch(this)
            Thread { Outbox.flush(this) }.start()
        }
        showConnected(onCall())
        if (CallService.Bus.connected) {  // what was said while this screen wasn't looking
            CallService.Bus.lastAgents?.let { fromServer(it) }
            CallService.Bus.lastMode?.let { fromServer(it) }
        }
        if (chatScreen.visibility == View.VISIBLE) {
            Messages.viewing = current
            showModel(current)  // the model may have been changed on the laptop meanwhile (in Cursor itself)
            main.removeCallbacks(thoughtsPoll); main.post(thoughtsPoll)  // the laptop asked again, as when it left
            main.removeCallbacks(workingPoll); main.post(workingPoll)
            main.removeCallbacks(planPoll); main.post(planPoll)
        }
        showHistory()  // what came meanwhile (a call's lines, a text): from the kept chat, on a call or off it
        showServer()
        showRinging()
        showCallBar()  // and its live ring beats again
    }

    /** Opened while an agent's ring still sounds: say so, with Answer and Decline. */
    private fun showRinging() {
        val ring = Ringing.now(this) ?: return
        if (CallService.Bus.service != null || ringingShown?.isShowing == true) return
        val reply = ring.optString("reply").ifEmpty { null }
        ringingShown = AlertDialog.Builder(this)
            .setIcon(avatarOf(ring.optString("agent")))
            .setTitle("${IncomingActivity.nameOf(ring.optString("agent"))} is calling")
            .setMessage(ring.optString("why").ifEmpty { "Your crew wants to talk." })
            .setPositiveButton("Answer") { _, _ -> call(reply) }
            .setNegativeButton("Decline") { _, _ -> Reply.send(reply, "declined"); Calls.outcome(this, reply, "declined"); Ringing.end(this) }
            .show()
    }

    private var ringingShown: AlertDialog? = null

    private fun showServer() {
        findViewById<TextView>(R.id.server).text =
            prefs().getString("server", "")?.removePrefix("https://")?.trimEnd('/')?.ifEmpty { null }
                ?: "Set the laptop's address"
    }

    /** While the app is open, texts that never came as a push are fetched every 15 s (Messages.catchUp). */
    private val textsPoll = object : Runnable {
        override fun run() {
            Thread { Messages.catchUp(this@MainActivity) }.start()
            if (lifecycleSeen) main.postDelayed(this, 15_000)
        }
    }

    override fun onPause() {
        Messages.viewing = ""  // not on screen: texts get their banner again
        lifecycleSeen = false
        pulseLive(false)  // no beat for nobody
        (panel as? CallMenu)?.close(animate = false)  // nor henchmen hopping unseen, or a menu left under the lock
        main.removeCallbacks(thoughtsPoll)  // nobody looking: the laptop isn't asked every few seconds
        main.removeCallbacks(workingPoll)
        main.removeCallbacks(planPoll)
        stopWorkingBubble()  // nor anything left dancing: the next ask brings it back
        CallService.Bus.listener = null  // the call goes on; this screen just stops drawing it
        // Leaving mid-recording: the mic is never left open with nothing on screen. Held, it is dropped (the
        // finger left with the app); locked, it was meant to be sent
        if (voiceNote != null) endVoiceNote(keep = locked, why = "Dropped: you left the app")
        super.onPause()
    }

    override fun onDestroy() {
        getSystemService(android.media.AudioManager::class.java).unregisterAudioDeviceCallback(devicesWatch)
        main.removeCallbacks(recordClock)
        lockHint?.dismiss(); lockHint = null
        super.onDestroy()
    }

    private fun call(given: String? = null) {
        // A call made by hand while a ring sounds answers that ring: the laptop routes it to the agent that rang
        val reply = given ?: Ringing.now(this)?.optString("reply")?.ifEmpty { null }
        Ringing.end(this)
        if (CallService.Bus.service != null) {  // already connecting: one call at a time
            state.text = "Connecting…"
            return
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingReply = reply ?: pendingReply  // the call is made when the microphone is allowed
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), MIC_FOR_CALL)
            return
        }
        val server = prefs().getString("server", "") ?: ""
        if (server.isEmpty()) {
            askServer { call(reply) }
            return
        }
        state.text = "Connecting…"
        // A call you start goes to the agent of the chat it was started from; a ring answered (reply) goes to the
        // agent that rang, which the laptop routes by the ring itself: the chat open then must not pull it away (a
        // ring from one Claude went to another whose chat was on screen, 2026-11-16). The phone follows to its chat.
        callFrom = if (reply == null) current else ""
        followRing = reply != null
        callSession = current
        show(chatScreen)
        startForegroundService(Intent(this, CallService::class.java).putExtra(CallService.SERVER, server)
            .putExtra(CallService.CLEAR, prefs().getBoolean("clear", true))
            // A ring answered (reply set) starts on the loudspeaker: the phone may be on the desk, and a ring-back
            // missed at the ear would be hung up on as quiet. A call you place follows your last choice.
            .putExtra(CallService.SPEAKER, reply != null || prefs().getBoolean("speaker", true))
            .putExtra(IncomingActivity.REPLY, reply))
    }

    private fun hangUp() {
        startService(Intent(this, CallService::class.java).setAction(CallService.STOP))
    }

    /** A ring shows over the lock screen only with this permission, which Android 14+ asks the user for, once. */
    private fun askFullScreen() {
        val manager = getSystemService(android.app.NotificationManager::class.java)
        if (android.os.Build.VERSION.SDK_INT < 34 || manager.canUseFullScreenIntent() || prefs().getBoolean("asked_full", false)) return
        prefs().edit().putBoolean("asked_full", true).apply()
        AlertDialog.Builder(this)
            .setIcon(R.mipmap.ic_launcher)
            .setTitle("Let your crew ring you")
            .setMessage("Allow Henchline to show calls over the lock screen, as a phone call does.")
            .setPositiveButton("Allow") { _, _ ->
                startActivity(Intent(android.provider.Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                    android.net.Uri.parse("package:$packageName")))
            }
            .setNegativeButton("Not now", null)
            .show()
    }

    /** The call page's address: https://<laptop's Tailscale name>/ (the laptop's `tailscale serve`). */
    private fun askServer(then: () -> Unit) {
        val box = EditText(this).apply {
            setText(prefs().getString("server", "https://"))
            hint = "https://laptop.your-tailnet.ts.net/"
        }
        AlertDialog.Builder(this)
            .setTitle("The laptop's address")
            .setView(LinearLayout(this).apply { setPadding(48, 16, 48, 0); addView(box) })
            .setPositiveButton("Save") { _, _ ->
                prefs().edit().putString("server", box.text.toString().trim()).apply()
                showServer()
                then()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun prefs() = getSharedPreferences("call", MODE_PRIVATE)

    private fun fromServer(message: String) {
        val status = JSONObject(message)
        when {
            status.has("opened") -> {
                // Started from a chat: the call is with that chat's agent
                if (callFrom.isNotEmpty()) CallService.Bus.service?.send(JSONObject().put("switch", callFrom).toString())
                callSession = callFrom
                callFrom = ""
                showConnected(onCall())
                showCallBar()
                if (buttonOnly) CallService.Bus.service?.send(JSONObject().put("button_only", true).toString())
                stayOn = stayDefault()
                CallService.Bus.service?.send(JSONObject().put("stay_on", stayOn).toString())
                sendCutDefault()
            }
            status.has("reconnecting") -> {  // the connection dropped mid-call: the bar says so until it's back
                callWords = if (status.getBoolean("reconnecting")) "reconnecting…" else ""
                // Back: the laptop starts each connection on an open line, so hold-to-talk is said again
                if (!status.getBoolean("reconnecting") && buttonOnly)
                    CallService.Bus.service?.send(JSONObject().put("button_only", true).toString())
                if (!status.getBoolean("reconnecting") && stayOn)
                    CallService.Bus.service?.send(JSONObject().put("stay_on", true).toString())
                findViewById<TextView>(R.id.callStatus).text = if (callWords.isNotEmpty()) " · $callWords" else ""
                if (onCall() && callWords.isNotEmpty()) state.text = subtitle("Reconnecting…")
            }
            status.has("closed") -> {
                (panel as? CallMenu)?.close()  // the call's menu goes with the call
                callSession = ""
                callWords = ""
                showConnected(false)
                showCallBar()
                say("", status.getString("closed"), note = true)
            }
            status.has("chat") -> {
                val line = status.getJSONObject("chat")
                // On the chat list, an arriving line shows there at once: bold, with its unread count (a text
                // reached the banner but not the list, 2026-11-16)
                if (listScreen.visibility == View.VISIBLE) main.post { showCrew() }
                val from = line.getString("from")
                val file = line.optJSONObject("file")
                if (from != "you" && line.optString("session") == current && file == null) {
                    // Added at the end, your ticks before it turned blue in place: no redraw, so the chat
                    // doesn't jump (a full redraw shifted it, 2026-11-16)
                    say(from, line.getString("text"), session = current, keep = false)
                    // Blue only for what it had taken in: one still waiting in its queue keeps its one tick
                    for ((bubble, words) in myBubbles) if (bubble !in waitingBubbles) bubble.text = stamp(bubble, words, "read")
                    myBubbles.removeAll { it.first !in waitingBubbles }
                    main.post { refreshStop() }  // it answered: nothing to stop
                    if (chatScreen.visibility == View.VISIBLE) {
                        if (line.has("tid")) Messages.seen(this, current)
                        markRead(current)
                    }
                    return
                }
                // The call has kept it already (CallService): drawn here only; a file's words are its caption
                if (file == null)
                    say(from, line.getString("text"), mine = from == "you", session = line.optString("session"), keep = false,
                        status = if (from == "you") "delivered" else "")
                if (file != null) {
                    val where = line.optString("session")
                    if (where == current && joinsAlbum(where)) showHistory()  // it joins the picture before it
                    else fileCard(from, file, where, keep = false, caption = line.optString("text"))
                }
                // An agent's text arriving in the chat you're looking at: read at once
                if (line.optString("session") == current && chatScreen.visibility == View.VISIBLE) {
                    if (line.has("tid")) Messages.seen(this, current)
                    markRead(current)
                }
            }
            status.has("agents") -> showAgents(status)
            status.has("delivered") -> tick(status.getString("delivered"), "delivered")  // in place: no jump
            status.has("reload") -> { showHistory(); if (listScreen.visibility == View.VISIBLE) showCrew() }  // a share or a file kept in a chat
            status.has("talking") ->  // the earbuds or the notification started or ended a turn
                android.widget.Toast.makeText(this, if (status.getBoolean("talking")) "Talking: tap again to send" else "Sent",
                    android.widget.Toast.LENGTH_SHORT).show()
            status.has("note") -> {
                val id = status.optString("unsent")
                chat.findViewWithTag<TextView>("typed-$id")?.apply {
                    alpha = 0.5f
                    paintFlags = paintFlags or Paint.STRIKE_THRU_TEXT_FLAG
                }
                say("", status.getString("note"), note = true)
            }
            status.has("mode") -> {
                if (onCall()) state.text = subtitle(status.optString("state"))
                callWords = status.optString("state").substringBefore(" · ").lowercase()
                findViewById<TextView>(R.id.callStatus).text = if (callWords.isNotEmpty()) " · $callWords" else ""
                showChipIn(status.optJSONObject("speaker"))
                showMode(status.getString("mode"))
            }
        }
    }

    /** The agents a call can switch to; the chat shows the current one's lines. */
    private fun showAgents(status: JSONObject) {
        followRenames(status)
        val list = status.getJSONArray("agents")
        names = (0 until list.length()).associate {
            list.getJSONObject(it).getString("session") to list.getJSONObject(it).getString("name")
        }
        prefs().edit().putString("crew", list.toString()).apply()  // to pick from, and text, off a call
        val now = status.optString("current")
        // Looking at the call (or answering a ring, once): the screen follows the switch
        val following = followRing || current.isEmpty() || current == callSession
        // A list from before your switch reached the laptop names the old agent: not followed for a few seconds
        val stale = now != callSession && System.currentTimeMillis() - switchedAt < 4000
        if (now.isNotEmpty() && !stale && (now != callSession || followRing)) {
            callSession = now
            followRing = false
            if (following && now != current) {
                current = now
                prefs().edit().putString("current", current).putString("current_name", names[current] ?: "").apply()
                showHistory()
            }
        }
        showConnected(onCall())
        showCallBar()
        showCrew()
    }

    /** The chat list (the home screen). Each chat is named by its project, its henchman's face and name saying
     *  which agent (the user's choice, 2026-11-16). Ordered newest first, or grouped by project or by agent; the
     *  search box matches project and agent names first, then what was said, showing the matching line. */
    private fun showCrew() {
        crew.removeAllViews()
        main.removeCallbacks(searchSoon)
        if (tab == "calls") return showCalls()
        if (tab == "usage") return showUsage()
        val query = if (::search.isInitialized) search.text.toString().trim() else ""
        if (names.isEmpty()) {
            crew.addView(TextView(this).apply {
                text = "No crew yet. Pull down to look for agents running on your computer, or call once."
                setTextColor(getColor(R.color.muted))
                setPadding(dp(20), dp(24), dp(20), dp(24))
            })
        }
        class Chat(val session: String, val agent: String, val project: String, val last: JSONObject?, val found: String)
        fun project(name: String) = name.substringAfter(", ", "").removePrefix("in ").ifEmpty { "home" }
        var chats = names.map { (session, name) -> Chat(session, name.substringBefore(","), project(name), Chats.last(this, session), "") }
        // Chats an agent texted lately that the laptop's crew list doesn't name: named from the session ("cursor-_tmp");
        // older ones stay out of the list (long-press clears one that's in it). Not hands-free's own: it is the
        // switchboard, not an agent (its "Now talking to …" notices from calls, 2026-11-16)
        chats = chats + Chats.sessions(this).filter { s -> s !in names && !s.startsWith("handsfree-") && (Chats.last(this, s)?.optLong("at") ?: 0) > System.currentTimeMillis() - 3 * 86_400_000L }.map { session ->
            val agent = session.substringBefore("-").replaceFirstChar { it.uppercase() }
            Chat(session, agent, "not on the crew list", Chats.last(this, session), "")  // its key can't say its folder
        }
        // One that texted since the crew was last asked: likely an agent just started from a new chat, under its own
        // key now (the user's catch, 2026-11-16: "not on the crew list" until the app was reopened): asked at once,
        // which also moves the new chat to it
        val strays = chats.filter { it.project == "not on the crew list" }
        if (strays.any { (it.last?.optLong("at") ?: 0L) > crewAsked }) { crewAsked = 0L; refreshCrewQuietly() }
        if (query.isNotEmpty()) {
            val q = query.lowercase()
            val byName = chats.filter { q in it.project.lowercase() || q in it.agent.lowercase() }
            if (byName.isEmpty()) {  // nothing by name: a deeper look, in what was said, off the main thread
                val done = searched?.takeIf { it.first == q }?.second
                if (done == null) {
                    val sessions = chats.map { it.session }
                    Thread {
                        val hits = sessions.mapNotNull { session ->
                            Chats.load(this, session).lastOrNull { q in it.optString("text").lowercase() }
                                ?.let { session to it.optString("text") }
                        }.toMap()
                        main.post {
                            searched = q to hits
                            if (search.text.toString().trim().lowercase() == q) showCrew()
                        }
                    }.start()
                    crew.addView(TextView(this).apply {
                        text = "Searching the chats…"; setTextColor(getColor(R.color.muted)); setPadding(dp(20), dp(24), dp(20), dp(24))
                    })
                    return
                }
                chats = chats.mapNotNull { c -> done[c.session]?.let { Chat(c.session, c.agent, c.project, c.last, it) } }
            } else chats = byName
            if (chats.isEmpty()) crew.addView(TextView(this).apply {
                text = "Nothing matches \"$query\"."; setTextColor(getColor(R.color.muted)); setPadding(dp(20), dp(24), dp(20), dp(24))
            })
        }
        val pins = pins()
        val byRecent = chats.sortedByDescending { it.last?.optLong("at") ?: 0 }
        val pinned = byRecent.filter { it.session in pins }
        val rest = byRecent.filter { it.session !in pins }
        val groups: List<Pair<String, List<Chat>>> = buildList {
            if (pinned.isNotEmpty()) add("Pinned" to pinned)
            addAll(when (sortMode()) {
                "project" -> rest.groupBy { it.project }.toList()
                "agent" -> rest.groupBy { it.agent }.toList()
                else -> if (rest.isEmpty()) emptyList() else listOf("" to rest)
            })
        }
        for ((heading, members) in groups) {
            if (heading.isNotEmpty()) crew.addView(TextView(this).apply {
                text = heading.replace("_", " ").uppercase()
                setTextColor(getColor(R.color.muted)); textSize = 12f; typeface = android.graphics.Typeface.DEFAULT_BOLD
                setPadding(dp(20), dp(16), dp(20), dp(4))
            })
            for (chat in members) crew.addView(chatRow(chat.session, chat.agent, chat.project, chat.last, chat.found))
        }
        crew.addView(TextView(this).apply {
            text = "Settings"
            setTextColor(getColor(R.color.muted)); textSize = 13f; gravity = Gravity.CENTER
            setPadding(dp(20), dp(28), dp(20), dp(36))
            setOnClickListener { show(settingsScreen) }
        })
        val name = names[current] ?: prefs().getString("current_name", "")?.ifEmpty { null }.takeIf { current.isNotEmpty() }
        who.text = name?.substringBefore(",") ?: "Henchline"
        avatar.setImageResource(if (name != null) avatarOf(current) else R.drawable.avatar_handsfree)
    }

    private fun setUpTalkButton() {
        val hold = Runnable {
            talking = true
            CallService.Bus.service?.send("""{"push": true}""")
            CallService.Bus.service?.beep(true)
        }
        talk.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> main.postDelayed(hold, HOLD_MS)
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    main.removeCallbacks(hold)
                    if (talking) {
                        talking = false
                        CallService.Bus.service?.send("""{"push": false}""")
                        CallService.Bus.service?.beep(false)
                    } else if (event.actionMasked == MotionEvent.ACTION_UP) {
                        buttonOnly = !buttonOnly
                        CallService.Bus.service?.send(JSONObject().put("button_only", buttonOnly).toString())
                        // A brief notice, not a chat line: these piled up in the chat (2026-11-15); the top bar says it too
                        android.widget.Toast.makeText(this, if (buttonOnly) "Open line off: hold to talk" else "Open line on: just talk",
                            android.widget.Toast.LENGTH_SHORT).show()
                        view.performClick()
                    }
                }
            }
            true
        }
    }

    private var voiceNote: VoiceNote? = null  // recording while the mic is held, or locked
    private var locked = false  // slid up: recording on without holding, sent with the button, dropped with ✕
    private var lockHint: android.widget.PopupWindow? = null  // the lock above the mic, while it is held
    private var tapped = false  // a new touch on the locked button
    private var downX = 0f
    private var downY = 0f

    /** Off a call, with the box empty, the send button is a mic, as in a messaging app (the user's wish, 2026-11-16): hold
     *  to record and let go to send; slide left to drop it; slide up to lock it on, then the button sends and ✕
     *  drops. The button follows the finger, so it feels like something is happening. The laptop transcribes the
     *  message and types it to this chat's agent. */
    private fun setUpVoiceNotes() {
        val swap = { if (voiceNote == null) send.setImageResource(if (micMode()) R.drawable.ic_mic else R.drawable.ic_send) }
        text.addTextChangedListener(object : android.text.TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: android.text.Editable?) = swap()
        })
        swap()
        send.setOnTouchListener { _, event ->
            if (!micMode() && voiceNote == null) return@setOnTouchListener sendTouch(event)  // a tap sends; held, sends now
            if (locked) {  // the button is "send" now, for a new tap: the finger that locked it lifting is not one
                if (event.actionMasked == MotionEvent.ACTION_DOWN) tapped = true
                if (event.actionMasked == MotionEvent.ACTION_UP && tapped) endVoiceNote(keep = true)
                return@setOnTouchListener true
            }
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> { downX = event.rawX; downY = event.rawY; startVoiceNote() }
                MotionEvent.ACTION_MOVE -> if (voiceNote != null) {
                    // Raw coordinates: the button moves under the finger, so its own would chase themselves
                    val dx = (event.rawX - downX).coerceAtMost(0f)
                    val dy = (event.rawY - downY).coerceAtMost(0f)
                    val sideways = -dx > -dy  // one direction at a time, as messaging apps do
                    send.translationX = if (sideways) dx else 0f
                    send.translationY = if (sideways) 0f else dy
                    lockHint?.contentView?.alpha = if (sideways) 0.4f else 1f
                    when {
                        dx < -dp(110) -> endVoiceNote(keep = false, why = "Dropped")
                        dy < -dp(90) -> lockVoiceNote()
                    }
                }
                MotionEvent.ACTION_UP -> endVoiceNote(keep = true)
                MotionEvent.ACTION_CANCEL -> endVoiceNote(keep = false, why = "Dropped")
            }
            true
        }
    }

    private fun micMode() = !onCall() && text.text.isNullOrBlank()

    private fun startVoiceNote() {
        if (current.isEmpty() || voiceNote != null) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
            return
        }
        val note = VoiceNote()
        if (!note.start()) { say("", "The microphone is busy.", note = true); return }
        voiceNote = note
        send.backgroundTintList = android.content.res.ColorStateList.valueOf(0xFFE24A4A.toInt())  // red while recording
        send.animate().scaleX(1.5f).scaleY(1.5f).setDuration(160).setInterpolator(android.view.animation.OvershootInterpolator()).start()
        send.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS)
        lockHint = android.widget.PopupWindow(TextView(this).apply {  // above the mic: slide up here to lock
            text = "🔒\n▲"
            gravity = Gravity.CENTER
            setTextColor(getColor(R.color.text)); textSize = 16f
            setPadding(dp(10), dp(10), dp(10), dp(10))
            background = GradientDrawable().apply { setColor(getColor(R.color.surface_high)); cornerRadius = dp(22).toFloat() }
        }, dp(48), dp(76)).apply {
            showAsDropDown(send, 0, -send.height - dp(130))
            contentView.translationY = dp(20).toFloat()
            contentView.animate().translationY(0f).setDuration(220).start()
            bounce(contentView)
        }
        main.post(recordClock)
    }

    /** The lock's arrow nudges up now and then, as a hint. */
    private fun bounce(view: View) {
        view.animate().translationY(-dp(6).toFloat()).setDuration(450).withEndAction {
            view.animate().translationY(0f).setDuration(450).withEndAction { if (lockHint?.isShowing == true) bounce(view) }.start()
        }.start()
    }

    /** The time recorded so far, in the message box, with what to do next. */
    private val recordClock = object : Runnable {
        override fun run() {
            val note = voiceNote ?: return
            val seconds = (System.currentTimeMillis() - note.started) / 1000
            if (seconds >= VoiceNote.LONGEST) return endVoiceNote(keep = true)  // at the cap: sent, and the mic let go
            val clock = "%d:%02d".format(seconds / 60, seconds % 60)
            text.hint = if (locked) "🔴 $clock   ✕ drops it, the button sends" else "🔴 $clock   ◀ slide to drop · ▲ to lock"
            main.postDelayed(this, 500)
        }
    }

    /** Slid up: recording stays on hands-off; the mic springs back as a send button, ✕ (where the paperclip is)
     *  drops it. */
    private fun lockVoiceNote() {
        if (locked || voiceNote == null) return
        locked = true
        tapped = false
        lockHint?.dismiss(); lockHint = null
        send.animate().translationX(0f).translationY(0f).scaleX(1f).scaleY(1f).setDuration(200)
            .setInterpolator(android.view.animation.OvershootInterpolator()).start()
        send.setImageResource(R.drawable.ic_send)
        attach.setImageResource(R.drawable.ic_close)
        attach.contentDescription = "Drop the voice message"
        send.performHapticFeedback(if (android.os.Build.VERSION.SDK_INT >= 30) android.view.HapticFeedbackConstants.CONFIRM else android.view.HapticFeedbackConstants.VIRTUAL_KEY)
        recordClock.run()
    }

    private fun endVoiceNote(keep: Boolean, why: String = "") {
        val note = voiceNote ?: return
        voiceNote = null
        locked = false
        main.removeCallbacks(recordClock)
        lockHint?.dismiss(); lockHint = null
        send.backgroundTintList = android.content.res.ColorStateList.valueOf(getColor(R.color.cyan))
        // Back in place with a little spring; a dropped one gives a shake, so it is clearly gone
        send.animate().translationX(0f).translationY(0f).scaleX(1f).scaleY(1f).setDuration(220)
            .setInterpolator(android.view.animation.OvershootInterpolator()).start()
        if (!keep) send.postDelayed({
            android.animation.ObjectAnimator.ofFloat(send, "rotation", 0f, -14f, 14f, -8f, 8f, 0f).setDuration(320).start()
        }, 220)
        attach.setImageResource(R.drawable.ic_add)
        attach.contentDescription = "Add a photo, a picture or files"
        showConnected(onCall())  // the hint and the mic back
        val short = System.currentTimeMillis() - note.started < 700
        val wav = note.stop(keep && !short)
        if (wav == null) {
            android.widget.Toast.makeText(this, if (short && keep) "Hold to record" else why, android.widget.Toast.LENGTH_SHORT).show()
            return
        }
        val id = java.util.UUID.randomUUID().toString()
        val to = current
        val waiting = "🎙 Voice message…"
        Chats.add(this, to, JSONObject().put("from", "you").put("text", waiting).put("mine", true).put("id", id).put("status", "sent"))
        val bubble = say("you", waiting, mine = true, session = to, keep = false, status = "sent")
        sentBubbles[id] = bubble to waiting
        Thread {
            val (failed, heard) = VoiceNote.send(this, to, wav)
            if (failed.isEmpty()) {
                val words = "🎙 $heard"
                Chats.setText(this, to, id, words)
                Chats.setStatus(this, to, id, "delivered")
                main.post {
                    if (sentBubbles[id]?.first === bubble) sentBubbles[id] = bubble to words
                    // And the bubble waiting to turn blue: the agent's answer must not put "Voice message…" back
                    // (it did, 2026-11-16)
                    myBubbles.replaceAll { if (it.first === bubble) bubble to words else it }
                    bubble.text = stamp(bubble, words, "delivered")  // in place: no redraw under you
                }
            } else {
                Chats.setText(this, to, id, "🎙 Voice message (not sent)")
                main.post {
                    bubble.alpha = 0.5f
                    bubble.paintFlags = bubble.paintFlags or Paint.STRIKE_THRU_TEXT_FLAG
                    say("", failed, note = true)
                }
            }
        }.start()
    }

    // Your lines on screen still waiting in the agent's queue (one tick): they don't turn blue with its next answer
    private val waitingBubbles = java.util.Collections.newSetFromMap(java.util.WeakHashMap<TextView, Boolean>())

    // --- send now: the send button held (the user's design, 2026-11-16) ----------------------------------------
    // Held, it buzzes, turns amber and pulses: let go, and the message interrupts the agent's work, so it gets the
    // words at once instead of after what it is doing. Slid left while held: cancelled, the words stay in the box.
    private var urgentArmed = false
    private var urgentCancelled = false
    private var urgentDownX = 0f
    private var urgentDownY = 0f
    private var urgentPulse: android.animation.Animator? = null
    private var urgentTint: android.content.res.ColorStateList? = null
    private val armUrgent = Runnable {
        urgentArmed = true
        urgentTint = send.backgroundTintList
        send.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS)
        try {  // a double buzz: unlike a plain hold
            getSystemService(android.os.Vibrator::class.java)?.vibrate(
                android.os.VibrationEffect.createWaveform(longArrayOf(0, 35, 70, 35), -1))
        } catch (_: Exception) {}
        send.backgroundTintList = android.content.res.ColorStateList.valueOf(0xFFF5A623.toInt())  // amber
        send.animate().scaleX(1.3f).scaleY(1.3f).setDuration(160).setInterpolator(android.view.animation.OvershootInterpolator()).start()
        urgentPulse = android.animation.ObjectAnimator.ofPropertyValuesHolder(send,
            android.animation.PropertyValuesHolder.ofFloat(View.SCALE_X, 1.3f, 1.42f),
            android.animation.PropertyValuesHolder.ofFloat(View.SCALE_Y, 1.3f, 1.42f)).apply {
            duration = 420; repeatCount = android.animation.ValueAnimator.INFINITE
            repeatMode = android.animation.ValueAnimator.REVERSE; startDelay = 160; start()
        }
        android.widget.Toast.makeText(this, "Let go: send now, interrupting · Slide up: send later · Slide left: cancel",
            android.widget.Toast.LENGTH_SHORT).show()
    }

    private fun relaxSend() {
        urgentPulse?.cancel(); urgentPulse = null
        urgentTint?.let { send.backgroundTintList = it }
        send.animate().scaleX(1f).scaleY(1f).translationX(0f).translationY(0f).setDuration(160).start()
    }

    /** The send button with words in the box: a tap sends, a hold sends now (see armUrgent). */
    private fun sendTouch(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                urgentArmed = false; urgentCancelled = false; urgentDownX = event.rawX; urgentDownY = event.rawY
                send.postDelayed(armUrgent, 450)
            }
            MotionEvent.ACTION_MOVE -> {
                val dx = event.rawX - urgentDownX
                val dy = event.rawY - urgentDownY
                if (urgentArmed && !urgentCancelled) {
                    send.translationX = dx.coerceIn(-dp(110).toFloat(), 0f)
                    send.translationY = dy.coerceIn(-dp(110).toFloat(), 0f)
                    if (-dy > dp(90) && kotlin.math.abs(dx) < dp(60)) {  // slid up: the send-later menu, words kept
                        urgentCancelled = true
                        try {  // a clear tick (the view's own haptic is often off on this phone)
                            getSystemService(android.os.Vibrator::class.java)?.vibrate(
                                android.os.VibrationEffect.createOneShot(40, android.os.VibrationEffect.DEFAULT_AMPLITUDE))
                        } catch (_: Exception) {}
                        relaxSend()
                        text.text.toString().trim().takeIf { it.isNotEmpty() }?.let { later(it) }  // on a call it can be empty
                    }
                    // Slid away, left or off the button any way: not sent, the words stay
                    else if (dx < -dp(90) || dy > dp(80) || dx > dp(80)) {
                        urgentCancelled = true
                        send.performHapticFeedback(android.view.HapticFeedbackConstants.CLOCK_TICK)
                        relaxSend()
                        android.widget.Toast.makeText(this, "Cancelled", android.widget.Toast.LENGTH_SHORT).show()
                    }
                } else if (!urgentArmed && (kotlin.math.abs(dx) > dp(24) || kotlin.math.abs(dy) > dp(24))) {
                    send.removeCallbacks(armUrgent)  // a swipe, not a hold
                    urgentCancelled = true
                }
            }
            MotionEvent.ACTION_UP -> {
                send.removeCallbacks(armUrgent)
                when {
                    urgentCancelled -> {}
                    urgentArmed -> {
                        relaxSend()
                        if (text.text.isNullOrBlank() && onCall()) {  // nothing typed: the turn being spoken cuts in
                            CallService.Bus.service?.send(JSONObject().put("interrupt", true).toString())
                            android.widget.Toast.makeText(this, "Cutting in", android.widget.Toast.LENGTH_SHORT).show()
                        } else sendTyped(urgent = true)
                    }
                    else -> sendTyped()
                }
            }
            MotionEvent.ACTION_CANCEL -> { send.removeCallbacks(armUrgent); if (urgentArmed) relaxSend() }
        }
        return true
    }

    private fun sendTyped(urgent: Boolean = false) {
        val entered = text.text.toString().trim()
        if (entered.isEmpty()) return
        if (entered == "/later" || entered.startsWith("/later ")) {  // words to send at a time picked next
            text.setText("")
            later(entered.removePrefix("/later").trim())
            return
        }
        if (entered == "/show" || entered.startsWith("/show ")) {
            text.setText("")
            showGrab(entered.removePrefix("/show").trim())
            return
        }
        if (entered.startsWith("/") && entered.drop(1) in PHONE_COMMANDS) {  // the phone's own: done here, not typed in
            text.setText("")
            runCommand(entered.drop(1))
            return
        }
        // Replying to a message: it goes along, on the same line (a newline would send early in a terminal)
        val words = if (quote.isEmpty()) entered else "(replying to: \"${quote.replace("\n", " ").take(160)}\") $entered"
        setQuote("")
        val id = java.util.UUID.randomUUID().toString()
        if (!onCall()) {  // not the call's chat: a text, to the agent whose chat this is
            if (current.isEmpty()) return
            Chats.add(this, current, JSONObject().put("from", "you").put("text", words).put("mine", true).put("id", id).put("status", "sent"))
            val bubble = say("you", words, mine = true, session = current, keep = false, status = "sent")
            sentBubbles[id] = bubble to words
            text.setText("")
            val to = current
            Thread {
                // Others still wait for the laptop: this one goes behind them, never ahead
                val sent = if (Outbox.all(this).isNotEmpty()) Messages.Sent("", false, offline = true)
                           else Messages.text(this, to, words, id, urgent)
                val (note, queued) = sent.note to sent.queued
                if (sent.offline) {  // the laptop away: a clock, and the Outbox sends it as soon as it can
                    Outbox.add(this, id, to, words)
                    Chats.setStatus(this, to, id, "waiting", force = true)
                    main.post { bubble.text = stamp(bubble, words, "waiting") }
                    return@Thread
                }
                if (note.isEmpty() && queued) main.post { waitingBubbles.add(bubble) }  // one tick until it takes it in
                if (note.isEmpty() && !queued) {
                    val now = Chats.setStatus(this, to, id, "delivered")
                    main.post { tick(id, now) }  // in place: no redraw under you
                }
                if (note.isNotEmpty()) main.post {
                    bubble.alpha = 0.5f
                    bubble.paintFlags = bubble.paintFlags or Paint.STRIKE_THRU_TEXT_FLAG
                    say("", note, note = true)
                }
            }.start()
            return
        }
        CallService.Bus.service?.lastSession = current
        CallService.Bus.service?.send(JSONObject().put("chat", words).put("session", current).put("id", id)
            .put("urgent", urgent).toString())
        Chats.add(this, current, JSONObject().put("from", "you").put("text", words).put("mine", true).put("id", id).put("status", "sent"))
        val bubble = say("you", words, mine = true, session = current, keep = false, status = "sent")
        waitingBubbles.add(bubble)  // one tick until the laptop says it was taken in
        bubble.tag = "typed-$id"
        sentBubbles[id] = bubble to words
        text.setText("")
    }

    /** Back to the call's chat, from anywhere (the call bar, or Call while texting another agent). */
    private fun openCall() {
        if (callSession.isNotEmpty() && callSession != current) open(callSession) else { showConnected(onCall()); show(chatScreen) }
    }

    private var callWords = ""  // what is happening on the call ("speaking", "listening"), for the bar's second line
    private var pulse: android.animation.Animator? = null  // the live ring's beat: only while the bar shows and the app is seen

    private fun pulseLive(on: Boolean) {
        if (on == (pulse != null)) return
        pulse?.cancel(); pulse = null
        if (!on) return
        val ring = findViewById<View>(R.id.callPulse)
        pulse = android.animation.AnimatorSet().apply {
            playTogether(
                android.animation.ObjectAnimator.ofFloat(ring, View.SCALE_X, 1f, 1.35f).apply { repeatCount = android.animation.ValueAnimator.INFINITE },
                android.animation.ObjectAnimator.ofFloat(ring, View.SCALE_Y, 1f, 1.35f).apply { repeatCount = android.animation.ValueAnimator.INFINITE },
                android.animation.ObjectAnimator.ofFloat(ring, View.ALPHA, 0.7f, 0f).apply { repeatCount = android.animation.ValueAnimator.INFINITE })
            duration = 1600
            start()
        }
    }

    /** While a call is up, on every screen, the same bar (the user's wish, 2026-11-16: it changed between the list
     *  and the chat): the agent's face with a live ring, its name, the call's time and what is happening, speaker
     *  and hang-up. The status bar turns the bar's green with it. */
    private fun showCallBar() {
        val up = CallService.Bus.connected
        val bar = findViewById<View>(R.id.callBar)
        if (up != (bar.visibility == View.VISIBLE)) {
            bar.visibility = if (up) View.VISIBLE else View.GONE
            window.statusBarColor = getColor(if (up) R.color.call_bar else R.color.navy)
            // Newer Android draws the app under the status bar and ignores its colour: our own strip shows there
            findViewById<View>(R.id.statusSpace).setBackgroundColor(getColor(if (up) R.color.call_bar else R.color.navy))
        }
        pulseLive(up && !isFinishing && lifecycleSeen)
        val timer = findViewById<android.widget.Chronometer>(R.id.callTimer)
        if (!up) { timer.stop(); cutIn = null; return }
        val name = names[callSession]?.substringBefore(",") ?: "Your crew"
        findViewById<TextView>(R.id.callName).text = name
        findViewById<ImageView>(R.id.callAvatar).setImageResource(avatarOf(callSession.ifEmpty { "handsfree" }))
        val started = CallService.Bus.service?.callStartedAt ?: 0L
        if (started > 0) {
            timer.base = android.os.SystemClock.elapsedRealtime() - (System.currentTimeMillis() - started)
            timer.start()
        }
        findViewById<TextView>(R.id.callStatus).text = if (callWords.isNotEmpty()) " · $callWords" else ""
        bar.contentDescription = "On a call with $name. Tap to return; hold to switch who it's with."
        showSpeaker(CallService.Bus.service?.speaker ?: prefs().getBoolean("speaker", true))
    }

    /** Cursor cuts in unless the call menu says otherwise; Claude and Codex let a turn wait. */
    private fun cutsIn() = cutIn ?: callSession.startsWith("cursor")

    /** The call's menu: who it's with and the crew to move it to, sound, the line, hang up. */
    private fun callMenu() {
        val service = CallService.Bus.service ?: return
        panel = CallMenu(this, names[callSession]?.substringBefore(",") ?: "Your crew",
            names.map { (session, name) -> session to name }, callSession,
            speaker = service.speaker, where = service.where,
            earbuds = android.os.Build.VERSION.SDK_INT >= 31 && service.headsetAvailable(),
            onRoute = { to -> route(to) },
            clear = service.clear, clearAllowed = service.where != "earpiece", buttonOnly = buttonOnly, stayOn = stayOn,
            cutIn = cutsIn(),
            onSwitch = { session -> moveCallTo(session) },
            onSpeaker = { on -> if (service.speaker != on) speakerButton.performClick() },
            onClear = { on -> setClearSound(on) },
            onButtonOnly = { only ->
                if (buttonOnly != only) {
                    buttonOnly = only
                    service.send(JSONObject().put("button_only", buttonOnly).toString())
                }
            },
            onStayOn = { on ->
                if (stayOn != on) {
                    stayOn = on
                    service.send(JSONObject().put("stay_on", stayOn).toString())
                }
            },
            onCutIn = { on ->
                cutIn = on
                service.send(JSONObject().put("cut_in", on).toString())
            },
            onVoices = { voices() },
            onHangUp = { hangUp() }).also { it.show() }
    }

    /** The call moved to another agent: the laptop is told, and the phone follows at once (that agent's chat,
     *  loaded, and the bar's name; it waited for the laptop's next word, then didn't load the chat, 2026-11-16). */
    private fun moveCallTo(session: String) {
        val service = CallService.Bus.service ?: return
        cutIn = null  // the new agent's own default: Cursor cuts in, the others let it wait
        service.send(JSONObject().put("cut_in", "auto").toString())
        service.send(JSONObject().put("switch", session).toString())
        callSession = session
        switchedAt = System.currentTimeMillis()
        open(session)
        showCallBar()
        showChipIn(null)
    }

    private var chipIn: ImageView? = null  // another agent speaking into this call, on the green bar
    private var chipInOf = ""

    /** Another agent talking on this call (the user's design, 2026-11-17): its face pops up on the green bar beside
     *  the one called, leaning in and bobbing while it talks, as if chipping in; a tap moves the call to it. Gone
     *  again, sliding out, when it stops. */
    private fun showChipIn(speaker: JSONObject?) {
        val session = speaker?.optString("session").orEmpty()
        val agent = speaker?.optString("agent").orEmpty()
        val wanted = session.isNotEmpty() && session != callSession && agent.isNotEmpty() && agent != "handsfree"
        val bar = findViewById<LinearLayout>(R.id.callBar)
        val face = chipIn ?: ImageView(this).apply {
            visibility = View.GONE
            contentDescription = "Another agent chipping in: tap to move the call to it"
            setOnClickListener { chipInOf.takeIf { it.isNotEmpty() }?.let { moveCallTo(it) } }
            bob(this, 0, dp(3).toFloat())
            bar.addView(this, 2, LinearLayout.LayoutParams(dp(34), dp(34)).apply { marginEnd = dp(8) })
        }.also { chipIn = it }
        if (wanted && (face.visibility != View.VISIBLE || chipInOf != session)) {
            chipInOf = session
            face.setImageResource(avatarOf(session.ifEmpty { agent }))
            face.performHapticFeedback(android.view.HapticFeedbackConstants.CLOCK_TICK)
            face.visibility = View.VISIBLE
            face.alpha = 0f; face.translationX = dp(28).toFloat(); face.rotation = 18f; face.scaleX = 0.5f; face.scaleY = 0.5f
            face.animate().alpha(1f).translationX(0f).rotation(-6f).scaleX(1f).scaleY(1f).setDuration(320)
                .setInterpolator(android.view.animation.OvershootInterpolator(2.2f)).start()
        } else if (!wanted && face.visibility == View.VISIBLE) {
            chipInOf = ""
            face.animate().alpha(0f).translationX(dp(28).toFloat()).scaleX(0.5f).scaleY(0.5f).setDuration(220)
                .withEndAction { face.visibility = View.GONE }.start()
        }
    }

    /** On the call, a tap on the agent's name or face: who the call is with, switched (as "switch me to …"). */
    private fun switchCall() {
        val crew = names.map { (session, name) -> JSONObject().put("id", session).put("name", name) }
        if (crew.size < 2) return
        panel = ModelPicker(this, "Switch the call to", crew, withDefault = false) { session, _ ->
            CallService.Bus.service?.send(JSONObject().put("switch", session).toString())
            callSession = session  // the phone follows at once, as from the call's menu
            switchedAt = System.currentTimeMillis()
            open(session)
            showCallBar()
        }.also { it.show() }
    }

    /** The clear-sound switch shows this, without counting as a flip. A flip switches the call too, if one is up. */
    private fun showClear(on: Boolean) {
        clear.setOnCheckedChangeListener(null)
        clear.isChecked = on
        clear.setOnCheckedChangeListener { _, flipped ->
            val service = CallService.Bus.service
            if (service != null && service.clear != flipped && !service.switchSound(flipped)) {
                showClear(!flipped)  // still switching from the last flip, or clear sound on the earpiece
                if (flipped && service.where == "earpiece") android.widget.Toast.makeText(this,
                    "On the earpiece: call sound only", android.widget.Toast.LENGTH_SHORT).show()
                return@setOnCheckedChangeListener
            }
            prefs().edit().putBoolean("clear", flipped).putBoolean("clear_back", false).apply()  // a choice of yours
            if (service != null) android.widget.Toast.makeText(this, if (flipped) "Clear sound" else "Call sound",
                android.widget.Toast.LENGTH_SHORT).show()
        }
    }

    /** Clear or call sound, by the call's own state, not the switch on screen: that one lags a switch still under
     *  way, and a choice that matched it did nothing (the user couldn't get clear sound back, 2026-11-16). Through
     *  the switch's listener, so the sound, the speaker and the saved choice all follow. */
    private fun setClearSound(on: Boolean) {
        val service = CallService.Bus.service
        if (service != null && service.clear == on) { showClear(on); return }
        showClear(!on)  // shown as the opposite without a flip, so the next line is one
        clear.isChecked = on
    }

    /** Earbuds, speaker or earpiece, picked from the list (earbuds connected): the call's sound goes there, clear
     *  or call sound as it was, except on the earpiece. */
    private fun route(to: String) {
        val service = CallService.Bus.service ?: return
        val wasClear = service.clear
        if (!service.setRoute(to)) return  // still switching from the last tap
        if (to != "headset") prefs().edit().putBoolean("speaker", to == "speaker").apply()
        afterMove(service, wasClear)
        main.postDelayed({ showSpeaker(service.speaker) }, 300)
    }

    /** The earpiece is call sound only (the user's wish, 2026-11-16): moving there from clear sound leaves it, and
     *  moving to the speaker or the earbuds afterwards brings it back. */
    private fun afterMove(service: CallService, wasClear: Boolean) {
        if (wasClear && service.where == "earpiece") {
            prefs().edit().putBoolean("clear", false).putBoolean("clear_back", true).apply()
            showClear(false)
        } else if (service.where != "earpiece" && prefs().getBoolean("clear_back", false)) {
            prefs().edit().putBoolean("clear_back", false).apply()
            setClearSound(true)
        }
    }

    private val devicesWatch = object : android.media.AudioDeviceCallback() {  // earbuds come or go: the button says so
        override fun onAudioDevicesAdded(added: Array<out android.media.AudioDeviceInfo>) { refreshRoute() }
        override fun onAudioDevicesRemoved(removed: Array<out android.media.AudioDeviceInfo>) { refreshRoute() }
    }

    private fun refreshRoute() {
        val service = CallService.Bus.service ?: return
        if (CallService.Bus.connected) showSpeaker(service.speaker)
    }

    /** The speaker button as it is: lit on the loudspeaker, dim on the earpiece. */
    private fun showSpeaker(on: Boolean) {
        val where = CallService.Bus.service?.where ?: if (on) "speaker" else "earpiece"
        val lit = where != "earpiece"
        speakerButton.setImageResource(if (where == "headset") R.drawable.ic_headset else R.drawable.ic_sound)
        speakerButton.background = if (lit) android.graphics.drawable.InsetDrawable(
            GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(0x33FFFFFF) }, dp(6)) else null
        speakerButton.alpha = if (lit) 1f else 0.6f
        speakerButton.contentDescription = when (where) {
            "headset" -> "Earbuds: tap to pick earbuds, speaker or earpiece"
            "speaker" -> "Speaker on: tap for the earpiece"
            else -> "Earpiece: tap for the speaker"
        }
    }

    private fun showConnected(on: Boolean) {
        // While a call is up the call bar has its controls: the header's call button would only repeat them
        callButton.visibility = if (CallService.Bus.connected) View.GONE else View.VISIBLE
        callButton.setImageResource(if (on) R.drawable.ic_call_end else R.drawable.ic_phone)
        callButton.setBackgroundResource(if (on) R.drawable.round_hang else R.drawable.round_call)
        callButton.contentDescription = if (on) "Hang up" else "Call"
        // Off a call the message box stays when an agent's chat is shown: what is typed (or attached) is a text
        val typing = on || current.isNotEmpty()
        talk.isEnabled = on
        text.isEnabled = typing
        send.isEnabled = typing
        attach.isEnabled = typing
        send.setImageResource(if (micMode()) R.drawable.ic_mic else R.drawable.ic_send)  // off a call, empty: voice
        val name = names[current] ?: prefs().getString("current_name", "")?.ifEmpty { null }
        text.hint = if (on) "Message your crew…" else "Text ${name?.substringBefore(",") ?: "this agent"}…"
        // Off a call there is nothing to hold or type to: just the henchman and how to call
        val shown = if (on) View.VISIBLE else View.GONE
        talk.visibility = shown
        findViewById<View>(R.id.inputRow).visibility = if (typing) View.VISIBLE else View.GONE
        // Off a call: the kept chat with the last agent, read-only, or the henchman and how to call
        showChatBody()
        showClear(CallService.Bus.service?.clear ?: prefs().getBoolean("clear", true))
        if (!on) {
            state.text = subtitle(if (CallService.Bus.connected) "Texting · on a call with ${names[callSession]?.substringBefore(",") ?: "your crew"}"
                         else "")  // off a call: just the project (the user's wish, 2026-11-16: "not on a call" said nothing)
            showMode("waiting")
        }
        showCrew()
    }

    /** The laptop tray's colours: grey while only the wake word gets in, then green, red, yellow. */
    private fun showMode(mode: String) {
        val colour = when (mode) {
            "listening" -> 0xFF3CB371
            "recording" -> 0xFFE24A4A
            "transcribing" -> 0xFFE0A100
            else -> 0xFF5A6A8E
        }.toInt()
        talk.background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(colour)
            setStroke(dp(3), getColor(R.color.cyan) and 0x55FFFFFF)  // a faint goggle-cyan ring
        }
    }

    /** A chat line: the agent's henchman and a bubble on the left, yours on the right, notes in the middle. */
    private fun say(from: String, words: String, mine: Boolean = false, note: Boolean = false, session: String = "",
                    keep: Boolean = true, status: String = "", at: Long = System.currentTimeMillis()): TextView {
        // Kept: the line's own time, by which a long-press finds it to delete
        val at = if (keep && !note) JSONObject().put("from", from).put("text", words).put("mine", mine)
            .also { Chats.add(this, session, it) }.optLong("at", at) else at
        if (!note && at > 0) dayLine(at)
        val bubble = Stamped(this).apply {
            if (!note) bubbleAt[this] = at
            text = if (note) words else stamp(this, words, if (mine) status else "")
            setTextColor(getColor(if (note) R.color.muted else R.color.text))
            textSize = if (note) 13f else 15f
            if (!note) {
                setBackgroundResource(if (mine) R.drawable.bubble_mine else R.drawable.bubble_agent)
                setPadding(dp(14), dp(10), dp(14), dp(10))
            }
        }
        if (mine && (status == "sent" || status == "delivered")) myBubbles.add(bubble to words)  // to turn blue
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = when {
                note -> Gravity.CENTER_HORIZONTAL
                mine -> Gravity.END
                else -> Gravity.START or Gravity.BOTTOM
            }
            setPadding(0, dp(4), 0, dp(4))
            setTag(R.id.chat, session)
            visibility = if (session.isEmpty() || current.isEmpty() || session == current) View.VISIBLE else View.GONE
        }
        if (!note && !mine && from.isNotEmpty()) {
            row.addView(ImageView(this).apply { setImageResource(avatarOf(session.ifEmpty { from })) },
                LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginEnd = dp(8) })
        }
        if (!note) swipeToReply(row, words) { messageMenu(session.ifEmpty { current }, listOf(at), words) }
        if (!note && at > 0) row.setTag(R.id.ats, listOf(at))
        row.addView(bubble, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT).apply { if (!note) width = LinearLayout.LayoutParams.WRAP_CONTENT })
        if (mine && !note) {  // you: the boss the crew works for, the vacation face while away
            row.addView(ImageView(this).apply { setImageResource(youFace()); tag = "you" },
                LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginStart = dp(8) })
        }
        bubble.maxWidth = (resources.displayMetrics.widthPixels * 0.7).toInt()
        chat.addView(row, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT))
        // A new chat opens empty, showing the henchman instead of the lines: its first line brings the lines back
        // (a first text was sent but never shown, 2026-11-16). On the Plan tab the lines stay underneath.
        if (row.visibility == View.VISIBLE) showChatBody()
        chatScroll.post { chatScroll.fullScroll(View.FOCUS_DOWN) }
        return bubble
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    private val myBubbles = mutableListOf<Pair<TextView, String>>()  // your lines on screen not yet read
    private val sentBubbles = HashMap<String, Pair<TextView, String>>()  // your lines on screen by id, for their ticks

    /** A line of yours moved on (delivered): its ticks change where it is, nothing else redrawn. */
    private fun tick(id: String, status: String) {
        refreshStop()
        val (bubble, words) = sentBubbles[id] ?: return
        waitingBubbles.remove(bubble)
        bubble.text = stamp(bubble, words, status)
    }

    /** Whether the agent of the chat on screen is working right now, as the laptop sees it (Cursor's page, Claude's
     *  and Codex's logs): a bubble at the bottom of its chat, as a messaging app shows someone typing (the user's
     *  wish, 2026-11-16: the terminal shows it at once, the phone showed nothing). null: that agent gives no sign. */
    private var agentWorking: Boolean? = null
    private var workingOf = ""  // the chat it belongs to
    private val workingPoll = object : Runnable {
        override fun run() {
            pollWorking()
            if (chatScreen.visibility == View.VISIBLE) main.postDelayed(this, 2500)
        }
    }

    private fun pollWorking() {
        val session = current
        if (session.isEmpty() || Files.origin(this) == null) return
        if (workingOf != session) { workingOf = session; showWorking(null); showWorkingBubble("", "") }
        ask("/chat/working?session=" + android.net.Uri.encode(session), null) { answer ->
            if (session != current || chatScreen.visibility != View.VISIBLE) return@ask
            showWorking(if (answer.isNull("working")) null else answer.optBoolean("working"))
            answer.optJSONArray("taken")?.let { taken ->  // the agent took these in: two ticks
                for (i in 0 until taken.length()) {
                    val id = taken.getString(i)
                    val at = (answer.optJSONObject("taken_at")?.optDouble(id, 0.0) ?: 0.0) * 1000
                    tick(id, Chats.setStatus(this, session, id, "delivered", takenAt = at.toLong()))
                }
            }
            showWorkingBubble(answer.optString("doing"), answer.optString("label"))
            showWorkflowStrip(answer.optJSONObject("workflow"))
        }
    }

    // --- Workflows: a Claude chat's multi-agent runs, watched from the phone (the user's wish, 2026-11-17) --------

    private var flowRow: View? = null  // the strip at the bottom of the chat while a workflow runs in it
    private var flowName: TextView? = null
    private var flowPhase: TextView? = null
    private var flowBar: LinearLayout? = null

    /** The run going on now: its name, its phase and a bar, below the chat's last line (and below the working
     *  bubble); a tap opens its sheet. Gone when nothing runs. */
    private fun showWorkflowStrip(flow: JSONObject?) {
        if (flow == null) {
            flowRow?.let { (it.parent as? android.view.ViewGroup)?.removeView(it) }
            flowRow = null
            return
        }
        val draw = UsageDraw(this)
        if (flowRow == null) {
            val card = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundResource(R.drawable.bubble_agent)
                setPadding(dp(14), dp(10), dp(14), dp(12))
                setOnClickListener { workflows() }
            }
            flowName = TextView(this).apply { setTextColor(getColor(R.color.text)); textSize = 15f; typeface = android.graphics.Typeface.DEFAULT_BOLD }
            flowPhase = TextView(this).apply { setTextColor(getColor(R.color.muted)); textSize = 13f }
            flowBar = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            card.addView(flowName); card.addView(flowPhase)
            card.addView(flowBar, LinearLayout.LayoutParams(dp(220), LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(8) })
            flowRow = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.START or Gravity.BOTTOM
                setPadding(0, dp(4), 0, dp(4))
                addView(ImageView(this@MainActivity).apply { setImageResource(avatarOf(current)) },
                    LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginEnd = dp(8) })
                addView(card)
            }
        }
        val done = flow.optInt("done")
        val total = flow.optInt("total").coerceAtLeast(1)
        flowName?.text = "⚙ " + flow.optString("name")
        flowPhase?.text = listOfNotNull(
            if (flow.optInt("phases") > 0) "Phase ${flow.optInt("at").coerceAtLeast(1)}/${flow.optInt("phases")}" else null,
            flow.optString("phase").ifEmpty { null }, "$done/${flow.optInt("total")} agents  ›").joinToString(" · ")
        flowBar?.removeAllViews()
        flowBar?.addView(draw.bar(listOf(100.0 * done / total to 0xFF2BC4D9.toInt())),
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(8)))
        // Just above the working bubble, which stays the very last row (both moving to the end swapped them)
        val row = flowRow!!
        val bubble = workingRow?.takeIf { it.parent === chat }
        val wanted = if (bubble != null) chat.indexOfChild(bubble) - (if (row.parent === chat) 1 else 0) else
            chat.childCount - (if (row.parent === chat) 1 else 0)
        if (row.parent !== chat || chat.indexOfChild(row) != wanted) {
            (row.parent as? android.view.ViewGroup)?.removeView(row)
            val at = workingRow?.takeIf { it.parent === chat }?.let { chat.indexOfChild(it) } ?: chat.childCount
            chat.addView(row, at, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
            chatScroll.post { chatScroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    /** The chat's newest workflow (or /workflow): its phases and agents, kept up to date while the sheet is open. */
    private fun workflows() {
        val session = current.ifEmpty { return }
        ask("/chat/workflows?session=" + android.net.Uri.encode(session), null) { answer ->
            val runs = answer.optJSONArray("runs")
            if (runs == null || runs.length() == 0) return@ask android.widget.Toast.makeText(this,
                "No workflow has run in this chat.", android.widget.Toast.LENGTH_LONG).show()
            val sheet = WorkflowSheet(this, runs.getJSONObject(0),
                refresh = { then -> ask("/chat/workflows?session=" + android.net.Uri.encode(session), null) { again ->
                    again.optJSONArray("runs")?.takeIf { it.length() > 0 }?.let { then(it.getJSONObject(0)) } } },
                askClaude = { words ->  // ready to type: the cursor at the end and the keyboard up (the user's wish)
                    text.setText(words); text.setSelection(text.length()); text.requestFocus()
                    main.postDelayed({  // after the sheet has gone, or its close takes the keyboard down again
                        text.requestFocus(); text.setSelection(text.length())
                        getSystemService(android.view.inputmethod.InputMethodManager::class.java)
                            ?.showSoftInput(text, android.view.inputmethod.InputMethodManager.SHOW_IMPLICIT)
                    }, 300)
                })
            panel = sheet.also { it.show() }
        }
    }

    private var workingRow: View? = null  // the bubble at the bottom of the chat while the agent works
    private var workingKind = ""  // "thinking", "tool", or none

    /** At the bottom of the chat while the agent works, as a messaging app shows someone typing (the user's wish,
     *  2026-11-16): three dots bobbing in a bubble while it thinks or writes; its own small avatar beside a
     *  turning gear while a tool it called runs. Always the last thing in the chat. */
    private var workingLabel: TextView? = null  // what the tool running is, under the dancing avatar

    /** The bubble gone and its animations with it, while nobody sees the chat (they would run on unseen). */
    private fun stopWorkingBubble() {
        agentWorking = null
        showWorkingBubble("", "")
        workingKind = ""
    }

    private fun showWorkingBubble(doing: String, label: String = "") {
        val want = if (agentWorking == true) doing.ifEmpty { "thinking" } else ""
        val row = workingRow
        if (want == workingKind && (want.isEmpty() || row?.parent === chat)) {
            workingLabel?.text = label.ifEmpty { "at work" }  // the next tool, same dance
            if (row != null && chat.indexOfChild(row) != chat.childCount - 1) {  // a new line came below it
                chat.removeView(row); chat.addView(row)
            }
            return
        }
        row?.let { (it.parent as? android.view.ViewGroup)?.removeView(it) }
        workingRow = null; workingKind = want; workingLabel = null
        if (want.isEmpty()) return
        val made = if (want == "tool") gearRow(label) else dotsRow()
        workingRow = made
        chat.addView(made, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        chatScroll.post { chatScroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun workingBubble(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        setPadding(dp(16), dp(12), dp(16), dp(12))
        clipToPadding = false; clipChildren = false  // what bobs and hops shows in full (the dots' tops were cut)
        setBackgroundResource(R.drawable.bubble_agent)  // the agent's next bubble: its fill, its shape and tail
    }

    /** A property animation that runs while the view is on screen: started when it is attached, ended when it is
     *  detached (keeping the bubble last moves it, and a view animation would stop for good). */
    private fun whileShown(view: View, make: () -> android.animation.Animator) {
        var running: android.animation.Animator? = null
        view.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(v: View) { running?.cancel(); running = make().also { it.start() } }
            override fun onViewDetachedFromWindow(v: View) { running?.cancel(); running = null }
        })
    }

    private fun bob(view: View, delay: Long, height: Float) = whileShown(view) {
        android.animation.ObjectAnimator.ofFloat(view, View.TRANSLATION_Y, 0f, -height, 0f).apply {
            duration = 1800; startDelay = delay; repeatCount = android.animation.ValueAnimator.INFINITE
            interpolator = android.view.animation.AccelerateDecelerateInterpolator()  // soft, not jittery (the user)
        }
    }

    private fun dotsRow(): View {
        val bubble = workingBubble()
        for (i in 0 until 3) {
            val dot = View(this).apply {
                background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(getColor(R.color.muted)) }
            }
            bubble.addView(dot, LinearLayout.LayoutParams(dp(7), dp(7)).apply { if (i > 0) marginStart = dp(4) })
            bob(dot, i * 300L, dp(4).toFloat())
        }
        return LinearLayout(this).apply {
            setPadding(dp(36), dp(8), 0, dp(4)); addView(bubble)  // in line with the agent's bubbles (after its face)
            contentDescription = "${names[current]?.substringBefore(",") ?: "The agent"} is thinking"
        }
    }

    /** While a tool it called runs: its avatar dancing (it rocks, bounces and now and then spins, sparks flying),
     *  or, with "Agents dance while they work" off, beside a turning gear; with a few words on the tool. */
    private fun gearRow(label: String): View {
        val bubble = workingBubble()
        val avatar = ImageView(this).apply { setImageResource(avatarOf(current)) }
        bubble.addView(avatar, LinearLayout.LayoutParams(dp(30), dp(30)))
        if (prefs().getBoolean("dance", true)) {
            whileShown(avatar) {
                val rock = android.animation.ObjectAnimator.ofFloat(avatar, View.ROTATION, 0f, -8f, 8f, -8f, 8f, 0f, 360f)
                    .apply { duration = 6000; interpolator = android.view.animation.AccelerateDecelerateInterpolator() }
                val hop = android.animation.ObjectAnimator.ofFloat(avatar, View.TRANSLATION_Y, 0f, -dp(3).toFloat(), 0f,
                    -dp(3).toFloat(), 0f, -dp(7).toFloat(), 0f)
                    .apply { duration = 6000; interpolator = android.view.animation.AccelerateDecelerateInterpolator() }
                android.animation.AnimatorSet().apply {
                    playTogether(rock, hop)
                    addListener(object : android.animation.AnimatorListenerAdapter() {
                        private var cancelled = false
                        override fun onAnimationCancel(animation: android.animation.Animator) { cancelled = true }
                        override fun onAnimationEnd(animation: android.animation.Animator) { if (!cancelled) start() }
                    })
                }
            }
            for ((i, spark) in listOf("✦", "✧", "✦").withIndex()) {
                val star = TextView(this).apply { text = spark; textSize = 11f; setTextColor(getColor(R.color.cyan)) }
                bubble.addView(star, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT).apply { marginStart = dp(if (i == 0) 8 else 3) })
                whileShown(star) {
                    android.animation.ObjectAnimator.ofFloat(star, View.ALPHA, 0.2f, 1f, 0.2f).apply {
                        duration = 2400; startDelay = i * 600L; repeatCount = android.animation.ValueAnimator.INFINITE
                    }
                }
                bob(star, i * 600L, dp(3).toFloat())
            }
        } else {
            val gear = TextView(this).apply { text = "⚙"; textSize = 20f; setTextColor(getColor(R.color.cyan)); gravity = Gravity.CENTER }
            bubble.addView(gear, LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginStart = dp(8) })
            whileShown(gear) {
                android.animation.ObjectAnimator.ofFloat(gear, View.ROTATION, 0f, 360f).apply {
                    duration = 3600; interpolator = android.view.animation.LinearInterpolator()
                    repeatCount = android.animation.ValueAnimator.INFINITE
                }
            }
        }
        val words = TextView(this).apply {
            text = label.ifEmpty { "at work" }; textSize = 13f; setTextColor(getColor(R.color.muted))
            maxLines = 2; ellipsize = android.text.TextUtils.TruncateAt.END
            maxWidth = (resources.displayMetrics.widthPixels * 0.55).toInt()
        }
        workingLabel = words
        bubble.addView(words, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT).apply { marginStart = dp(8) })
        return LinearLayout(this).apply {
            setPadding(0, dp(8), 0, dp(4)); addView(bubble)  // it carries the agent's face itself
            contentDescription = "${names[current]?.substringBefore(",") ?: "The agent"} is running a tool"
        }
    }

    private fun showWorking(working: Boolean?) {
        if (working == agentWorking) return
        agentWorking = working
        refreshStop()
    }

    private val shownThoughts = HashSet<String>()  // the thoughts on screen (session, time): each shown once
    private var thoughtsSince = ""  // the newest thought shown, for the next ask
    private var thoughtsOf = ""  // the chat they belong to
    private val thoughtsPoll = object : Runnable {
        override fun run() {
            pollThoughts()
            if (chatScreen.visibility == View.VISIBLE) main.postDelayed(this, 5000)
        }
    }

    /** A Cursor IDE chat's thinking, as thought bubbles, while its chat is on screen (the user's wish, 2026-11-16:
     *  a thought bubble beside the speech ones). Only thoughts after its last kept line, then each new one. */
    private fun pollThoughts() {
        val session = current
        if (!session.matches(Regex("(cursor|claude)-[0-9a-f-]{36}")) || !prefs().getBoolean("thoughts", true)) return
        if (Files.origin(this) == null) return
        if (thoughtsOf != session) {
            thoughtsOf = session
            val at = Chats.last(this, session)?.optLong("at") ?: 0L
            thoughtsSince = if (at > 0) java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", java.util.Locale.US)
                .apply { timeZone = java.util.TimeZone.getTimeZone("UTC") }.format(java.util.Date(at)) else ""
        }
        ask("/chat/thoughts?session=" + android.net.Uri.encode(session) + "&since=" + android.net.Uri.encode(thoughtsSince), null) { answer ->
            val list = answer.optJSONArray("thoughts") ?: return@ask
            if (session != current || chatScreen.visibility != View.VISIBLE) return@ask
            for (i in 0 until list.length()) {
                val thought = list.getJSONObject(i)
                thoughtsSince = maxOf(thoughtsSince, thought.optString("at"))
                // Reopening a chat asks again from its last kept line, and two asks can cross: never twice (the
                // user saw them repeated, 2026-11-16)
                if (!shownThoughts.add(session + " " + thought.optString("at") + " " + thought.optString("text").hashCode())) continue
                thoughtBubble(thought.optString("text"))
            }
        }
    }

    /** A thought: a cloud, not a speech bubble (as a comic's: soft and round, with the small circles trailing
     *  from the thinker), italic and quieter than speech; two lines and "tap to read all", the rest a tap away. */
    private fun thoughtBubble(words: String) {
        val cloud = getColor(R.color.surface) and 0x00FFFFFF or 0x99000000.toInt()
        val bubble = TextView(this).apply {
            text = words
            setTextColor(getColor(R.color.muted)); textSize = 13.5f
            setTypeface(typeface, android.graphics.Typeface.ITALIC)
            maxLines = 3; ellipsize = android.text.TextUtils.TruncateAt.END
            setPadding(dp(18), dp(12), dp(18), dp(12))
            background = GradientDrawable().apply { setColor(cloud); cornerRadius = dp(28).toFloat() }
            setOnClickListener {  // open in full; again: folded to three lines
                if (maxLines != Int.MAX_VALUE) { maxLines = Int.MAX_VALUE; text = words } else { maxLines = 3; text = words }
            }
            maxWidth = (resources.displayMetrics.widthPixels * 0.72).toInt()
        }
        bubble.post {  // folded: say there is more
            val layout = bubble.layout ?: return@post
            if (layout.lineCount >= 3 && layout.getEllipsisCount(2) > 0) {  // the hint as the 3rd line's end
                bubble.maxLines = 4
                bubble.text = layout.text.subSequence(0, layout.getLineEnd(1)).toString().trimEnd() + "…\n— tap to read all"
            }
        }
        fun dot(size: Int) = View(this).apply {
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(cloud) }
        }.also { it.layoutParams = LinearLayout.LayoutParams(dp(size), dp(size)).apply { marginEnd = dp(3) } }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.BOTTOM
            setPadding(dp(16), dp(4), 0, dp(4))
            addView(dot(6)); addView(dot(10).apply { (layoutParams as LinearLayout.LayoutParams).bottomMargin = dp(6) })
            addView(bubble)
        }
        chat.addView(row, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        chatScroll.post { chatScroll.fullScroll(View.FOCUS_DOWN) }
    }

    /** The stop square: shown while the agent works, by the laptop's word when that agent gives one, else while
     *  your last line there was typed into it (delivered) and it hasn't answered since (the user's wish,
     *  2026-11-16). */
    private fun refreshStop() {
        val last = if (current.isEmpty()) null else Chats.last(this, current)
        // The laptop's word when the agent gives one; else a guess: your last line delivered and no answer yet
        val working = agentWorking ?: (last != null && last.optBoolean("mine") && last.optString("status") == "delivered")
        findViewById<View>(R.id.stop).visibility = if (working) View.VISIBLE else View.GONE
    }

    private fun stopAgent() {
        val session = current.ifEmpty { return }
        findViewById<View>(R.id.stop).visibility = View.GONE
        ask("/crew/stop", JSONObject().put("session", session)) { answer ->
            android.widget.Toast.makeText(this, if (answer.optBoolean("ok")) "Stopped" else answer.optString("note").ifEmpty { "Not stopped" },
                android.widget.Toast.LENGTH_SHORT).show()
            if (!answer.optBoolean("ok")) refreshStop()
        }
    }

    private val bubbleAt = java.util.WeakHashMap<TextView, Long>()  // when each bubble's line was said
    private var lastDay = ""  // the day of the newest line drawn, for the day line between days

    /** The words with the time and, for yours, the ticks small at the end, as messaging apps shows them: ✓ sent,
     *  ✓✓ delivered, ✓✓ in cyan once read. */
    private fun stamp(bubble: TextView, words: String, status: String): CharSequence {
        val at = bubbleAt[bubble] ?: 0L
        val time = if (at > 0) android.text.format.DateFormat.getTimeFormat(this).format(java.util.Date(at)) else ""
        val ticks = when (status) { "" -> ""; "waiting" -> "🕓"; "sent" -> "✓"; else -> "✓✓" }  // 🕓: not yet on the laptop
        val tail = listOf(time, ticks).filter { it.isNotEmpty() }.joinToString(" ")
        if (tail.isEmpty()) { (bubble as? Stamped)?.tail = null; return styled(words) }
        // On your teal bubble the muted grey-blue was too faint (3.6:1): a lighter one there (5.4:1)
        val faint = if (status.isNotEmpty()) 0xFFB4CCD8.toInt() else getColor(R.color.muted)
        val shown = android.text.SpannableStringBuilder(tail).apply {
            setSpan(android.text.style.ForegroundColorSpan(faint), 0, length, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
            if (ticks.isNotEmpty() && status == "read")
                setSpan(android.text.style.ForegroundColorSpan(getColor(R.color.cyan)), length - ticks.length, length,
                    android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
            setSpan(android.text.style.RelativeSizeSpan(0.75f), 0, length, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
        }
        val all = android.text.SpannableStringBuilder(styled(words)).append("  ").append(tail)
        val from = all.length - tail.length
        if (bubble is Stamped) {  // the words keep room for it; the bubble draws it in its corner
            bubble.tail = shown
            all.setSpan(android.text.style.ForegroundColorSpan(android.graphics.Color.TRANSPARENT), from, all.length,
                android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
            all.setSpan(android.text.style.RelativeSizeSpan(0.75f), from, all.length, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
            return all
        }
        return android.text.SpannableStringBuilder(styled(words)).append("  ").append(shown)
    }

    /** A message's time, small, for the corner of a picture, a file or an album. */
    private fun timeLabel(at: Long, mine: Boolean = false) = TextView(this).apply {
        text = if (at > 0) android.text.format.DateFormat.getTimeFormat(this@MainActivity).format(java.util.Date(at)) else ""
        setTextColor(if (mine) 0xFFB4CCD8.toInt() else getColor(R.color.muted)); textSize = 11.5f
        gravity = Gravity.END
    }

    /** An agent's light markdown, shown as it means: **bold** bold and `code` in a fixed-width face, the marks
     *  themselves gone (they showed as asterisks, 2026-11-16). */
    private fun styled(words: String): CharSequence {
        val out = android.text.SpannableStringBuilder()
        val marks = Regex("\\*\\*(.+?)\\*\\*|`([^`]+)`")
        var at = 0
        for (m in marks.findAll(words)) {
            out.append(words, at, m.range.first)
            val start = out.length
            if (m.groupValues[1].isNotEmpty()) {
                out.append(m.groupValues[1])
                out.setSpan(android.text.style.StyleSpan(android.graphics.Typeface.BOLD), start, out.length, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
            } else {
                out.append(m.groupValues[2])
                out.setSpan(android.text.style.TypefaceSpan("monospace"), start, out.length, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
            at = m.range.last + 1
        }
        out.append(words, at, words.length)
        return out
    }

    /** The line under the agent's name: its project, then what is happening ("pc diagnostics · Not on a call"). */
    private fun subtitle(status: String): String {
        val name = names[current] ?: prefs().getString("current_name", "") ?: ""
        val project = name.substringAfter(", in ", "").substringBefore(" · ").replace("_", " ")
        return listOf(project, status).filter { it.isNotEmpty() }.joinToString(" · ")
    }

    /** A call in the chat as a messaging app shows one (the user's example, 2026-11-16): a message bubble on the side of
     *  whoever started it, a round phone mark (red when missed), "Voice call" and how long, and its time. */
    private fun callBubble(line: JSONObject) {
        val call = line.optJSONObject("call") ?: JSONObject().put("dir", "out")  // from before: one of yours
        val mine = call.optString("dir") == "out"
        val outcome = call.optString("outcome")
        val missed = outcome == "missed" || outcome == "declined"
        val title = when (outcome) { "missed" -> "Missed voice call"; "declined" -> "Declined voice call"; else -> "Voice call" }
        val detail = when {
            call.has("seconds") -> Calls.length(call.optLong("seconds"))
            missed -> call.optString("why").take(60)
            else -> line.optString("text").substringAfter("· ", "")
        }
        val at = line.optLong("at")
        val time = if (at > 0) android.text.format.DateFormat.getTimeFormat(this).format(java.util.Date(at)) else ""
        val mark = TextView(this).apply {
            text = if (mine) "↗" else "↙"
            setTextColor(getColor(if (missed) R.color.hang else R.color.text)); textSize = 18f; gravity = Gravity.CENTER
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(getColor(R.color.navy)) }
        }
        val words = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setPadding(dp(12), 0, dp(12), 0)
            addView(TextView(this@MainActivity).apply {
                text = "📞 $title"; setTextColor(getColor(if (missed) R.color.hang else R.color.text)); textSize = 15f
                typeface = android.graphics.Typeface.DEFAULT_BOLD
            })
            if (detail.isNotEmpty()) addView(TextView(this@MainActivity).apply {
                text = detail; setTextColor(getColor(R.color.muted)); textSize = 13f; maxLines = 1
                ellipsize = android.text.TextUtils.TruncateAt.END
            })
        }
        val bubble = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(10), dp(10), dp(12), dp(8))
            setBackgroundResource(if (mine) R.drawable.bubble_mine else R.drawable.bubble_agent)
            addView(mark, LinearLayout.LayoutParams(dp(40), dp(40)))
            addView(words, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            addView(TextView(this@MainActivity).apply {
                text = time; setTextColor(getColor(R.color.muted)); textSize = 11f
            }, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
                gravity = Gravity.BOTTOM
            })
        }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = if (mine) Gravity.END else Gravity.START or Gravity.BOTTOM
            setPadding(0, dp(4), 0, dp(4))
        }
        if (!mine) row.addView(ImageView(this).apply { setImageResource(avatarOf(current)) },
            LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginEnd = dp(8) })
        row.addView(bubble, LinearLayout.LayoutParams((resources.displayMetrics.widthPixels * 0.68).toInt(),
            LinearLayout.LayoutParams.WRAP_CONTENT))
        if (mine) row.addView(ImageView(this).apply { setImageResource(youFace()); tag = "you" },
            LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginStart = dp(8) })
        chat.addView(row, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
    }

    /** "Today", "Yesterday" or the date, centred between the lines of different days. */
    private fun dayLine(at: Long) {
        val day = android.text.format.DateFormat.getMediumDateFormat(this).format(java.util.Date(at))
        if (day == lastDay) return
        lastDay = day
        val label = when {
            android.text.format.DateUtils.isToday(at) -> "Today"
            android.text.format.DateUtils.isToday(at + 86_400_000L) -> "Yesterday"
            else -> day
        }
        chat.addView(TextView(this).apply {
            text = label
            setTextColor(getColor(R.color.muted)); textSize = 12f; gravity = Gravity.CENTER
            setPadding(0, dp(10), 0, dp(6))
        }, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
    }

    private val searchSoon = Runnable { showCrew() }
    private var searched: Pair<String, Map<String, String>>? = null  // a content search's result, for its query

    /** When you last looked at a chat: what its agent said after that is unread (bold in the list). */
    private fun markRead(session: String) = prefs().edit().putLong("seen_$session", System.currentTimeMillis()).apply()

    /** How many of the agent's lines you haven't seen: read from the file only when the newest one is unseen. */
    private fun unread(session: String, last: JSONObject?): Int {
        val seen = prefs().getLong("seen_$session", 0)
        if (last == null || last.optBoolean("mine") || last.optLong("at") <= seen) return 0
        return Chats.load(this, session).takeLastWhile { !it.optBoolean("mine") }
            .count { !it.optBoolean("note") && it.optLong("at") > seen }  // a call's line isn't a message
    }

    private var tab = "chats"  // what the list screen shows: the chats, the call history, or every AI's usage

    /** The call history, newest first: who rang and why, and how it went; calls with their length. Tap one for
     *  that agent's chat. */
    private fun showCalls() {
        val calls = Calls.load(this).asReversed()
        if (calls.isEmpty()) crew.addView(TextView(this).apply {
            text = "No calls yet. Rings and calls show here."
            setTextColor(getColor(R.color.muted)); setPadding(dp(20), dp(24), dp(20), dp(24))
        })
        val ringSeconds = (RingService.RING_SECONDS + 30) * 1000
        for (call in calls) {
            val ring = call.optString("kind") == "ring"
            val at = call.optLong("at")
            // A banner ring nobody answered or declined is still "ringing" in the log: missed, once its time is up
            val outcome = call.optString("outcome").let { if (it == "ringing" && System.currentTimeMillis() - at > ringSeconds) "missed" else it }
            val agent = call.optString("agent")
            val session = call.optString("session")
            val name = if (ring) IncomingActivity.nameOf(agent) else names[session]?.substringBefore(",") ?: "Call"
            val (mark, colour) = when {
                !ring -> "📞" to R.color.text
                outcome == "answered" -> "↙ answered" to R.color.cyan
                outcome == "declined" -> "↙ declined" to R.color.muted
                outcome == "ringing" || outcome == "rang" -> "↙ rang" to R.color.muted
                else -> "↙ $outcome" to R.color.hang
            }
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(20), dp(12), dp(20), dp(12))
                if (session.isNotEmpty()) setOnClickListener { showTab("chats"); open(session) }
            }
            row.addView(ImageView(this).apply {
                setImageResource(if (ring) avatarOf(agent) else if (session.isNotEmpty()) avatarOf(session) else R.drawable.avatar_handsfree)
            }, LinearLayout.LayoutParams(dp(44), dp(44)))
            val words = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(14), 0, 0, 0) }
            val top = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            top.addView(TextView(this).apply {
                text = name
                setTextColor(getColor(if (outcome == "missed") R.color.hang else R.color.text)); textSize = 16f
                typeface = android.graphics.Typeface.DEFAULT_BOLD; maxLines = 1
            }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            top.addView(TextView(this).apply {
                text = android.text.format.DateUtils.formatDateTime(this@MainActivity, at,
                    android.text.format.DateUtils.FORMAT_SHOW_TIME or android.text.format.DateUtils.FORMAT_SHOW_DATE or
                        android.text.format.DateUtils.FORMAT_ABBREV_MONTH)
                setTextColor(getColor(R.color.muted)); textSize = 12f
            })
            words.addView(top)
            val detail = if (ring) listOf(mark, call.optString("why")) else
                listOf("$mark Call" + if (call.has("seconds")) " · ${Calls.length(call.optLong("seconds"))}" else " · going on")
            words.addView(TextView(this).apply {
                text = detail.filter { it.isNotEmpty() }.joinToString(" · ")
                setTextColor(getColor(colour)); textSize = 14f; maxLines = 2
                ellipsize = android.text.TextUtils.TruncateAt.END
            })
            row.addView(words, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            crew.addView(row)
        }
    }

    /** The bottom bar: Chats, Calls or Usage, the one showing lit. */
    private fun showTab(which: String) {
        // Slid in from the side it lies on (Usage left, Calls right), as a pager does (the user's catch, 2026-11-17)
        val from = tabs.indexOf(tab)
        val to = tabs.indexOf(which)
        if (from != to && from >= 0 && to >= 0) refresh.apply {
            translationX = (if (to > from) 1 else -1) * width / 3f
            alpha = 0.4f
            animate().translationX(0f).alpha(1f).setDuration(220)
                .setInterpolator(android.view.animation.DecelerateInterpolator(1.5f)).start()
        }
        tab = which
        for ((id, name) in listOf(R.id.tabChats to "chats", R.id.tabCalls to "calls", R.id.tabUsage to "usage"))
            findViewById<TextView>(id).setTextColor(getColor(if (name == which) R.color.cyan else R.color.muted))
        // The search pill (with the sort in it) and the new-chat button belong to the chats
        for (view in listOf(findViewById<View>(R.id.search).parent as View, findViewById(R.id.compose)))
            view.visibility = if (which == "chats") View.VISIBLE else View.GONE
        showCrew()
    }

    /** Every AI on the laptop at once (the user's wish, 2026-11-17): a card each, its plan's limits as bars, with
     *  when each resets. Asked of the laptop each time the tab is shown (it keeps Cursor's for ten minutes). */
    private fun showUsage(fresh: Boolean = false) {
        val draw = UsageDraw(this)
        val waiting = TextView(this).apply {
            text = if (fresh) "Asking every AI again…" else "Asking the laptop…"
            setTextColor(getColor(R.color.muted)); setPadding(dp(20), dp(24), dp(20), dp(24))
        }
        if (fresh) crew.removeAllViews()
        crew.addView(waiting)
        ask("/usage" + if (fresh) "?fresh=1" else "", null) { answer ->
            refresh.isRefreshing = false
            if (tab != "usage") return@ask
            crew.removeAllViews()
            val agents = answer.optJSONArray("agents")
            if (agents == null || agents.length() == 0) {
                crew.addView(waiting.apply { text = answer.optString("note").ifEmpty { "No usage reported yet." } })
                return@ask
            }
            for (i in 0 until agents.length()) {
                val one = agents.getJSONObject(i)
                val agent = one.optString("agent")
                val card = LinearLayout(this).apply {
                    orientation = LinearLayout.VERTICAL
                    background = android.graphics.drawable.GradientDrawable().apply {
                        setColor(getColor(R.color.surface)); cornerRadius = dp(18).toFloat()
                    }
                    setPadding(dp(18), dp(16), dp(18), dp(16))
                }
                card.addView(LinearLayout(this).apply {
                    orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                    addView(ImageView(this@MainActivity).apply { setImageResource(avatarOf(agent)) }, LinearLayout.LayoutParams(dp(36), dp(36)))
                    addView(TextView(this@MainActivity).apply {
                        text = IncomingActivity.nameOf(agent); setTextColor(getColor(R.color.text)); textSize = 18f
                        typeface = android.graphics.Typeface.DEFAULT_BOLD; setPadding(dp(12), 0, 0, 0)
                    })
                })
                draw.limits(card, one.optJSONArray("limits"))
                draw.notes(card, one.optJSONArray("notes"))
                crew.addView(card, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT).apply { setMargins(dp(14), dp(10), dp(14), dp(4)) })
            }
        }
    }

    private fun sortMode() = prefs().getString("sort", "recent") ?: "recent"

    private fun pins(): Set<String> = prefs().getStringSet("pinned", emptySet())?.toSet() ?: emptySet()

    private fun setPinned(session: String, on: Boolean) {
        val next = pins().toMutableSet()
        if (on) next.add(session) else next.remove(session)
        prefs().edit().putStringSet("pinned", next).apply()
    }

    /** A chat in the list: henchman, project, the agent's name and the last line (or the line a search found). */
    private fun chatRow(session: String, agent: String, project: String, last: JSONObject?, found: String): View {
        val unread = unread(session, last)
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(20), dp(12), dp(20), dp(12))
            background = getDrawable(if (session == current) R.drawable.row_current else android.R.color.transparent)
            setOnClickListener { open(session) }
            setOnLongClickListener { chatMenu(session, project, agent); true }
        }
        row.addView(ImageView(this).apply { setImageResource(avatarOf(session)) }, LinearLayout.LayoutParams(dp(52), dp(52)))
        val words = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(14), 0, 0, 0) }
        val top = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        top.addView(TextView(this).apply {
            text = (if (session in pins()) "📌 " else "") + project.replace("_", " ")
            setTextColor(getColor(R.color.text)); textSize = 17f; typeface = android.graphics.Typeface.DEFAULT_BOLD
            maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END
        }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        top.addView(TextView(this).apply {
            text = last?.optLong("at")?.takeIf { it > 0 }?.let { android.text.format.DateUtils.getRelativeTimeSpanString(it).toString() } ?: ""
            setTextColor(getColor(if (unread > 0) R.color.cyan else R.color.muted)); textSize = 12f
        })
        words.addView(top)
        val bottom = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        TextView(this).apply {
            // A workflow running in it says so instead of the last line (the design review, 2026-11-17)
            val flow = flows[session]?.takeIf { found.isEmpty() }
            val line = found.ifEmpty {
                flow?.let { f -> "⚙ ${f.optString("name")} · " + (if (f.optInt("phases") > 0) "${f.optInt("at").coerceAtLeast(1)}/${f.optInt("phases")} · " else "") +
                    "${f.optInt("done")}/${f.optInt("total")} done" }
                    ?: last?.let { (if (it.optBoolean("mine")) "You: " else "") + it.optString("text").ifEmpty { "📎 file" } } ?: ""
            }
            text = listOf(agent, line.replace("\n", " ")).filter { it.isNotEmpty() }.joinToString(" · ")
            setTextColor(getColor(when { found.isNotEmpty() || flow != null -> R.color.cyan; unread > 0 -> R.color.text; else -> R.color.muted }))
            textSize = 14f; maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
            if (unread > 0) typeface = android.graphics.Typeface.DEFAULT_BOLD  // unread: something to look at
        }.let { line -> bottom.addView(line, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)) }
        if (unread > 0) bottom.addView(TextView(this).apply {  // the count, as a messaging app shows it
            text = if (unread > 99) "99+" else unread.toString()
            setTextColor(getColor(R.color.navy)); textSize = 12f; typeface = android.graphics.Typeface.DEFAULT_BOLD
            gravity = Gravity.CENTER; minWidth = dp(22); setPadding(dp(6), dp(1), dp(6), dp(1))
            background = GradientDrawable().apply { setColor(getColor(R.color.cyan)); cornerRadius = dp(11).toFloat() }
        }, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { marginStart = dp(8) })
        words.addView(bottom)
        row.addView(words, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        return row
    }

    /** One screen at a time: the chat list, a chat, or settings. */
    private fun show(screen: View) {
        BottomPanel.closeAll()  // a menu belongs to the screen it opened on, including one opened on top of another
        if (screen !== chatScreen) stopSelecting()  // and a selection to the chat it was made in
        (shownUndo?.parent as? android.view.ViewGroup)?.removeView(shownUndo)
        for (s in listOf(listScreen, chatScreen, settingsScreen)) s.visibility = if (s === screen) View.VISIBLE else View.GONE
        if (screen === listScreen) { showCrew(); refreshCrewQuietly(); refreshAway() }
        showCallBar()
        // A chat opens at its newest message, as a messaging app's does (it opened at the oldest, 2026-11-16)
        if (screen === chatScreen) chatScroll.post { chatScroll.fullScroll(View.FOCUS_DOWN) }
        main.removeCallbacks(thoughtsPoll)
        if (screen === chatScreen) { thoughtsOf = ""; main.post(thoughtsPoll) }
        main.removeCallbacks(planPoll)
        if (screen === chatScreen) main.post(planPoll)
        main.removeCallbacks(workingPoll)
        if (screen === chatScreen) { workingOf = ""; main.post(workingPoll) } else stopWorkingBubble()
        Messages.viewing = if (screen === chatScreen) current else ""
        if (screen !== chatScreen) { sentBubbles.clear(); myBubbles.clear() }  // only bubbles on screen are kept
    }

    /** Into an agent's chat from the list: on a call the laptop switches to it (and says so); off one, its chat
     *  is shown and texts go to it. */
    private fun open(session: String) {
        if (session != current) {  // a chat, not the call: on a call this doesn't switch it (the call bar leads back)
            stopSelecting()
            current = session
            prefs().edit().putString("current", current).putString("current_name", names[current] ?: "").apply()
            showHistory()
        }
        showConnected(onCall())
        showCrew()
        show(chatScreen)
        if (current.isNotEmpty()) { Messages.seen(this, current); markRead(current) }  // opened: read
        showModel(session)
        commandsAsked.remove(session)  // the "/" list, fresh for this chat
        loadCommands(session)
        loadPlan(session)
    }

    /** A model's name short enough for the top bar: "Claude Opus 5.5 300K Medium" is "Opus 5.5", "gpt-5.6-sol xhigh"
     *  is "gpt-5.6-sol" (the chip pushed the call button off the bar, 2026-11-16). */
    private fun shortModel(name: String): String {
        val words = name.split(" ").filter { it.isNotEmpty() && it != "·" }  // the laptop's own " · " parts: not words
            .takeWhile { !Regex("(?i)\\d+[KM]|low|medium|high|xhigh|extra|max|fast|thinking|\\(.*").matches(it) }
        val short = words.dropWhile { it.equals("Claude", ignoreCase = true) && words.size > 1 }.joinToString(" ").ifEmpty { name }
        // The effort, short, when the name has one: "Grok 4.7 Extra High" is "Grok 4.7 · xhigh" (showing only the
        // family hid that a chat ran on extra high, 2026-11-16)
        val rest = name.lowercase()
        val effort = when {
            "extra high" in rest || "xhigh" in rest -> "xhigh"
            Regex("\\bhigh\\b").containsMatchIn(rest) -> "high"
            Regex("\\bmedium\\b").containsMatchIn(rest) -> "med"
            Regex("\\blow\\b").containsMatchIn(rest) -> "low"
            else -> ""
        }
        return if (effort.isEmpty()) short else "$short · $effort"
    }

    /** The chat's model chip: the model the agent is using now, as the laptop sees it (the user's wish,
     *  2026-11-16); until it answers, the one last switched to from here. */
    private fun showModel(session: String) {
        val chip = findViewById<TextView>(R.id.modelChip)
        chip.text = prefs().getString("model_$session", null)?.let { "✦ " + shortModel(it) } ?: "✦ Model"
        if (session.isEmpty() || Files.origin(this) == null) return
        ask("/crew/models?only=now&session=" + android.net.Uri.encode(session), null) { answer ->
            val now = answer.optString("label").ifEmpty { answer.optString("now") }  // model · effort · context
            if (now.isNotEmpty() && session == current) {
                chip.text = "✦ " + shortModel(now)  // family and effort; context stays in the sheet
                chip.contentDescription = "Model: $now. Tap to switch."
                prefs().edit().putString("model_$session", now).apply()
            }
            showPlan(session, answer.optString("mode"))
        }
    }

    /** Plan mode, next to the model: lit while the terminal is planning. A tap enters it (/plan) or leaves it. */
    private fun showPlan(session: String, mode: String) {
        val chip = findViewById<TextView>(R.id.planChip)
        val shows = session.startsWith("claude") || session.startsWith("cursor")
        chip.visibility = if (shows) View.VISIBLE else View.GONE
        if (!shows) return
        val on = mode == "plan"
        planning = on
        if (findViewById<View>(R.id.planRefresh).visibility == View.VISIBLE) showBuildBar(lastPlan)
        // Not a second model pill (the design review): an outlined option while in agent mode, solid cyan in plan mode
        chip.text = if (on) "☰ Plan" else "◇ Agent"
        chip.setTextColor(getColor(if (on) R.color.navy else R.color.muted))
        chip.typeface = if (on) android.graphics.Typeface.create("sans-serif-medium", android.graphics.Typeface.NORMAL) else android.graphics.Typeface.DEFAULT
        chip.background = android.graphics.drawable.GradientDrawable().apply {
            cornerRadius = dp(18).toFloat()
            if (on) setColor(getColor(R.color.cyan)) else { setColor(android.graphics.Color.TRANSPARENT); setStroke(dp(1), getColor(R.color.surface_high)) }
        }
        chip.contentDescription = if (on) "Plan mode on: tap to leave it" else "Agent mode: tap for plan mode"
    }

    /** Chat or Plan, when this chat has a plan document. Plan lists the steps and how many are done. */
    private var planKey = ""

    private fun loadPlan(session: String) {
        if (session != current) return
        planKey = ""
        findViewById<View>(R.id.planBar).visibility = View.GONE
        showChatTab()
        refreshPlan()
    }

    /** While the chat is open, so a plan that appears (or a step that gets marked done) shows without leaving. */
    private val planPoll = object : Runnable {
        override fun run() {
            if (chatScreen.visibility == View.VISIBLE) refreshPlan()
            if (chatScreen.visibility == View.VISIBLE) main.postDelayed(this, 5000)
        }
    }

    private fun refreshPlan() {
        val session = current
        if (session.isEmpty() || Files.origin(this) == null) {
            findViewById<SwipeRefreshLayout>(R.id.planRefresh).isRefreshing = false
            return
        }
        ask("/chat/plan?session=" + android.net.Uri.encode(session), null) { answer ->
            if (session != current) {
                findViewById<SwipeRefreshLayout>(R.id.planRefresh).isRefreshing = false
                return@ask
            }
            val bar = findViewById<View>(R.id.planBar)
            val todos = answer.optJSONArray("todos")
            if (todos == null) {
                if (bar.visibility == View.VISIBLE) {
                    bar.visibility = View.GONE
                    showChatTab()
                }
                findViewById<SwipeRefreshLayout>(R.id.planRefresh).isRefreshing = false
                return@ask
            }
            bar.visibility = View.VISIBLE
            lastPlan = answer
            if (findViewById<View>(R.id.planRefresh).visibility != View.VISIBLE) showPlanSwitch(answer, onPlan = false)
            findViewById<View>(R.id.chatTab).setOnClickListener { showChatTab() }
            findViewById<View>(R.id.planTab).setOnClickListener { showPlanTab(answer) }
            val key = answer.optString("name") + " " + answer.optInt("done") + "/" + answer.optInt("total") + " " + todos
            val showing = findViewById<View>(R.id.planRefresh).visibility == View.VISIBLE
            if (showing && (key != planKey || findViewById<SwipeRefreshLayout>(R.id.planRefresh).isRefreshing)) showPlanTab(answer)
            planKey = key
            findViewById<SwipeRefreshLayout>(R.id.planRefresh).isRefreshing = false
        }
    }

    private var lastPlan: JSONObject? = null

    // --- Away (the user's idea, 2026-11-17; the design review's look) ---------------------------------------------

    private var away = false

    /** You, away or not: the avatar with its headset on or round the neck, a green or grey dot, and a line under
     *  the title while away. The laptop's state is the truth (the tray and /away set it too). */
    private fun showAway(on: Boolean, animate: Boolean = true) {
        val changed = on != away
        away = on
        prefs().edit().putBoolean("away", on).apply()
        val face = findViewById<ImageButton>(R.id.profile)
        val swap = { face.setImageResource(if (on) R.drawable.avatar_you_away else R.drawable.avatar_you) }
        if (animate && changed) face.animate().scaleX(0.9f).scaleY(0.9f).alpha(0f).setDuration(140).withEndAction {
            swap()
            face.animate().scaleX(1f).scaleY(1f).alpha(1f).setDuration(220)
                .setInterpolator(android.view.animation.OvershootInterpolator(2f)).start()
        }.start() else swap()
        findViewById<View>(R.id.presence).apply {
            background = if (on) getDrawable(R.drawable.presence_away)  // a crescent moon while you're away
                else android.graphics.drawable.GradientDrawable().apply {
                    shape = android.graphics.drawable.GradientDrawable.OVAL
                    setColor(getColor(R.color.call_live)); setStroke(dp(2), getColor(R.color.navy))
                }
            layoutParams = layoutParams.apply { width = dp(if (on) 16 else 12); height = width }
        }
        findViewById<TextView>(R.id.awayLine).apply {
            visibility = if (on) View.VISIBLE else View.GONE
            text = android.text.Html.fromHtml("<font color=\"#00E5FF\">Away</font> · agents text you", android.text.Html.FROM_HTML_MODE_COMPACT)
        }
        findViewById<TextView>(R.id.chatAway).apply {
            visibility = if (on) View.VISIBLE else View.GONE
            text = android.text.Html.fromHtml("Away · replies come as texts · <font color=\"#00E5FF\">Back</font>",
                android.text.Html.FROM_HTML_MODE_COMPACT)
            setOnClickListener { if (away) toggleAway(false) }
        }
        refreshYouFaces()
    }

    /** The face beside your own lines: the vacation one while you're away, the headset otherwise. */
    private fun youFace() = if (away) R.drawable.avatar_you_away else R.drawable.avatar_you

    private fun refreshYouFaces() {
        val face = youFace()
        fun walk(group: android.view.ViewGroup) {
            for (i in 0 until group.childCount) {
                val child = group.getChildAt(i)
                if (child is ImageView && child.tag == "you") child.setImageResource(face)
                else if (child is android.view.ViewGroup) walk(child)
            }
        }
        findViewById<android.view.ViewGroup>(R.id.chat)?.let { walk(it) }
    }

    /** Holding your avatar: you, Available or Away, and Settings (the design review, 2026-11-17). */
    private fun youSheet() {
        panel = YouSheet(this, prefs().getString("my_name", "Boss") ?: "Boss", away,
            onAway = { on -> if (on != away) toggleAway(on) }, onSettings = { show(settingsScreen) }).also { it.show() }
    }

    /** The header breathes (sorting moves into the search box) and Settings reads in sections with a line each. */
    private fun tidyHeaderAndSettings() {
        val search = findViewById<EditText>(R.id.search)
        val sort = findViewById<TextView>(R.id.sort)
        (sort.parent as? android.view.ViewGroup)?.removeView(sort)
        val holder = search.parent as android.view.ViewGroup
        val at = holder.indexOfChild(search)
        val params = search.layoutParams
        holder.removeView(search)
        val pill = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            background = android.graphics.drawable.GradientDrawable().apply { cornerRadius = dp(22).toFloat(); setColor(getColor(R.color.surface)) }
            setPadding(dp(4), 0, dp(4), 0)
        }
        search.background = null
        pill.addView(search, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        pill.addView(View(this).apply { setBackgroundColor(getColor(R.color.surface_high)) }, LinearLayout.LayoutParams(dp(1), dp(20)))
        sort.background = null
        sort.setTextColor(getColor(R.color.muted))
        sort.setPadding(dp(12), dp(10), dp(12), dp(10))
        (sort.layoutParams as? android.view.ViewGroup.MarginLayoutParams)?.marginEnd = 0
        pill.addView(sort)
        holder.addView(pill, at, params)

        // Settings: sections, plain names, a line saying what each does
        val soundRow = findViewById<View>(R.id.soundRow)
        val list = soundRow.parent as LinearLayout
        fun section(before: View, title: String) =
            list.addView(UsageDraw(this).label(title).apply { setPadding(dp(20), dp(24), dp(20), dp(6)) }, list.indexOfChild(before))
        fun explain(after: View, words: String) = list.addView(TextView(this).apply {
            text = words; textSize = 13f; setTextColor(getColor(R.color.muted)); setLineSpacing(0f, 1.3f)
            setPadding(dp(20), 0, dp(20), dp(10))
        }, list.indexOfChild(after) + 1)
        val thoughts = findViewById<View>(R.id.thoughtsSwitch).parent as View
        val dance = findViewById<View>(R.id.danceSwitch).parent as View
        val server = findViewById<View>(R.id.serverRow)
        explain(soundRow, "Calls start with it. The call menu changes it for one call.")
        explain(thoughts, "The agents' thought bubbles in the chat.")
        explain(dance, "Avatars move while an agent works. Off: a calmer screen.")
        explain(server, "Your laptop's address on Tailscale: where Henchline reaches your agents.")
        section(soundRow, "CALLS")
        section(thoughts, "CHATS")
        section(server, "LAPTOP")

        fun line(title: String, value: TextView, onClick: () -> Unit) = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(20), dp(12), dp(20), dp(12))
            setOnClickListener { onClick() }
            addView(TextView(this@MainActivity).apply {
                text = title; textSize = 16f; setTextColor(getColor(R.color.text))
            }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            value.textSize = 14f
            value.setTextColor(getColor(R.color.cyan))
            addView(value)
        }
        val stayValue = TextView(this)
        val cutValue = TextView(this)
        fun showDefaults() {
            stayValue.text = if (stayDefault()) "Stay on" else "Check on me"
            cutValue.text = when (cutDefault()) { "in" -> "Cut in"; "wait" -> "Let it wait"; else -> "Follow the agent" }
        }
        showDefaults()
        list.addView(UsageDraw(this).label("VOICES").apply { setPadding(dp(20), dp(24), dp(20), dp(6)) })
        list.addView(line("Agents' voices", TextView(this).apply { text = "Choose"; textSize = 14f; setTextColor(getColor(R.color.cyan)) }) { voices() })
        list.addView(TextView(this).apply {
            text = "Each agent its own, one voice for all, or surprise me. A tap on a voice picks the next one."
            textSize = 13f; setTextColor(getColor(R.color.muted)); setPadding(dp(20), 0, dp(20), dp(10))
        })
        list.addView(UsageDraw(this).label("NEW CALLS").apply { setPadding(dp(20), dp(24), dp(20), dp(6)) })
        list.addView(line("Stay or check", stayValue) {
            prefs().edit().putBoolean("stay_default", !stayDefault()).apply(); showDefaults()
        })
        list.addView(line("Cut in", cutValue) {
            val next = when (cutDefault()) { "auto" -> "in"; "in" -> "wait"; else -> "auto" }
            prefs().edit().putString("cut_default", next).apply(); showDefaults()
        })
        list.addView(TextView(this).apply {
            text = "How the next call starts. The call menu changes the call you're on."
            textSize = 13f; setTextColor(getColor(R.color.muted)); setPadding(dp(20), 0, dp(20), dp(10))
        })
        val version = try { packageManager.getPackageInfo(packageName, 0).versionName ?: "" } catch (_: Exception) { "" }
        list.addView(UsageDraw(this).label("ABOUT").apply { setPadding(dp(20), dp(24), dp(20), dp(6)) })
        list.addView(line("Henchline", TextView(this).apply { text = version; textSize = 14f; setTextColor(getColor(R.color.muted)) }) {
            android.app.AlertDialog.Builder(this).setTitle("Henchline").setMessage("Version $version").setPositiveButton("OK", null).show()
        })
    }

    private fun stayDefault() = prefs().getBoolean("stay_default", true)

    private fun cutDefault() = prefs().getString("cut_default", "auto") ?: "auto"

    /** The saved line for a new call: cut in, let it wait, or follow the agent. */
    private fun sendCutDefault() {
        val service = CallService.Bus.service ?: return
        when (cutDefault()) {
            "in" -> { cutIn = true; service.send(JSONObject().put("cut_in", true).toString()) }
            "wait" -> { cutIn = false; service.send(JSONObject().put("cut_in", false).toString()) }
            else -> { cutIn = null; service.send(JSONObject().put("cut_in", "auto").toString()) }
        }
    }

    /** The voices sheet: the laptop keeps the choice, and plays a short preview. */
    private fun voices() {
        if (Files.origin(this) == null) return
        ask("/voices", null) { answer ->
            if (!answer.has("showing")) {
                android.widget.Toast.makeText(this, answer.optString("note").ifEmpty { "Couldn't reach the laptop." },
                    android.widget.Toast.LENGTH_LONG).show()
                return@ask
            }
            panel = VoicesSheet(this, answer, save = { change, then ->
                ask("/voices", change) { saved ->
                    if (saved.has("showing")) then(saved)
                    else android.widget.Toast.makeText(this, saved.optString("note").ifEmpty { "Couldn't save that." },
                        android.widget.Toast.LENGTH_LONG).show()
                }
            }, preview = { voice ->
                ask("/voices/preview", JSONObject().put("voice", voice)) { heard ->
                    val url = heard.optString("url")
                    if (url.isEmpty()) android.widget.Toast.makeText(this, heard.optString("note").ifEmpty { "Couldn't play that." },
                        android.widget.Toast.LENGTH_LONG).show()
                    else playOrPause(url, ImageButton(this), TextView(this), "Preview")
                }
            }).also { it.show() }
        }
    }

    private fun toggleAway(to: Boolean = !away, undo: Boolean = true) {
        if (onCall()) return android.widget.Toast.makeText(this, "End the call first.", android.widget.Toast.LENGTH_SHORT).show()
        findViewById<View>(R.id.profile).performHapticFeedback(android.view.HapticFeedbackConstants.CONFIRM)
        showAway(to)
        ask("/away", JSONObject().put("on", to)) { answer ->
            if (!answer.has("away")) {  // not reached: as it was
                showAway(!to)
                android.widget.Toast.makeText(this, "Couldn't reach the laptop.", android.widget.Toast.LENGTH_SHORT).show()
                return@ask
            }
            showAway(answer.optBoolean("away"))
            if (to && answer.optBoolean("away") && !prefs().getBoolean("away_card", false)) {
                prefs().edit().putBoolean("away_card", true).apply()
                android.app.AlertDialog.Builder(this)
                    .setTitle("Away")
                    .setMessage("The laptop stops listening. Agents are told, and they text you. Push-to-talk still works.")
                    .setPositiveButton("OK", null)
                    .show()
            }
            if (undo) undoBar(if (to) "You're away. Agents will text you." else "Back. Agents will speak again.") { toggleAway(!to, undo = false) }
        }
    }

    private var shownUndo: View? = null

    /** A bar at the bottom for a few seconds, with Undo; a new one replaces the one showing. */
    private fun undoBar(words: String, undo: () -> Unit) {
        (shownUndo?.parent as? android.view.ViewGroup)?.removeView(shownUndo)
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            background = android.graphics.drawable.GradientDrawable().apply { cornerRadius = dp(14).toFloat(); setColor(getColor(R.color.surface_high)) }
            setPadding(dp(16), dp(12), dp(8), dp(12)); elevation = dp(6).toFloat()
            addView(TextView(this@MainActivity).apply { text = words; setTextColor(getColor(R.color.text)); textSize = 14f },
                LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        }
        val gone = { (bar.parent as? android.view.ViewGroup)?.removeView(bar); if (shownUndo === bar) shownUndo = null }
        shownUndo = bar
        bar.addView(TextView(this).apply {
            text = "UNDO"; setTextColor(getColor(R.color.cyan)); textSize = 14f; typeface = android.graphics.Typeface.DEFAULT_BOLD
            setPadding(dp(12), dp(6), dp(12), dp(6)); setOnClickListener { gone(); undo() }
        })
        addContentView(bar, android.widget.FrameLayout.LayoutParams(android.widget.FrameLayout.LayoutParams.MATCH_PARENT,
            android.widget.FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM).apply { setMargins(dp(16), 0, dp(16), dp(96)) })
        main.postDelayed({ gone() }, 4000)
    }

    /** The laptop says whether you're away (the tray or /away may have changed it). */
    private fun refreshAway() {
        if (Files.origin(this) == null) return
        ask("/away", null) { answer -> if (answer.has("away")) showAway(answer.optBoolean("away")) }
    }

    /** The henchman and "call your crew" fill an empty chat. The Plan tab has its own page, so they stay off it,
     *  even before the chat's lines have loaded. */
    private fun showChatBody() {
        val lines = chat.childCount > (if (workingRow?.parent === chat) 1 else 0)
        val onPlan = findViewById<View>(R.id.planRefresh).visibility == View.VISIBLE
        chatScroll.visibility = if ((onCall() || lines) && !onPlan) View.VISIBLE else View.GONE
        findViewById<View>(R.id.idle).visibility = if (onCall() || lines || onPlan) View.GONE else View.VISIBLE
    }

    private fun showChatTab() {
        findViewById<View>(R.id.planRefresh).visibility = View.GONE
        showChatBody()
        showPlanSwitch(lastPlan, onPlan = false)
    }

    private var planView: PlanView? = null
    private var buildBar: LinearLayout? = null
    private var buildButton: TextView? = null
    private var buildSent = ""  // the plan whose build was asked for, until the plan changes
    private var buildSentAt = 0L  // and when: after 2 min with no step under way, Build can be sent again
    private var planning = false  // the chip's mode: Build this is only for approving a plan

    private fun showPlanTab(plan: JSONObject) {
        findViewById<View>(R.id.planRefresh).visibility = View.VISIBLE
        showChatBody()
        showPlanSwitch(plan, onPlan = true)
        val list = findViewById<LinearLayout>(R.id.planList)
        list.setPadding(0, 0, 0, dp(88))  // room above the sticky button
        (planView ?: PlanView(this).also { planView = it }).fill(list, plan)
        showBuildBar(plan)
    }

    /** "Build this", pinned at the bottom of the Plan tab over a fade (the design review): hidden when every step is
     *  done, "Building · step n of m" while one is in progress, "Sent…" once tapped. */
    private fun showBuildBar(plan: JSONObject?) {
        val holder = findViewById<View>(R.id.planRefresh).parent as android.widget.FrameLayout
        val bar = buildBar ?: LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = android.graphics.drawable.GradientDrawable(android.graphics.drawable.GradientDrawable.Orientation.BOTTOM_TOP,
                intArrayOf(getColor(R.color.navy), getColor(R.color.navy), getColor(R.color.navy) and 0x00FFFFFF))
            setPadding(dp(16), dp(24), dp(16), dp(16))
            val button = TextView(this@MainActivity).apply {
                gravity = Gravity.CENTER; textSize = 16f; typeface = android.graphics.Typeface.DEFAULT_BOLD
            }
            addView(button, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(52)))
            buildButton = button
            holder.addView(this, android.widget.FrameLayout.LayoutParams(android.widget.FrameLayout.LayoutParams.MATCH_PARENT,
                android.widget.FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))
        }.also { buildBar = it }
        val todos = plan?.optJSONArray("todos")
        val steps = (0 until (todos?.length() ?: 0)).map { todos!!.getJSONObject(it) }
        val done = steps.count { it.optString("status") == "completed" }
        val going = steps.indexOfFirst { it.optString("status") == "in_progress" }
        val showing = plan != null && planning && findViewById<View>(R.id.planRefresh).visibility == View.VISIBLE
        bar.visibility = if (showing && (steps.isEmpty() || done < steps.size)) View.VISIBLE else View.GONE
        val button = buildButton ?: return
        fun pill(fill: Int, ink: Int) {
            button.background = android.graphics.drawable.GradientDrawable().apply { cornerRadius = dp(26).toFloat(); setColor(fill) }
            button.setTextColor(ink)
        }
        val key = plan?.optString("file").orEmpty()
        when {
            going >= 0 -> { button.text = "Building · step ${going + 1} of ${steps.size}"; pill(getColor(R.color.surface_high), getColor(R.color.muted))
                button.setOnClickListener(null); button.isClickable = false }
            buildSent == key && key.isNotEmpty() && System.currentTimeMillis() - buildSentAt < 120_000 -> { button.text = "Sent…"; pill(getColor(R.color.surface_high), getColor(R.color.cyan))
                button.setOnClickListener(null); button.isClickable = false }
            else -> {
                button.text = "▶  Build this"; pill(getColor(R.color.cyan), getColor(R.color.navy))
                button.setOnClickListener {
                    button.performHapticFeedback(android.view.HapticFeedbackConstants.CONFIRM)
                    buildSent = key
                    buildSentAt = System.currentTimeMillis()
                    showBuildBar(plan)
                    ask("/chat/plan/build", JSONObject().put("session", current)) { answer ->
                        answer.optString("note").takeIf { it.isNotEmpty() }?.let {
                            buildSent = ""; showBuildBar(plan)
                            android.widget.Toast.makeText(this@MainActivity, it, android.widget.Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
        }
    }

    /** Chat | Plan as a two-part switch (the design review): a surface track, the chosen half on a lighter thumb,
     *  the plan's progress as a badge on its half. */
    private fun showPlanSwitch(plan: JSONObject?, onPlan: Boolean) {
        val chat = findViewById<TextView>(R.id.chatTab)
        val tab = findViewById<TextView>(R.id.planTab)
        val bar = findViewById<LinearLayout>(R.id.planBar)
        bar.background = android.graphics.drawable.GradientDrawable().apply { cornerRadius = dp(20).toFloat(); setColor(getColor(R.color.surface)) }
        bar.setPadding(dp(3), dp(3), dp(3), dp(3))
        (bar.layoutParams as? android.view.ViewGroup.MarginLayoutParams)?.let { it.marginStart = dp(16); it.marginEnd = dp(16); it.topMargin = dp(4); it.bottomMargin = dp(8) }
        for ((view, on) in listOf(chat to !onPlan, tab to onPlan)) {
            view.layoutParams = LinearLayout.LayoutParams(0, dp(34), 1f)
            view.gravity = Gravity.CENTER
            view.setTextColor(getColor(if (on) R.color.text else R.color.muted))
            view.typeface = if (on) android.graphics.Typeface.create("sans-serif-medium", android.graphics.Typeface.NORMAL) else android.graphics.Typeface.DEFAULT
            view.background = if (on) android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = dp(17).toFloat(); setColor(getColor(R.color.surface_high)) } else null
        }
        val total = plan?.optInt("total") ?: 0
        val done = plan?.optInt("done") ?: 0
        tab.text = if (total > 0) android.text.Html.fromHtml("Plan &nbsp;<font color=\"#00E5FF\"><b>" +
            (if (done == total) "✓" else "$done/$total") + "</b></font>", android.text.Html.FROM_HTML_MODE_COMPACT) else "Plan"
        tab.contentDescription = if (total > 0) "Plan, $done of $total steps done" else "Plan"
        if (!onPlan) buildBar?.visibility = View.GONE
    }

    private fun togglePlan() {
        val session = current.ifEmpty { return }
        if (Files.origin(this) == null) return
        ask("/crew/plan", JSONObject().put("session", session)) { answer ->
            answer.optString("note").takeIf { it.isNotEmpty() }?.let {
                android.widget.Toast.makeText(this, it, android.widget.Toast.LENGTH_LONG).show()
            }
            answer.optString("mode").takeIf { it.isNotEmpty() }?.let { showPlan(session, it) }
        }
    }

    private var crewAsked = 0L

    /** The list coming into view: the laptop's crew asked for again, quietly, at most every half minute, so agents
     *  ended on the laptop leave the list without a pull (one stayed listed after it was killed, 2026-11-16). */
    private fun refreshCrewQuietly() {
        if (System.currentTimeMillis() - crewAsked < 30_000 || Files.origin(this) == null) return
        crewAsked = System.currentTimeMillis()
        ask("/crew", JSONObject()) { answer ->
            if (answer.has("agents")) { takeCrew(answer); if (listScreen.visibility == View.VISIBLE) showCrew() }
        }
    }

    /** Pull down on the list: the laptop looks for every agent running (as "switch me to" does) and lists them. */
    private fun findCrew() {
        val origin = Files.origin(this) ?: run { refresh.isRefreshing = false; return say("", "Set the laptop's address in Settings first.", note = true).let {} }
        Thread {
            val found = try {
                Files.client.newCall(okhttp3.Request.Builder().url("$origin/crew").header("Origin", origin)
                    .post(ByteArray(0).toRequestBody(null)).build())
                    .execute().use { org.json.JSONObject(it.body?.string() ?: "{}") }
            } catch (_: Exception) { null }
            main.post {
                refresh.isRefreshing = false
                if (found == null) {
                    android.widget.Toast.makeText(this, "Couldn't reach the laptop (is Tailscale on?)", android.widget.Toast.LENGTH_SHORT).show()
                    return@post
                }
                if (!found.has("agents")) {
                    android.widget.Toast.makeText(this, "Couldn't reach the laptop (is Tailscale on?)", android.widget.Toast.LENGTH_SHORT).show()
                    return@post
                }
                takeCrew(found)
                showCrew()
            }
        }.start()
    }

    /** A request to the laptop off the main thread: the JSON answer (with "note" saying why not), on the main thread. */
    private fun ask(path: String, body: JSONObject?, then: (JSONObject) -> Unit) {
        val origin = Files.origin(this) ?: return say("", "Set the laptop's address in Settings first.", note = true).let {}
        Thread {
            val answer = try {
                val request = okhttp3.Request.Builder().url("$origin$path").header("Origin", origin)
                if (body != null) request.post(body.toString().toRequestBody("application/json".toMediaType()))
                Files.client.newCall(request.build()).execute().use { JSONObject(it.body?.string()?.ifEmpty { "{}" } ?: "{}") }
            } catch (_: Exception) { JSONObject().put("note", "Couldn't reach the laptop (is Tailscale on?)") }
            main.post { then(answer) }
        }.start()
    }

    /** The crew as the laptop listed it: names, and which agents were started from here (those can be ended). */
    /** An agent started from a new chat speaks first under its own key, not the one it was found under: its chat
     *  here, with the open chat and the call, follows it (the user's catch, 2026-11-16: the reply came in another
     *  chat). */
    private fun followRenames(answer: JSONObject) {
        val moves = answer.optJSONObject("renamed") ?: return
        var moved = false
        for (old in moves.keys()) {
            val new = moves.optString(old)
            if (new.isEmpty() || new == old) continue
            if (Chats.merge(this, old, new)) { Media.rename(this, old, new); moved = true }
            if (current == old) { current = new; prefs().edit().putString("current", new).apply(); moved = true }
            if (callSession == old) callSession = new
        }
        if (moved && chatScreen.visibility == View.VISIBLE) showHistory()
    }

    private fun takeCrew(answer: JSONObject) {
        followRenames(answer)
        val found = answer.optJSONArray("agents") ?: return
        names = (0 until found.length()).associate { found.getJSONObject(it).getString("session") to found.getJSONObject(it).getString("name") }
        flows = (0 until found.length()).mapNotNull { i -> found.getJSONObject(i).let { a ->
            a.optJSONObject("workflow")?.let { a.getString("session") to it } } }.toMap()
        prefs().edit().putString("crew", found.toString()).apply()
    }

    private var flows: Map<String, JSONObject> = emptyMap()  // a workflow running in a chat, by session: its list line

    private fun startedHere(session: String): Boolean = try {
        val crew = org.json.JSONArray(prefs().getString("crew", "[]"))
        (0 until crew.length()).any { crew.getJSONObject(it).optString("session") == session && crew.getJSONObject(it).optBoolean("spawned") }
    } catch (_: Exception) { false }

    private var panel: BottomPanel? = null  // the new-chat or long-press sheet, while it is up

    /** New chat (the user's wish, 2026-11-16): the laptop's folders (those its Claude trusts, newest first, or a
     *  typed path) and agents, in a bottom sheet; the agent starts in its own tmux window there and its chat opens. */
    private fun newChat() {
        ask("/crew/folders", null) { answer ->
            val folders = answer.optJSONArray("folders")
            val agents = answer.optJSONArray("agents")
            if (folders == null || agents == null || agents.length() == 0) {
                android.widget.Toast.makeText(this, answer.optString("note").ifEmpty { "No agent to start on the laptop (it needs tmux)." },
                    android.widget.Toast.LENGTH_LONG).show()
                return@ask
            }
            panel = NewChatSheet(this, (0 until folders.length()).map { folders.getJSONObject(it) },
                (0 until agents.length()).map { agents.getString(it) },
                loadModels = { agent, then -> ask("/crew/models?agent=$agent", null) { answer -> then(answer) } },
                cursorChats = { cursorChats() }
            ) { folder, agent, model, params, typed, done ->
                ask("/crew/new", JSONObject().put("folder", folder).put("agent", agent).put("model", model)
                    .put("params", JSONObject(params as Map<*, *>)).put("typed", typed)) { started ->
                    val session = started.optString("session")
                    if (session.isEmpty()) return@ask done(started.optString("note").ifEmpty { "It didn't start." })
                    done(null)
                    takeCrew(started)
                    open(session)
                }
            }.also { it.show() }
        }
    }

    /** /usage and /context: the bars, and the same readout typed into the agent (not as the slash command). */
    private fun usage(which: String = "usage") {
        val session = current.ifEmpty { return }
        ask("/crew/usage?session=" + android.net.Uri.encode(session), null) { answer ->
            if (!answer.has("agent")) return@ask android.widget.Toast.makeText(this,
                answer.optString("note").ifEmpty { "No usage to show." }, android.widget.Toast.LENGTH_LONG).show()
            panel = UsageSheet(this, answer).also { it.show() }
            ask("/crew/usage/tell", JSONObject().put("session", session).put("which", which)) { told ->
                told.optString("note").takeIf { it.isNotEmpty() }?.let {
                    android.widget.Toast.makeText(this, it, android.widget.Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    /** Any Cursor chat on the laptop, quiet ones too: picked, it opens in Cursor and its chat here. */
    private fun cursorChats() {
        ask("/crew/cursor-chats", null) { answer ->
            val list = answer.optJSONArray("chats")
                ?: return@ask android.widget.Toast.makeText(this, answer.optString("note").ifEmpty { "The laptop didn't list them." },
                    android.widget.Toast.LENGTH_LONG).show()
            panel = CursorChatsSheet(this, (0 until list.length()).map { list.getJSONObject(it) }) { session, done ->
                ask("/crew/cursor-chats/open", JSONObject().put("session", session)) { opened ->
                    val got = opened.optString("session")
                    if (got.isEmpty()) return@ask done(opened.optString("note").ifEmpty { "It didn't open." })
                    done(null)
                    takeCrew(opened)
                    open(got)
                }
            }.also { it.show() }
        }
    }

    private fun modelsOf(answer: JSONObject): List<JSONObject> =
        answer.optJSONArray("models")?.let { list -> (0 until list.length()).map { list.getJSONObject(it) } } ?: emptyList()

    /** The model chip in a chat's top bar: the agent's models, and the pick typed into it as its own /model. */
    private fun switchModel() {
        val session = current.ifEmpty { return }
        ask("/crew/models?session=" + android.net.Uri.encode(session), null) { answer ->
            val toast = { words: String -> android.widget.Toast.makeText(this, words, android.widget.Toast.LENGTH_LONG).show() }
            if (answer.has("note")) return@ask toast(answer.optString("note"))
            if (!answer.optBoolean("switch")) return@ask toast(when {
                answer.optString("agent") == "codex" -> "Codex switches models only in its own picker: start a new chat with the model."
                session.matches(Regex("cursor-[0-9a-f-]{36}")) -> "On ${answer.optString("now").ifEmpty { "its own model" }}. Cursor only lets you change an IDE chat's model in Cursor itself."
                else -> "This agent can't switch models from the phone."
            })
            // A Cursor IDE chat: Cursor's own menu (model, effort, context, fast)
            answer.optJSONArray("menu")?.let { menu ->
                val current = answer.optJSONObject("current") ?: JSONObject()
                val settings = mutableMapOf<String, String>()
                current.optJSONObject("params")?.let { p -> p.keys().forEach { k -> settings[k] = p.optString(k) } }
                panel = CursorModelMenu(this, (0 until menu.length()).map { menu.getJSONObject(it) },
                    current.optString("model"), settings) { model, params, done ->
                    val list = org.json.JSONArray(params.map { (k, v) -> JSONObject().put("id", k).put("value", v) })
                    ask("/crew/model", JSONObject().put("session", session).put("model", model).put("params", list)) { result ->
                        done(if (result.optBoolean("ok")) result.optString("note").ifEmpty { null }
                             else result.optString("note").ifEmpty { "Didn't switch." })
                        showModel(session)
                    }
                }.also { it.show() }
                return@ask
            }
            val now = answer.optString("now")
            panel = ModelPicker(this, if (now.isNotEmpty()) "Now on $now" else "Switch model", modelsOf(answer), withDefault = false) { id, name ->
                ask("/crew/model", JSONObject().put("session", session).put("model", id)) { done ->
                    if (done.optBoolean("ok")) {
                        say("", "✦ Switched to $name", note = true)
                        prefs().edit().putString("model_$session", name).apply()
                        showModel(session)  // what it really uses now (Cursor may pick a near match)
                        done.optString("note").takeIf { it.isNotEmpty() }?.let(toast)  // e.g. press Enter in Konsole
                    } else toast(done.optString("note").ifEmpty { "Didn't switch." })
                }
            }.also { it.show() }
        }
    }

    /** Long-press on a chat: clear it on this phone, or (for an agent started from here) end that agent, asked again
     *  first. Agents the user started on the laptop are never ended from the phone. */
    /** Rows drawn since `before` (a picture, an album, a call): held, the message's menu. Their own taps (a picture
     *  opening whole) stay; their holds open the menu. */
    private fun menuOn(before: Int, session: String, ats: List<Long>) {
        if (ats.none { it > 0 }) return
        val hold = View.OnLongClickListener { messageMenu(session, ats); true }
        fun on(view: View) {
            if (view.isClickable) view.setOnLongClickListener(hold)
            (view as? android.view.ViewGroup)?.let { group -> for (k in 0 until group.childCount) on(group.getChildAt(k)) }
        }
        for (k in before until chat.childCount) chat.getChildAt(k).let { row ->
            row.setTag(R.id.ats, ats); row.setOnLongClickListener(hold); on(row) }
    }

    /** Held on a message: copy it, open its file elsewhere, or delete it from this phone, with the files it saved
     *  if no other message has them (the user's wish, 2026-11-16). */
    private fun messageMenu(session: String, ats: List<Long>, words: String? = null) {
        if (selected != null) return toggleSelected(ats)
        val kept = Chats.load(this, session)
        // A line shown as it arrived (kept by the call, not this screen) has the time it was kept: found by its words
        val lines = kept.filter { it.optLong("at") in ats }.ifEmpty {
            words?.let { w -> kept.lastOrNull { it.optString("text") == w } }?.let { listOf(it) } ?: emptyList() }
        if (lines.isEmpty()) return
        val ats = lines.map { it.optLong("at") }
        val first = lines.first()
        val file = first.optJSONObject("file")
        val mine = first.optBoolean("mine")
        val saved = Media.onlyIn(this, session, Media.savedFor(this, lines), except = ats.toSet())
        val title = when {
            lines.size > 1 -> "${lines.size} pictures"
            file != null -> file.optString("name", "File")
            first.optBoolean("note") -> "Call"
            mine -> "Your message"
            else -> "${IncomingActivity.nameOf(first.optString("from").lowercase())}'s message"
        }
        val time = android.text.format.DateFormat.getMediumDateFormat(this).format(java.util.Date(first.optLong("at"))) + ", " +
            android.text.format.DateFormat.getTimeFormat(this).format(java.util.Date(first.optLong("at")))
        val words = lines.mapNotNull { it.optString("text").takeIf { t -> t.isNotEmpty() && !it.optBoolean("note") } }
            .joinToString("\n").ifEmpty { null }
        val open: (() -> Unit)? = file?.let { f -> {
            Files.open(this, f.getString("url"), f.optString("name", "file"), f.optString("type", "application/octet-stream"),
                f.optLong("size"), session) { say("", it, note = true) }
        } }
        panel = MessageSheet(this, title, time, words, open, mine, saved.size,
            onSelect = { startSelecting(ats) }) { alsoFiles ->
            val removed = Chats.remove(this, session, ats.toSet())
            Media.dropPreviews(this, removed)
            if (alsoFiles) deleted(saved, Media.delete(this, saved))
            if (session == current) showHistory()
            showCrew()
        }.also { it.show() }
    }

    // --- Several messages at once (the user's wish, 2026-11-17): delete or copy them together; a reply stays one ---

    private var selected: MutableSet<Long>? = null  // the kept lines picked, by their times; null: not selecting
    private var selectBar: View? = null
    private var selectCount: TextView? = null

    private fun startSelecting(ats: List<Long>) {
        selected = mutableSetOf()
        hideKeyboard(this)
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(getColor(R.color.surface_high))
            setPadding(dp(8), findViewById<View>(R.id.statusSpace).height + dp(6), dp(8), dp(10))
            elevation = dp(8).toFloat()
            isClickable = true
        }
        fun button(label: String, then: () -> Unit) = TextView(this).apply {
            text = label; setTextColor(getColor(R.color.text)); textSize = 15f
            setPadding(dp(14), dp(10), dp(14), dp(10)); setOnClickListener { then() }
            background = getDrawable(android.R.drawable.list_selector_background)
        }
        bar.addView(button("✕") { stopSelecting() })
        selectCount = TextView(this).apply { setTextColor(getColor(R.color.text)); textSize = 17f; typeface = android.graphics.Typeface.DEFAULT_BOLD }
        bar.addView(selectCount, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        bar.addView(button("📋 Copy") { copySelected() })
        bar.addView(button("↗ Share") { shareSelected() })
        bar.addView(button("🗑 Delete") { deleteSelected() })
        addContentView(bar, android.widget.FrameLayout.LayoutParams(android.widget.FrameLayout.LayoutParams.MATCH_PARENT,
            android.widget.FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.TOP))
        selectBar = bar
        toggleSelected(ats)
    }

    private fun stopSelecting() {
        if (selected == null) return
        selected = null
        (selectBar?.parent as? android.view.ViewGroup)?.removeView(selectBar)
        selectBar = null
        for (k in 0 until chat.childCount) chat.getChildAt(k).let { if (it.getTag(R.id.ats) != null) it.background = null }
    }

    /** Picked, or unpicked; each row showing a picked line is tinted. None left: selecting ends. */
    private fun toggleSelected(ats: List<Long>) {
        val picked = selected ?: return
        if (ats.isEmpty()) return
        if (ats.all { it in picked }) picked.removeAll(ats.toSet()) else picked.addAll(ats)
        tintSelected()
    }

    /** The rows showing a picked line, tinted; picks no longer in the chat dropped (after a redraw too). */
    private fun tintSelected() {
        val picked = selected ?: return
        val shown = mutableSetOf<Long>()
        for (k in 0 until chat.childCount) {
            @Suppress("UNCHECKED_CAST") (chat.getChildAt(k).getTag(R.id.ats) as? List<Long>)?.let { shown.addAll(it) }
        }
        picked.retainAll(shown)
        if (picked.isEmpty()) return stopSelecting()
        for (k in 0 until chat.childCount) {
            val row = chat.getChildAt(k)
            @Suppress("UNCHECKED_CAST") val rowAts = row.getTag(R.id.ats) as? List<Long> ?: continue
            row.setBackgroundColor(if (rowAts.any { it in picked }) (getColor(R.color.cyan) and 0x33FFFFFF) else android.graphics.Color.TRANSPARENT)
        }
        selectCount?.text = "${picked.size} selected"
    }

    private fun selectedLines(): List<JSONObject> {
        val picked = selected ?: return emptyList()
        return Chats.load(this, current).filter { it.optLong("at") in picked }
    }

    private fun copySelected() {
        val words = selectedLines().mapNotNull { line ->
            line.optString("text").takeIf { it.isNotEmpty() && !line.optBoolean("note") }
                ?: line.optJSONObject("file")?.let { "📎 " + it.optString("name", "file") }
        }.joinToString("\n\n")
        if (words.isEmpty()) return
        getSystemService(android.content.ClipboardManager::class.java)
            .setPrimaryClip(android.content.ClipData.newPlainText("Henchline", words))
        android.widget.Toast.makeText(this, "Copied", android.widget.Toast.LENGTH_SHORT).show()
        stopSelecting()
    }

    /** To another app, the pictures and files themselves with the words (the user's wish, 2026-11-17: copying
     *  carries only text): files from the laptop saved to Downloads/Henchline first; your own pictures are in your
     *  gallery already, so their words come along without them. */
    private fun shareSelected() {
        val lines = selectedLines()
        if (lines.isEmpty()) return stopSelecting()
        val session = current
        android.widget.Toast.makeText(this, "Getting them ready…", android.widget.Toast.LENGTH_SHORT).show()
        Thread {
            val uris = ArrayList<android.net.Uri>()
            val types = mutableSetOf<String>()
            var missed = 0
            for (line in lines) {
                val f = line.optJSONObject("file") ?: continue
                try {
                    uris.add(Files.keep(this, f.getString("url"), f.optString("name", "file"),
                        f.optString("type", "application/octet-stream"), f.optLong("size"), session))
                    types.add(f.optString("type", "application/octet-stream"))
                } catch (_: Exception) { missed++ }
            }
            val words = lines.mapNotNull { line ->
                line.optString("text").takeIf { it.isNotEmpty() && !line.optBoolean("note") }
            }.joinToString("\n\n")
            main.post {
                val send = if (uris.isEmpty()) Intent(Intent.ACTION_SEND).setType("text/plain")
                    else Intent(Intent.ACTION_SEND_MULTIPLE).putParcelableArrayListExtra(Intent.EXTRA_STREAM, uris)
                        .setType(if (types.all { it.startsWith("image/") }) "image/*" else "*/*")
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                if (words.isNotEmpty()) send.putExtra(Intent.EXTRA_TEXT, words)
                if (uris.isEmpty() && words.isEmpty()) return@post
                if (missed > 0) android.widget.Toast.makeText(this, "$missed file${if (missed == 1) "" else "s"} couldn't be fetched (is Tailscale on?)",
                    android.widget.Toast.LENGTH_LONG).show()
                startActivity(Intent.createChooser(send, "Share"))
                stopSelecting()
            }
        }.start()
    }

    /** The same question as deleting one message: from this phone, and the files they saved if no other line has them. */
    private fun deleteSelected() {
        val lines = selectedLines()
        if (lines.isEmpty()) return stopSelecting()
        val session = current
        val ats = lines.map { it.optLong("at") }.toSet()
        val saved = Media.onlyIn(this, session, Media.savedFor(this, lines), except = ats)
        panel = MessageSheet(this, "${lines.size} message${if (lines.size == 1) "" else "s"}", "Selected in this chat",
            null, null, lines.any { it.optBoolean("mine") }, saved.size) { alsoFiles ->
            val removed = Chats.remove(this, session, ats)
            Media.dropPreviews(this, removed)
            if (alsoFiles) deleted(saved, Media.delete(this, saved))
            stopSelecting()
            if (session == current) showHistory()
            showCrew()
        }.also { it.show() }
    }

    /** Saved files that Android wouldn't let go (saved by an earlier install of the app): the user is told. */
    private fun deleted(saved: List<Media.Saved>, gone: Int) {
        if (gone < saved.size) android.widget.Toast.makeText(this,
            "${saved.size - gone} saved file${if (saved.size - gone == 1) "" else "s"} couldn't be deleted: remove ${if (saved.size - gone == 1) "it" else "them"} in Files, Downloads/Henchline",
            android.widget.Toast.LENGTH_LONG).show()
    }

    // --- "/" commands (the user's wish, 2026-11-16) ------------------------------------------------------------

    private val commandsOf = HashMap<String, List<JSONObject>>()  // each chat's "/" list, from the laptop
    private val commandsAsked = HashMap<String, Long>()  // when each chat's list was last asked for: not every keystroke
    private lateinit var commandBox: android.widget.ScrollView
    private lateinit var commandRows: LinearLayout

    /** Typing "/" at the start of a message lists the agent's commands above the words, as a messaging app lists a
     *  bot's: the phone's own (stop, model, flush, call) first, then the agent's built-ins that work typed in, then
     *  the user's and the project's commands and skills. A tap on a phone one does it; on the others it is put in
     *  the box, to send as it is or with more words after it. */
    private fun setUpCommands() {
        commandRows = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        commandBox = object : android.widget.ScrollView(this) {
            override fun onMeasure(widthSpec: Int, heightSpec: Int) =  // at most about five rows; the rest scroll
                super.onMeasure(widthSpec, View.MeasureSpec.makeMeasureSpec(dp(260), View.MeasureSpec.AT_MOST))
        }.apply { addView(commandRows); visibility = View.GONE }
        findViewById<LinearLayout>(R.id.inputRow).addView(commandBox, 0, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        text.addTextChangedListener(object : android.text.TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: android.text.Editable?) = showCommands()
        })
    }

    private fun showCommands() {
        val typed = text.text.toString()
        if (!typed.startsWith("/") || typed.any { it.isWhitespace() } || current.isEmpty()) {
            commandBox.visibility = View.GONE
            return
        }
        val list = commandsOf[current] ?: run { loadCommands(current); phoneCommands() }
        val wanted = typed.drop(1).lowercase()
        val first = list.filter { it.optString("name").lowercase().startsWith(wanted) }
        val shown = first + list.filter { it !in first && it.optString("name").lowercase().contains(wanted) }
        commandRows.removeAllViews()
        for (command in shown.take(40)) commandRows.addView(commandRow(command))
        commandBox.visibility = if (shown.isEmpty()) View.GONE else View.VISIBLE
        commandBox.scrollTo(0, 0)
    }

    /** The phone's own commands, when the laptop's list isn't in yet (or the laptop is out of reach). */
    private fun phoneCommands() = listOf(
        "stop" to "Stop what the agent is doing", "model" to "Switch the agent's model",
        "flush" to "Clear this chat on the phone, with its saved files if you like", "call" to "Call your crew",
        "later" to "Send a message later: /later your words, then pick a time",
        "usage" to "Plan limits and context, as bars", "context" to "How full the context is, and what fills it",
        "workflow" to "This chat's workflows: phases and agents, live",
        "away" to "Away on or off: no listening at the laptop, agents text you instead of speaking",
        "show" to "A picture of this terminal, the screen, or one window",
    ).map { (name, about) -> JSONObject().put("name", name).put("about", about).put("kind", "phone") }

    private fun loadCommands(session: String) {
        val now = System.currentTimeMillis()
        if (session.isEmpty() || now - (commandsAsked[session] ?: 0L) < 30_000 || Files.origin(this) == null) return
        commandsAsked[session] = now
        ask("/crew/commands?session=" + android.net.Uri.encode(session), null) { answer ->
            val got = answer.optJSONArray("commands") ?: return@ask
            commandsOf[session] = (0 until got.length()).map { got.getJSONObject(it) }
            if (session == current && commandBox.visibility == View.VISIBLE) showCommands()
        }
    }

    private fun commandRow(command: JSONObject) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(12), dp(8), dp(12), dp(8))
        background = getDrawable(android.R.drawable.list_selector_background)
        val name = command.optString("name")
        val phone = command.optString("kind") == "phone"
        addView(TextView(this@MainActivity).apply {
            text = android.text.SpannableStringBuilder("/$name").apply {
                setSpan(android.text.style.StyleSpan(android.graphics.Typeface.BOLD), 0, length, 0)
                if (phone) append("  · on the phone")
            }
            setTextColor(getColor(R.color.cyan)); textSize = 15f
        })
        command.optString("about").takeIf { it.isNotEmpty() }?.let { about ->
            addView(TextView(this@MainActivity).apply {
                text = about; setTextColor(getColor(R.color.muted)); textSize = 12.5f; maxLines = 2
                ellipsize = android.text.TextUtils.TruncateAt.END
            })
        }
        setOnClickListener {
            if (name == "later") { text.setText("/later "); text.setSelection(text.length()) }  // words first, then the time
            else if (phone) { text.setText(""); runCommand(name) }
            else { text.setText("/$name "); text.setSelection(text.length()) }
        }
    }

    /** A phone command, picked or typed. */
    private fun runCommand(name: String) {
        // The keyboard down first: the sheet a command opens came up under it (the user's catch, 2026-11-16)
        getSystemService(android.view.inputmethod.InputMethodManager::class.java)?.hideSoftInputFromWindow(text.windowToken, 0)
        text.clearFocus()
        when (name) {
            "stop" -> stopAgent()
            "model" -> switchModel()
            "flush" -> current.takeIf { it.isNotEmpty() }?.let { session ->
                val label = names[session] ?: session
                chatMenu(session, label.substringAfter(", ", ""), label.substringBefore(","), clearOnly = true)
            }
            "call" -> call()
            "later" -> later("")
            "usage", "context" -> usage(name)
            "workflow" -> workflows()
            "away" -> ask("/away", JSONObject()) { answer ->
                android.widget.Toast.makeText(this, if (answer.optBoolean("away"))
                    "Away: the laptop isn't listening, and agents text you instead of speaking. /away again, or a call, ends it."
                    else "Back: hands-free listens and agents speak at the laptop again.", android.widget.Toast.LENGTH_LONG).show()
            }
            "show" -> showGrab("")
        }
    }

    /** /show, /show screen, or /show thunderbird: a picture into this chat. No words: pick from the windows. */
    private fun showGrab(query: String) {
        val session = current.ifEmpty { return }
        fun send(what: String, id: String = "") {
            ask("/show", JSONObject().put("session", session).put("what", what).put("id", id).put("query", query)
                .put("words", "Here.")) { answer ->
                val matches = answer.optJSONArray("matches")
                if (matches != null && matches.length() != 1) {
                    showWindows((0 until matches.length()).map { matches.getJSONObject(it) })
                    return@ask
                }
                android.widget.Toast.makeText(this, answer.optString("note").ifEmpty {
                    if (answer.optBoolean("ok")) "Picture sent." else "Couldn't take that picture."
                }, android.widget.Toast.LENGTH_LONG).show()
            }
        }
        when {
            query.isEmpty() -> ask("/show/windows", null) { answer ->
                showWindows(answer.optJSONArray("windows")?.let { list -> (0 until list.length()).map { list.getJSONObject(it) } }
                    ?: emptyList())
            }
            query == "screen" -> send("screen")
            query.startsWith("0x") -> send("window", query)
            else -> send("find")
        }
    }

    private fun showWindows(windows: List<JSONObject>) {
        panel = ShowSheet(this, windows) { what, id ->
            ask("/show", JSONObject().put("session", current).put("what", what).put("id", id).put("words", "Here.")) { answer ->
                android.widget.Toast.makeText(this, answer.optString("note").ifEmpty {
                    if (answer.optBoolean("ok")) "Picture sent." else "Couldn't take that picture."
                }, android.widget.Toast.LENGTH_LONG).show()
            }
        }.also { it.show() }
    }

    /** /later: the words go to this chat's agent at a time picked here (the laptop types them in then, even with
     *  the phone away); also this chat's scheduled messages, to take one back. */
    private fun later(words: String) {
        val session = current.ifEmpty { return }
        ask("/reminders?session=" + android.net.Uri.encode(session), null) { answer ->
            val list = answer.optJSONArray("reminders")
            val pending = (0 until (list?.length() ?: 0)).map { list!!.getJSONObject(it) }.filter { it.optString("state") == "pending" }
            panel = LaterSheet(this, words, pending,
                onPick = { at, keepAwake ->
                    ask("/reminders", JSONObject().put("session", session).put("words", words).put("at", at / 1000.0)
                        .put("keep_awake", keepAwake)) { got ->
                        val made = got.optJSONObject("reminder")
                        if (made != null && text.text.toString().trim() == words) text.setText("")
                        if (made == null) say("", got.optString("note").ifEmpty { "Not scheduled." }, note = true)
                        else say("", "⏰ Scheduled for ${android.text.format.DateFormat.getTimeFormat(this)
                            .format(java.util.Date(at))}: $words", note = true)
                    }
                },
                onCancel = { id -> ask("/reminders/cancel", JSONObject().put("id", id)) { } }).also { it.show() }
        }
    }

    private fun chatMenu(session: String, project: String, agent: String, clearOnly: Boolean = false) {
        val lines = Chats.load(this, session)
        val saved = Media.onlyIn(this, session, Media.savedFor(this, lines), except = lines.map { it.optLong("at") }.toSet())
        val pinned = session in pins()
        panel = ChatActionsSheet(this, agent, project.replace("_", " "), startedHere(session), gone = session !in names,
            pinned = pinned, onPin = { on -> setPinned(session, on); showCrew() },
            savedFiles = saved.size,
            onClear = { alsoFiles ->
                Chats.clear(this, session)
                Media.forget(this, session)
                Media.dropPreviews(this, lines)
                if (alsoFiles) deleted(saved, Media.delete(this, saved))
                if (session == current) showHistory()
                showCrew()
            },
            onRemove = {
                setPinned(session, false)
                Chats.clear(this, session)
                names = names - session
                val kept = org.json.JSONArray(prefs().getString("crew", "[]"))
                val crew = org.json.JSONArray((0 until kept.length()).map { kept.getJSONObject(it) }.filter { it.optString("session") != session })
                prefs().edit().putString("crew", crew.toString()).apply()
                if (session == current) { current = ""; prefs().edit().putString("current", "").apply() }
                showCrew()
            },
            onEnd = { done ->
                ask("/crew/end", JSONObject().put("session", session)) { answer ->
                    if (!answer.optBoolean("ok")) return@ask done(answer.optString("note").ifEmpty { "Not ended." })
                    done(null)
                    setPinned(session, false)
                    Chats.clear(this, session)
                    names = names - session
                    val kept = org.json.JSONArray(prefs().getString("crew", "[]"))
                    val crew = org.json.JSONArray((0 until kept.length()).map { kept.getJSONObject(it) }.filter { it.optString("session") != session })
                    prefs().edit().putString("crew", crew.toString()).apply()
                    if (session == current) { current = ""; prefs().edit().putString("current", "").apply() }
                    showCrew()
                }
            },
            // As typing /rename in it: Claude and Codex rename themselves, a Cursor editor chat is renamed by Cursor
            // (the Cursor CLI has no /rename known: no row there)
            onRename = if (!(session.startsWith("claude-") || session.startsWith("codex-") ||
                    Regex("cursor-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}").matches(session))) null
            else { name ->
                Thread {
                    val sent = Messages.text(this, session, "/rename $name", java.util.UUID.randomUUID().toString())
                    main.post {
                        android.widget.Toast.makeText(this, when {
                            sent.offline -> "Not renamed: the laptop couldn't be reached"
                            sent.note.isNotEmpty() -> sent.note
                            else -> "Renamed to “$name”"
                        }, android.widget.Toast.LENGTH_LONG).show()
                        refreshCrewQuietly()  // its new name in the list once the agent has it
                    }
                }.start()
            }).also { if (clearOnly) it.showClear() else it.show() }
    }

    /** Replying to a message: shown above the box until sent, or tapped away. */
    private fun setQuote(words: String) {
        quote = words
        quoteBar.text = "↩ " + words
        quoteBar.visibility = if (words.isEmpty()) View.GONE else View.VISIBLE
        if (words.isNotEmpty()) text.requestFocus()
    }

    /** A message dragged to the right far enough is the one being replied to, as in a messaging app. */
    private fun swipeToReply(row: View, words: String, onLong: (() -> Unit)? = null) {
        var startX = 0f
        var startY = 0f
        var dragging = false
        var longDone = false
        val slop = android.view.ViewConfiguration.get(this).scaledTouchSlop
        // Held still: the message's menu (the row takes every touch for the swipe, so it sees the hold itself)
        val held = Runnable { longDone = true; row.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS); onLong?.invoke() }
        row.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                // Kept from the start (a row that doesn't take the DOWN never sees the drag); the list still
                // scrolls, as the ScrollView takes over a vertical move
                MotionEvent.ACTION_DOWN -> {
                    startX = event.rawX; startY = event.rawY; dragging = false; longDone = false
                    if (onLong != null) view.postDelayed(held, android.view.ViewConfiguration.getLongPressTimeout().toLong())
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - startX
                    val dy = event.rawY - startY
                    if (kotlin.math.abs(dx) > slop || kotlin.math.abs(dy) > slop) view.removeCallbacks(held)
                    if (!dragging && dx > dp(12) && dx > 2 * kotlin.math.abs(dy)) {  // sideways, not a slow scroll
                        dragging = true
                        view.parent.requestDisallowInterceptTouchEvent(true)
                    }
                    if (dragging) view.translationX = dx.coerceIn(0f, dp(96).toFloat())
                    dragging
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    view.removeCallbacks(held)
                    if (dragging && !longDone && view.translationX >= dp(72)) setQuote(words)
                    view.animate().translationX(0f).setDuration(150).start()
                    dragging.also { dragging = false }
                }
                else -> false
            }
        }
    }

    private fun askName(view: TextView) {
        val box = EditText(this).apply { setText(view.text) }
        AlertDialog.Builder(this).setTitle("Your name").setView(LinearLayout(this).apply { setPadding(48, 16, 48, 0); addView(box) })
            .setPositiveButton("Save") { _, _ ->
                val name = box.text.toString().trim().ifEmpty { "Boss" }
                prefs().edit().putString("my_name", name).apply()
                view.text = name
            }
            .setNegativeButton("Cancel", null).show()
    }

    /** The current agent's kept chat, from the start: on opening the app, and on switching agents. */
    private fun showHistory() {
        chat.removeAllViews()
        shownThoughts.clear()  // they went with the views: shown again when asked again
        lastDay = ""
        myBubbles.clear()
        sentBubbles.clear()  // their views are gone with the old rows
        val history = Chats.load(this, current)
        val lastAgent = history.indexOfLast { !it.optBoolean("mine") && it.optString("from").isNotEmpty() }
        if (current.isNotEmpty() && chatScreen.visibility == View.VISIBLE && sheet?.isOpen != true) {  // you're looking: read
            Messages.seen(this, current)
            markRead(current)
        }
        var i = 0
        while (i < history.size) {
            val line = history[i]
            var end = i + 1  // pictures sent together: one album
            while (end < history.size && sameAlbum(history[end - 1], history[end])) end++
            val before = chat.childCount
            if (end - i >= 2) {
                album(history.subList(i, end), current)
                menuOn(before, current, history.subList(i, end).map { it.optLong("at") })
                i = end; continue
            }
            val file = line.optJSONObject("file")
            if (file != null) fileCard(line.optString("from"), file, current, keep = false, at = line.optLong("at"),
                caption = line.optString("text"))
            else if (line.has("thumb")) { picture(line.getString("thumb"), line.optString("text"), current, line.optLong("at")); menuOn(before, current, listOf(line.optLong("at"))) }
            else if (line.optBoolean("note")) {  // a kept note: a call and its length
                dayLine(line.optLong("at"))
                callBubble(line)
                menuOn(before, current, listOf(line.optLong("at")))
            } else {
                val bubble = say(line.optString("from"), line.optString("text"), mine = line.optBoolean("mine"),
                    session = current, keep = false, at = line.optLong("at"),
                    status = line.optString("status").ifEmpty {  // from before ticks: answered since means read
                        if (!line.optBoolean("mine")) "" else if (i < lastAgent) "read" else "delivered" })
                // Yours, by id: a delivered reply arriving later changes this bubble, the one on screen
                if (line.optBoolean("mine") && line.has("id")) sentBubbles[line.getString("id")] = bubble to line.optString("text")
            }
            i++
        }
        if (!onCall()) showConnected(false)  // not the call's chat: the chat if there is one, else the henchman
        refreshStop()
        chatScroll.post { chatScroll.fullScroll(View.FOCUS_DOWN) }
        // The thoughts went with the old views: asked for again since the chat's last line (after a resume, a
        // reinstall, an album joining; the user missed them, 2026-11-16). Each still shows once.
        thoughtsOf = ""
        if (chatScreen.visibility == View.VISIBLE) { main.removeCallbacks(thoughtsPoll); main.post(thoughtsPoll) }
        tintSelected()  // picked before the redraw: still picked
    }

    /** A file an agent sent: a picture shown small (tap for it whole), audio with its own play button, anything
     *  else as a card that saves and opens it. Long-press any of them to save it and open it in another app. */
    /** A document as a messaging app shows one (the user's wish, 2026-11-16: it was a paper clip and its name): a
     *  PDF's first page on top, then a coloured badge of its kind, its name, and "PDF · 6 pages · 6.1 MB"; a tap
     *  opens it (from Downloads/Henchline if it is there already). */
    private fun documentCard(card: LinearLayout, url: String, name: String, type: String, size: Long) {
        val kind = name.substringAfterLast('.', "").uppercase().take(4).ifEmpty { "FILE" }
        val colour = when (kind) {
            "PDF" -> 0xFFE5534B; "PPT", "PPTX", "KEY", "ODP" -> 0xFFE8833A; "DOC", "DOCX", "ODT", "RTF", "MD", "TXT" -> 0xFF4A8FE7
            "XLS", "XLSX", "CSV", "ODS" -> 0xFF3CB371; "ZIP", "TAR", "GZ", "7Z", "XZ" -> 0xFF8A8F98; else -> 0xFF26B8C8
        }.toInt()
        val width = dp(240)
        val page = ImageView(this).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            visibility = View.GONE  // only once there is a page to show
            background = GradientDrawable().apply { setColor(0xFFFFFFFF.toInt()); cornerRadius = dp(10).toFloat() }
            clipToOutline = true
        }
        card.addView(page, LinearLayout.LayoutParams(width, dp(150)).apply { bottomMargin = dp(8) })
        val badge = TextView(this).apply {
            text = kind; setTextColor(0xFFFFFFFF.toInt()); textSize = 11f; gravity = Gravity.CENTER
            setTypeface(typeface, android.graphics.Typeface.BOLD)
            background = GradientDrawable().apply { setColor(colour); cornerRadius = dp(6).toFloat() }
        }
        val title = TextView(this).apply {
            text = name; setTextColor(getColor(R.color.text)); textSize = 15f; maxLines = 2
            ellipsize = android.text.TextUtils.TruncateAt.MIDDLE
        }
        val about = TextView(this).apply {
            text = "$kind · ${Files.sizeText(size)}"; setTextColor(getColor(R.color.muted)); textSize = 12.5f
        }
        val words = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; addView(title); addView(about) }
        val line = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            addView(badge, LinearLayout.LayoutParams(dp(40), dp(44)).apply { marginEnd = dp(10) })
            addView(words, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        }
        card.addView(line, LinearLayout.LayoutParams(width, LinearLayout.LayoutParams.WRAP_CONTENT))
        card.setOnClickListener { Files.open(this, url, name, type, size, current) { say("", it, note = true) } }
        // A PDF's first page, fetched once (opening it later copies that, not the laptop again); not for a big one
        // (a known size only: one of unknown size could be anything)
        if ((type == "application/pdf" || kind == "PDF") && size in 1..25_000_000) {
            fun show(shown: android.graphics.Bitmap, pages: Int) {
                page.setImageBitmap(shown); page.visibility = View.VISIBLE
                about.text = "$kind · $pages page${if (pages == 1) "" else "s"} · ${Files.sizeText(size)}"
            }
            val kept = pictures.get("$url#page")
            val keptPages = pageCounts[url]
            if (kept != null && keptPages != null) show(kept, keptPages)  // a redraw: not rendered again
            else decoder.execute {
                val shown = Files.pdfPreview(this, url, width)
                if (shown != null) { pictures.put("$url#page", shown.first); pageCounts[url] = shown.second }
                main.post { if (shown != null) show(shown.first, shown.second) }
            }
        }
    }

    private fun fileCard(from: String, file: JSONObject, session: String, keep: Boolean = true, at: Long = 0,
                         caption: String = "") {
        val at = if (keep) JSONObject().put("from", from).put("file", file).also { Chats.add(this, session, it) }.optLong("at") else at
        val url = file.getString("url")
        val name = file.optString("name", "file")
        val type = file.optString("type", "application/octet-stream")
        val size = file.optLong("size")
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bubble_agent)
            setPadding(dp(10), dp(10), dp(10), dp(10))
            setOnLongClickListener {  // the message's menu: open elsewhere, copy, delete
                if (at > 0) messageMenu(session.ifEmpty { current }, listOf(at))
                else Files.open(this@MainActivity, url, name, type, size, session) { say("", it, note = true) }
                true
            }
        }
        val label = TextView(this).apply {
            text = "$name · ${Files.sizeText(size)}"
            setTextColor(getColor(R.color.muted))
            textSize = 13f
        }
        when {
            type.startsWith("image/") -> {
                val picture = ImageView(this).apply {
                    adjustViewBounds = true
                    maxHeight = dp(280)
                    scaleType = ImageView.ScaleType.FIT_START
                    setOnClickListener { showPicture(url, name, type, this) }
                }
                card.addView(picture, LinearLayout.LayoutParams(dp(240), LinearLayout.LayoutParams.WRAP_CONTENT))
                Thread {
                    val bitmap = Files.preview(this, url, dp(240))
                    main.post { if (bitmap != null) picture.setImageBitmap(bitmap) else label.text = "$name (couldn't load the preview: tap and hold to download)" }
                }.start()
                if (caption.isNotEmpty()) card.addView(captionView(caption))
                card.addView(label)
            }
            type.startsWith("audio/") -> card.addView(audioRow(url, name, size))
            else -> documentCard(card, url, name, type, size)
        }
        if (caption.isNotEmpty() && !type.startsWith("image/")) card.addView(captionView(caption))
        // Its time in the bottom-right corner, as every message's (the cat picture had none, 2026-11-16): on the
        // name's line under a picture, else a line of its own
        if (at > 0) {
            val last = card.getChildAt(card.childCount - 1)
            if (last === label) {
                card.removeView(label)
                card.addView(LinearLayout(this).apply {
                    orientation = LinearLayout.HORIZONTAL; gravity = Gravity.BOTTOM
                    addView(label, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
                    addView(timeLabel(at).apply { setPadding(dp(8), 0, 0, 0) })
                }, LinearLayout.LayoutParams(dp(240), LinearLayout.LayoutParams.WRAP_CONTENT))
            } else card.addView(timeLabel(at), LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(4) })
        }
        // Held anywhere on it, the picture itself too, not only its edge (the user's catch, 2026-11-16): a tappable
        // part takes the touch, so it opens the same menu
        fun holdOpensMenu(view: View) {
            if (view !== card && view.isClickable) view.setOnLongClickListener { card.performLongClick() }
            (view as? android.view.ViewGroup)?.let { group -> for (k in 0 until group.childCount) holdOpensMenu(group.getChildAt(k)) }
        }
        holdOpensMenu(card)
        // Swiped right, as any message: the one replied to (the user's catch, 2026-11-17: pictures couldn't be)
        val row = SwipeRow(this, caption.ifEmpty { "📎 $name" }) { setQuote(it) }.apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.START or Gravity.BOTTOM
            setPadding(0, dp(4), 0, dp(4))
            setTag(R.id.chat, session)
            if (at > 0) setTag(R.id.ats, listOf(at))
            visibility = if (session.isEmpty() || current.isEmpty() || session == current) View.VISIBLE else View.GONE
        }
        row.addView(ImageView(this).apply { setImageResource(avatarOf(session.ifEmpty { from })) },
            LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginEnd = dp(8) })
        row.addView(card)
        chat.addView(row, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT))
        chatScroll.post { chatScroll.fullScroll(View.FOCUS_DOWN) }
    }

    /** Play and pause, streamed from the laptop: one sound at a time. On a call it plays alongside the agents'
     *  voices (it asks for no audio focus of its own, so the call isn't paused for it). */
    private fun audioRow(url: String, name: String, size: Long): View {
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val button = ImageButton(this).apply {
            setImageResource(android.R.drawable.ic_media_play)
            setBackgroundResource(R.drawable.round_call)
            backgroundTintList = android.content.res.ColorStateList.valueOf(getColor(R.color.cyan))
            contentDescription = "Play $name"
        }
        val label = TextView(this).apply {
            text = "$name\n${Files.sizeText(size)}"
            setTextColor(getColor(R.color.text))
            textSize = 14f
            setPadding(dp(12), 0, 0, 0)
        }
        row.addView(button, LinearLayout.LayoutParams(dp(44), dp(44)))
        row.addView(label, LinearLayout.LayoutParams(dp(190), LinearLayout.LayoutParams.WRAP_CONTENT))
        button.setOnClickListener { playOrPause(url, button, label, name) }
        return row
    }

    private var sound: android.media.MediaPlayer? = null
    private var soundUrl = ""
    private var soundButton: ImageButton? = null

    private fun playOrPause(url: String, button: ImageButton, label: TextView, name: String) {
        val playing = sound
        if (playing != null && soundUrl == url) {
            if (playing.isPlaying) { playing.pause(); button.setImageResource(android.R.drawable.ic_media_play) }
            else { playing.start(); button.setImageResource(android.R.drawable.ic_media_pause) }
            return
        }
        stopSound()
        val address = Files.resolve(this, url) ?: return
        soundUrl = url
        soundButton = button
        label.text = "$name\nLoading…"
        sound = android.media.MediaPlayer().apply {
            setAudioAttributes(android.media.AudioAttributes.Builder().setUsage(android.media.AudioAttributes.USAGE_MEDIA)
                .setContentType(android.media.AudioAttributes.CONTENT_TYPE_MUSIC).build())
            setOnPreparedListener {
                label.text = "$name\n${it.duration / 60000}:${"%02d".format(it.duration / 1000 % 60)}"
                it.start()
                button.setImageResource(android.R.drawable.ic_media_pause)
            }
            setOnCompletionListener { button.setImageResource(android.R.drawable.ic_media_play) }
            setOnErrorListener { _, _, _ ->
                label.text = "$name\nCouldn't play it here: tap and hold to download"
                stopSound()
                true
            }
            try {
                setDataSource(address)
                prepareAsync()
            } catch (e: Exception) {
                label.text = "$name\nCouldn't play it: ${e.message}"
            }
        }
    }

    private fun stopSound() {
        sound?.release()
        sound = null
        soundUrl = ""
        soundButton?.setImageResource(android.R.drawable.ic_media_play)
        soundButton = null
    }

    /** A picture whole, as a messaging app's viewer (the user's wish, 2026-11-16: it was a grey box): black, the
     *  whole screen, pinch and drag to look into it, double tap to zoom; a slim bar on top with back, its name and
     *  Save (saves it and opens it in another app), a tap hiding or showing the bar. */
    private fun showPicture(url: String, name: String, type: String, from: View? = null) =
        showPictures(listOf(Pic(name, url, type, null)), 0, from)

    /** Several, as an album opens them: it grows out of the picture tapped (from) over a darkening screen, slides
     *  with your finger to the next ("2 / 5" in the bar), and shrinks back into it when closed (back, or a drag
     *  down). Smooth, not a black box over the app (the user's catch, 2026-11-16). */
    private fun showPictures(pics: List<Pic>, start: Int, from: View? = null) {
        val dialog = object : android.app.Dialog(this, android.R.style.Theme_Black_NoTitleBar_Fullscreen) {
            var closing: (() -> Unit)? = null
            @Deprecated("Back closes it with its animation") override fun onBackPressed() { closing?.invoke() ?: dismiss() }
        }
        dialog.window?.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(android.graphics.Color.TRANSPARENT))
        val screen = FrameLayout(this)
        val dark = View(this).apply { setBackgroundColor(0xFF000000.toInt()); alpha = 0f }
        screen.addView(dark, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        val cache = HashMap<Int, android.graphics.Bitmap>()
        val pager = PicturePager(this, pics.size) { i, done ->
            cache[i]?.let { done(it); return@PicturePager }
            Thread {  // twice the screen's width: room to zoom before it blurs
                val pic = pics[i]
                val bitmap = if (pic.url != null) Files.preview(this, pic.url, resources.displayMetrics.widthPixels * 2)
                             else pic.thumb?.let { decodeSmall(it, resources.displayMetrics.widthPixels * 2) }
                main.post {
                    if (bitmap != null) { cache[i] = bitmap; if (cache.size > 5) cache.keys.firstOrNull { k -> k != i }?.let { cache.remove(it) } }
                    done(bitmap)
                }
            }.start()
        }
        screen.addView(pager, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(4), dp(28), dp(12), dp(8)); alpha = 0f
            background = GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM, intArrayOf(0xCC000000.toInt(), 0x00000000))
        }
        val back = ImageView(this).apply {
            setImageResource(R.drawable.ic_back); setColorFilter(0xFFFFFFFF.toInt())
            setPadding(dp(12), dp(12), dp(12), dp(12)); contentDescription = "Back"
        }
        bar.addView(back, LinearLayout.LayoutParams(dp(48), dp(48)))
        val title = TextView(this).apply {
            setTextColor(0xFFFFFFFF.toInt()); textSize = 16f; maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.MIDDLE
        }
        bar.addView(title, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        val save = TextView(this).apply {
            text = "Save"; setTextColor(getColor(R.color.cyan)); textSize = 16f; setPadding(dp(12), dp(10), dp(4), dp(10))
        }
        bar.addView(save)
        screen.addView(bar, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.TOP))
        pager.onIndex = { at ->
            val pic = pics[at]
            title.text = if (pics.size > 1) "${at + 1} / ${pics.size} · ${pic.name}" else pic.name
            save.visibility = if (pic.url != null) View.VISIBLE else View.GONE  // yours is on your phone already
            save.setOnClickListener { pic.url?.let { u -> Files.open(this@MainActivity, u, pic.name, pic.type, session = current) { say("", it, note = true) } } }
        }
        pager.onTap = {  // hidden, it takes no taps either (Back and Save were reachable unseen)
            if (bar.alpha > 0.5f) bar.animate().alpha(0f).setDuration(150).withEndAction { bar.visibility = View.INVISIBLE }.start()
            else { bar.visibility = View.VISIBLE; bar.animate().alpha(1f).setDuration(150).start() }
        }
        pager.onDown = { part -> dark.alpha = 1f - part * 0.7f; bar.alpha = minOf(bar.alpha, 1f - part) }

        // Where the tapped picture is on the screen: the viewer grows out of it and shrinks back into it
        fun origin(): android.graphics.RectF? {
            val view = from ?: return null
            if (!view.isAttachedToWindow) return null
            val at = IntArray(2); view.getLocationOnScreen(at)
            return android.graphics.RectF(at[0].toFloat(), at[1].toFloat(), (at[0] + view.width).toFloat(), (at[1] + view.height).toFloat())
        }
        fun toward(target: View, box: android.graphics.RectF?) {  // as if it sat where box is (else a little smaller)
            val w = resources.displayMetrics.widthPixels.toFloat()
            val h = resources.displayMetrics.heightPixels.toFloat()
            if (box == null) { target.scaleX = 0.9f; target.scaleY = 0.9f; target.translationX = 0f; target.translationY = 0f; return }
            val scale = maxOf(box.width() / w, box.height() / h).coerceIn(0.1f, 1f)
            target.scaleX = scale; target.scaleY = scale
            target.translationX = box.centerX() - w / 2; target.translationY = box.centerY() - h / 2
        }
        var shut = false
        val close = {
            if (!shut) {
                shut = true
                val current = pager.current()
                val box = if (pager.index == start) origin() else null
                val w = resources.displayMetrics.widthPixels.toFloat()
                val h = resources.displayMetrics.heightPixels.toFloat()
                val scale = if (box == null) 0.9f else maxOf(box.width() / w, box.height() / h).coerceIn(0.1f, 1f)
                current.animate().scaleX(scale).scaleY(scale)
                    .translationX(if (box == null) current.translationX else box.centerX() - w / 2)
                    .translationY(if (box == null) current.translationY + dp(40) else box.centerY() - h / 2)
                    .alpha(if (box == null) 0f else 1f)
                    .setDuration(220).setInterpolator(android.view.animation.AccelerateInterpolator()).start()
                dark.animate().alpha(0f).setDuration(220).withEndAction { dialog.dismiss() }.start()
                bar.animate().alpha(0f).setDuration(120).start()
            }
        }
        dialog.closing = close
        back.setOnClickListener { close() }
        pager.onClose = close
        dialog.setContentView(screen)
        dialog.show()
        pager.show(start)
        // In: from the tapped picture to the whole screen, the dark coming up behind it
        pager.post {
            toward(pager, origin())
            pager.animate().scaleX(1f).scaleY(1f).translationX(0f).translationY(0f)
                .setDuration(260).setInterpolator(android.view.animation.DecelerateInterpolator(1.5f)).start()
            dark.animate().alpha(1f).setDuration(260).start()
            bar.animate().alpha(1f).setStartDelay(120).setDuration(180).start()
        }
    }

    /** A picture file on the phone, decoded no wider than it needs to be (and never huge). */
    private fun decodeSmall(path: String, width: Int): android.graphics.Bitmap? = try {
        val bounds = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
        android.graphics.BitmapFactory.decodeFile(path, bounds)
        var sample = 1
        while (bounds.outWidth / (sample * 2) >= width) sample *= 2
        while (bounds.outWidth / sample > 4096 || bounds.outHeight / sample > 4096) sample *= 2
        android.graphics.BitmapFactory.decodeFile(path, android.graphics.BitmapFactory.Options().apply { inSampleSize = sample })
    } catch (_: Exception) { null } catch (_: OutOfMemoryError) { null }

    private fun isPicture(line: JSONObject): Boolean =
        line.has("thumb") || line.optJSONObject("file")?.optString("type")?.startsWith("image/") == true

    /** Two pictures in a row from the same side, sent within a minute: one album, as a messaging app groups them
     *  (the user's wish, 2026-11-16: five pictures were five bubbles). */
    private fun sameAlbum(a: JSONObject, b: JSONObject): Boolean =
        isPicture(a) && isPicture(b) && a.has("thumb") == b.has("thumb") &&
            !(a.has("file") && a.optString("text").isNotEmpty()) && !(b.has("file") && b.optString("text").isNotEmpty()) && a.optBoolean("mine") == b.optBoolean("mine") &&
            a.optString("from") == b.optString("from") && b.optLong("at") - a.optLong("at") in 0..60_000

    /** The newest line of that chat joins an album with the one before it: the chat is drawn again to group them. */
    /** A file's words, under it in the same bubble. */
    private fun captionView(words: String) = TextView(this).apply {
        text = words
        setTextColor(getColor(R.color.text))
        textSize = 15f
        setTextIsSelectable(false)
        setPadding(0, dp(6), 0, dp(2))
        maxWidth = dp(240)
    }

    private fun joinsAlbum(session: String): Boolean {
        val history = Chats.load(this, session)
        return history.size >= 2 && sameAlbum(history[history.size - 2], history[history.size - 1])
    }

    /** An album: up to four pictures in a square grid, "+N" over the fourth when there are more; a tap opens the
     *  viewer at that picture, flicking through all of them. Your words with them, if any, under it. */
    private fun album(lines: List<JSONObject>, session: String) {
        val first = lines[0]
        val mine = first.optBoolean("mine")
        val from = first.optString("from")
        val pics = lines.map { line ->
            line.optJSONObject("file")?.let { f -> Pic(f.optString("name", "picture"), f.getString("url"), f.optString("type", "image/*"), null) }
                ?: Pic(line.optString("text").removePrefix("📎 ").substringBefore("\n"), null, "image/*", line.optString("thumb"))
        }
        val said = lines.mapNotNull { l -> l.optString("text").substringAfter("\n", "").trim().ifEmpty { null } }.joinToString("\n")
        dayLine(first.optLong("at"))
        val tile = dp(112)
        val grid = android.widget.GridLayout(this).apply { columnCount = 2 }
        for (k in 0 until minOf(4, pics.size)) {
            val cell = FrameLayout(this)
            val image = ImageView(this).apply {
                scaleType = ImageView.ScaleType.CENTER_CROP
                background = GradientDrawable().apply { setColor(0xFF000000.toInt()); cornerRadius = dp(10).toFloat() }
                clipToOutline = true
                contentDescription = pics[k].name
                setOnClickListener { showPictures(pics, k, this) }
            }
            cell.addView(image, FrameLayout.LayoutParams(tile, tile))
            if (k == 3 && pics.size > 4) cell.addView(TextView(this).apply {
                text = "+${pics.size - 4}"; setTextColor(0xFFFFFFFF.toInt()); textSize = 26f; gravity = Gravity.CENTER
                background = GradientDrawable().apply { setColor(0x99000000.toInt()); cornerRadius = dp(10).toFloat() }
                setOnClickListener { v -> showPictures(pics, 3, v) }
            }, FrameLayout.LayoutParams(tile, tile))
            grid.addView(cell, android.widget.GridLayout.LayoutParams().apply { width = tile; height = tile; setMargins(dp(2), dp(2), dp(2), dp(2)) })
            val pic = pics[k]
            val key = (pic.url ?: pic.thumb ?: "") + "#tile"
            val cached = pictures.get(key)
            if (cached != null) image.setImageBitmap(cached)
            else decoder.execute {
                val bitmap = if (pic.url != null) Files.preview(this, pic.url, tile) else pic.thumb?.let { decodeSmall(it, tile) }
                if (bitmap != null) pictures.put(key, bitmap)
                main.post { if (bitmap != null) image.setImageBitmap(bitmap) }
            }
        }
        val bubble = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(if (mine) R.drawable.bubble_mine else R.drawable.bubble_agent)
            setPadding(dp(6), dp(6), dp(6), dp(6))
            addView(grid)
            if (said.isNotEmpty()) addView(TextView(this@MainActivity).apply {
                text = said; setTextColor(getColor(R.color.text)); textSize = 15f; setPadding(dp(8), dp(6), dp(8), dp(2))
                maxWidth = tile * 2 + dp(8)
            })
            addView(timeLabel(lines.last().optLong("at"), mine).apply { setPadding(dp(8), dp(2), dp(6), 0) },
                LinearLayout.LayoutParams(tile * 2 + dp(8), LinearLayout.LayoutParams.WRAP_CONTENT))
        }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = if (mine) Gravity.END else Gravity.START or Gravity.BOTTOM
            setPadding(0, dp(4), 0, dp(4))
            setTag(R.id.chat, session)
        }
        if (!mine && from.isNotEmpty()) row.addView(ImageView(this).apply { setImageResource(avatarOf(session.ifEmpty { from })) },
            LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginEnd = dp(8) })
        row.addView(bubble)
        if (mine) row.addView(ImageView(this).apply { setImageResource(youFace()); tag = "you" },
            LinearLayout.LayoutParams(dp(28), dp(28)).apply { marginStart = dp(8) })
        chat.addView(row, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
    }

    /** A file from a notification (no call up, or the call screen wasn't showing): pictures and audio here,
     *  anything else saved and opened. */
    private fun openFromNotification(intent: Intent) {
        val url = intent.getStringExtra(Files.URL) ?: return
        val name = intent.getStringExtra(Files.NAME) ?: "file"
        val type = intent.getStringExtra(Files.TYPE) ?: "application/octet-stream"
        val from = intent.getStringExtra(Files.SESSION) ?: ""  // the chat it came in, for deleting it with that chat
        intent.action = null
        val note: (String) -> Unit = { android.widget.Toast.makeText(this, it, android.widget.Toast.LENGTH_SHORT).show() }
        when {
            type.startsWith("image/") -> showPicture(url, name, type)
            type.startsWith("audio/") -> {
                val holder = LinearLayout(this).apply { setPadding(dp(24), dp(16), dp(24), 0) }
                val row = audioRow(url, name, intent.getLongExtra(Files.SIZE, 0)) as LinearLayout
                holder.addView(row)
                AlertDialog.Builder(this).setTitle(name).setView(holder)
                    .setPositiveButton("Save and open") { _, _ -> Files.open(this, url, name, type, intent.getLongExtra(Files.SIZE, 0), from, note) }
                    .setNegativeButton("Close", null)
                    .setOnDismissListener { stopSound() }
                    .show()
                row.getChildAt(0).performClick()  // it starts playing
            }
            else -> Files.open(this, url, name, type, intent.getLongExtra(Files.SIZE, 0), from, note)
        }
    }

    companion object {
        const val HOLD_MS = 300L  // longer than this is a hold; shorter is a tap
        const val ANSWER = "io.github.ninjatuna007.henchline.ANSWER"  // from IncomingActivity's Answer
        const val MIC_FOR_CALL = 2  // the permission request a call waits on
        const val PICK_FILES = 3  // the paperclip's picker
        const val TAKE_PHOTO = 5  // the + sheet's camera
        // Pictures in the chat: decoded two at a time and kept a while, for the app's whole life (not per screen)
        private val decoder = java.util.concurrent.Executors.newFixedThreadPool(2)
        private val pageCounts = java.util.concurrent.ConcurrentHashMap<String, Int>()  // a PDF's pages, by its link
        private val pictures = object : android.util.LruCache<String, android.graphics.Bitmap>(16 * 1024 * 1024) {  // 16 MB
            override fun sizeOf(key: String, value: android.graphics.Bitmap) = value.byteCount
        }
        const val CALL_NOW = "io.github.ninjatuna007.henchline.CALL_NOW"  // the Quick Settings tile
        const val CALL_KEY = "call_key"

        /** A secret of this install's, which only the tile knows to send. */
        fun callKey(context: android.content.Context): String {
            val prefs = context.getSharedPreferences("call", MODE_PRIVATE)
            return prefs.getString(CALL_KEY, null) ?: java.util.UUID.randomUUID().toString().also {
                prefs.edit().putString(CALL_KEY, it).apply()
            }
        }
        val PHONE_COMMANDS = setOf("stop", "model", "flush", "call", "later", "usage", "context", "workflow", "away", "show")
        const val OPEN_FILE = "io.github.ninjatuna007.henchline.OPEN_FILE"  // a file's notification, tapped

        /** Each agent's henchman, by its session (claude-…, cursor-…, codex-…) or its name. */
        fun avatarOf(sessionOrName: String): Int = when {
            sessionOrName.startsWith("claude", ignoreCase = true) -> R.drawable.avatar_claude
            sessionOrName.startsWith("cursor", ignoreCase = true) -> R.drawable.avatar_cursor
            sessionOrName.startsWith("codex", ignoreCase = true) -> R.drawable.avatar_codex
            else -> R.drawable.avatar_handsfree
        }
    }
}

/** A row that a sideways drag pulls right, to reply to it, while what's in it keeps its taps and holds (a picture
 *  opens, its card holds for the menu): the drag is taken only once it is clearly sideways. */
class SwipeRow(context: android.content.Context, private val words: String, private val onSwiped: (String) -> Unit) :
    LinearLayout(context) {
    private var startX = 0f
    private var startY = 0f
    private var dragging = false
    private val px = context.resources.displayMetrics.density

    override fun onInterceptTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> { startX = event.rawX; startY = event.rawY; dragging = false }
            MotionEvent.ACTION_MOVE -> {
                val dx = event.rawX - startX
                val dy = event.rawY - startY
                if (dx > 12 * px && dx > 2 * kotlin.math.abs(dy)) {
                    dragging = true
                    parent.requestDisallowInterceptTouchEvent(true)
                }
            }
        }
        return dragging
    }

    @android.annotation.SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> { startX = event.rawX; startY = event.rawY; dragging = false }  // beside the bubble
            MotionEvent.ACTION_MOVE -> {
                val dx = event.rawX - startX
                if (!dragging && dx > 12 * px && dx > 2 * kotlin.math.abs(event.rawY - startY)) {
                    dragging = true
                    parent.requestDisallowInterceptTouchEvent(true)
                }
                if (dragging) translationX = dx.coerceIn(0f, 96 * px)
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (dragging && event.actionMasked == MotionEvent.ACTION_UP && translationX >= 72 * px) onSwiped(words)
                dragging = false
                animate().translationX(0f).setDuration(150).start()
            }
        }
        return dragging || event.actionMasked == MotionEvent.ACTION_DOWN
    }
}
