package ai.openphone.agent;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.LayerDrawable;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.SpannableString;
import android.text.Spanned;
import android.text.TextWatcher;
import android.text.style.StyleSpan;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.HorizontalScrollView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;
import org.json.JSONArray;

import java.io.File;
import java.io.FileWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Main-screen chat UI. The private display remains an implementation detail. */
public class MainActivity extends Activity {

    private static final int INK = Color.rgb(13, 13, 13);
    private static final int MUTED = Color.rgb(107, 114, 128);
    private static final int SUBTLE = Color.rgb(156, 163, 175);
    private static final int SURFACE = Color.rgb(244, 244, 244);
    private static final int BORDER = Color.rgb(229, 229, 229);
    private static final int OK = Color.rgb(16, 163, 127);
    private static final int WARN = Color.rgb(180, 83, 9);
    private static final int ERROR = Color.rgb(185, 28, 28);
    /** Finished tasks older than this are not replayed into a freshly opened chat. */
    private static final long RESTORE_WINDOW_MS = 10 * 60 * 1000;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView agentStatus;
    private LinearLayout messages;
    private ScrollView chatScroll;
    private View welcome;
    private View suggestions;
    private View toolsRow;
    private EditText taskInput;
    private TextView sendButton;
    private String renderedUpdate = "";
    private String progressTaskId;
    // Views of the newest task turn; older turns keep their own captured views.
    private TextView progressTitle;
    private ProgressBar progressSpinner;
    private TextView progressDone;
    private LinearLayout progressSteps;
    private TextView progressResult;
    private boolean progressUserToggled;
    private long progressStartedAt;
    private long progressFinishedAt;
    private ImageView progressPreview;
    private View progressActions;
    // Live, read-only mirror of the private display, copied in by the Mac bridge.
    private long previewStamp;
    private AlertDialog liveDialog;
    private ImageView liveImage;
    private TextView liveCaption;
    private String pendingTakeoverId;
    private long pendingTakeoverAt;
    private TextView pendingTakeoverButton;
    // The current turn's window was handed to the main screen: nothing is left to take over.
    private boolean progressTakenOver;
    // Set only for the duration of one action-button tap (see renderTurn).
    private String nextClosePackage;
    private static final String KEY_MODE = "agentMode";
    private TextView modeChip;
    private static final String KEY_USED_ACTIONS = "usedActionTasks";
    private static final String KEY_TAKEN_OVER = "takenOverTask";
    private View progressPreviewRow;
    private LinearLayout progressSuggestions;
    /** "[[action:label|request]]" at the end of a result: a one-tap follow-up request. */
    private static final Pattern ACTION = Pattern.compile("\\[\\[action:([^|\\]]{1,20})\\|([^\\]]{1,300})\\]\\]");
    // Local chat history (see ConversationStore); restoring = rebuilding views, not new events.
    private ConversationStore store;
    private JSONObject conversation;
    private boolean restoring;
    private FrameLayout drawer;
    private LinearLayout historyList;

    private final Runnable resultPoller = new Runnable() {
        @Override
        public void run() {
            renderLatestTaskUpdate();
            refreshAgentStatus();
            refreshPreview();
            checkTakeoverResult();
            handler.postDelayed(this, 600);
        }
    };

    private final BroadcastReceiver statusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            refreshAgentStatus();
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        store = new ConversationStore(this);
        setContentView(buildUi());
        openInitialConversation();

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7);
        }
        refreshAgentStatus();
    }

    @Override
    protected void onStart() {
        super.onStart();
        IntentFilter filter = new IntentFilter(AgentDisplayService.ACTION_STATUS);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(statusReceiver, filter);
        }
        handler.post(resultPoller);
        refreshAgentStatus();
    }

    @Override
    protected void onStop() {
        handler.removeCallbacks(resultPoller);
        unregisterReceiver(statusReceiver);
        super.onStop();
    }

    private View buildUi() {
        FrameLayout frame = new FrameLayout(this);
        frame.setBackgroundColor(Color.WHITE);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.WHITE);
        frame.addView(root, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        drawer = buildDrawer();
        drawer.setVisibility(View.GONE);
        frame.addView(drawer, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        // Android 15 enforces edge-to-edge. Keep the header below system bars
        // and the composer above the IME instead of allowing either to overlap.
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            frame.setOnApplyWindowInsetsListener((view, insets) -> {
                Insets safe = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout());
                Insets ime = insets.getInsets(WindowInsets.Type.ime());
                view.setPadding(safe.left, safe.top, safe.right,
                        Math.max(safe.bottom, ime.bottom));
                return insets;
            });
        } else {
            frame.setFitsSystemWindows(true);
        }
        root.addView(buildHeader(), matchWrap());

        FrameLayout body = new FrameLayout(this);
        chatScroll = new ScrollView(this);
        chatScroll.setFillViewport(true);
        messages = new LinearLayout(this);
        messages.setOrientation(LinearLayout.VERTICAL);
        messages.setPadding(dp(16), dp(8), dp(16), dp(16));
        chatScroll.addView(messages, matchWrap());
        body.addView(chatScroll, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        welcome = buildWelcome();
        body.addView(welcome, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER));
        root.addView(body, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        suggestions = buildSuggestions();
        root.addView(suggestions, matchWrap());
        toolsRow = buildToolsRow();
        toolsRow.setVisibility(View.GONE);
        root.addView(toolsRow, matchWrap());
        root.addView(buildComposer(), matchWrap());

        LinearLayout footer = new LinearLayout(this);
        footer.setGravity(Gravity.CENTER);
        footer.setPadding(dp(16), 0, dp(16), dp(8));
        modeChip = text("", 11, INK);
        modeChip.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        modeChip.setPadding(dp(10), dp(4), dp(10), dp(4));
        modeChip.setBackground(rounded(SURFACE, 12));
        modeChip.setOnClickListener(v -> toggleMode());
        footer.addView(modeChip);
        TextView footnote = text("  后台独立屏操作 · 发送、付款前会请你确认", 11, SUBTLE);
        footer.addView(footnote);
        root.addView(footer, matchWrap());
        renderMode();
        return frame;
    }

    /** "experience": the model plus steps learned from its own successful runs; "agent": the model decides every step. */
    private String agentMode() {
        return getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE).getString(KEY_MODE, "experience");
    }

    private void toggleMode() {
        String next = "experience".equals(agentMode()) ? "agent" : "experience";
        getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE).edit().putString(KEY_MODE, next).apply();
        renderMode();
        android.widget.Toast.makeText(this, "experience".equals(next)
                ? "经验模式：页面和以前成功的运行一致时直接执行，其余每步由模型决定"
                : "纯 Agent：每一步都由模型看屏幕决定", android.widget.Toast.LENGTH_SHORT).show();
    }

    private void renderMode() {
        boolean experience = "experience".equals(agentMode());
        modeChip.setText(experience ? "⚡ 经验模式" : "🧠 纯 Agent");
        modeChip.setContentDescription("执行模式：" + (experience ? "经验模式" : "纯 Agent") + "，点按切换");
    }

    private View buildHeader() {
        LinearLayout bar = new LinearLayout(this);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(8), dp(6), dp(8), dp(6));
        TextView menu = iconButton("☰", "历史对话");
        menu.setOnClickListener(v -> openHistory());
        bar.addView(menu, new LinearLayout.LayoutParams(dp(44), dp(44)));
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.VERTICAL);
        header.setGravity(Gravity.CENTER_HORIZONTAL);
        header.setPadding(0, dp(4), 0, dp(4));
        bar.addView(header, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        TextView compose = iconButton("✎", "新对话");
        compose.setOnClickListener(v -> startNewConversation());
        bar.addView(compose, new LinearLayout.LayoutParams(dp(44), dp(44)));
        TextView title = text("OpenPhone Agent", 17, INK);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        title.setGravity(Gravity.CENTER);
        header.addView(title, matchWrap());
        agentStatus = text("正在检查后台工作区…", 12, MUTED);
        agentStatus.setPadding(dp(10), dp(3), dp(10), dp(3));
        agentStatus.setContentDescription("后台工作区状态");
        agentStatus.setBackground(rounded(SURFACE, 12));
        agentStatus.setDefaultFocusHighlightEnabled(false);
        agentStatus.setOnClickListener(view -> openLiveView());
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        statusParams.topMargin = dp(2);
        header.addView(agentStatus, statusParams);
        return bar;
    }

    private TextView iconButton(String glyph, String description) {
        TextView button = text(glyph, 20, INK);
        button.setGravity(Gravity.CENTER);
        button.setContentDescription(description);
        button.setBackground(rounded(Color.WHITE, 22));
        return button;
    }

    private void explainWorkspace() {
        hideWelcome();
        if (!isAgentDisplayAlive()) {
            addAssistantMessage("后台工作区还没就绪。请连接已授权 ADB 的电脑并启动 Agent 服务；"
                    + "发送任务时会自动准备独立焦点的工作区，不会录制或操作你的主屏。");
        } else {
            addAssistantMessage("后台工作区已开启（Display " + agentDisplayId() + "）。"
                    + "我在这块独立屏幕里打开 APP、点击和输入，不占用你的画面和键盘；"
                    + "点顶部的状态标签可以随时查看我的实时画面；任务暂停时可以一键把窗口接管到主屏。");
        }
    }

    private View buildWelcome() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER_HORIZONTAL);
        box.setPadding(dp(32), 0, dp(32), dp(40));
        box.addView(avatar(48, 22), new LinearLayout.LayoutParams(dp(48), dp(48)));
        TextView greet = text("今天想让我帮你做什么？", 22, INK);
        greet.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        greet.setGravity(Gravity.CENTER);
        greet.setPadding(0, dp(16), 0, dp(8));
        box.addView(greet, matchWrap());
        TextView sub = text("我会在后台的独立屏幕里替你操作 APP，\n你可以继续正常使用手机。", 14, MUTED);
        sub.setGravity(Gravity.CENTER);
        sub.setLineSpacing(0, 1.2f);
        box.addView(sub, matchWrap());
        return box;
    }

    private View buildSuggestions() {
        HorizontalScrollView scroll = new HorizontalScrollView(this);
        scroll.setHorizontalScrollBarEnabled(false);
        LinearLayout row = new LinearLayout(this);
        row.setPadding(dp(12), dp(4), dp(12), dp(8));
        row.addView(suggestionCard("建一个日程", "明天下午的需求评审会 · Google 日历",
                "建日程建议", v -> submitGoal(calendarPrompt())));
        row.addView(suggestionCard("看看明天的安排", "只读 · 模型打开日历查看",
                "查看安排建议", v -> submitGoal(agendaPrompt())));
        row.addView(suggestionCard("后台工作区是什么？", "为什么不会打断你",
                "工作区说明", v -> explainWorkspace()));
        scroll.addView(row);
        return scroll;
    }

    private View suggestionCard(String title, String subtitle, String description, View.OnClickListener click) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(14), dp(10), dp(14), dp(10));
        card.setBackground(outlined(Color.WHITE, BORDER, 16));
        card.setContentDescription(description);
        card.setClickable(true);
        card.setOnClickListener(click);
        TextView head = text(title, 14, INK);
        head.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        card.addView(head);
        card.addView(text(subtitle, 12, MUTED));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        params.rightMargin = dp(8);
        card.setLayoutParams(params);
        return card;
    }

    private View buildToolsRow() {
        LinearLayout row = new LinearLayout(this);
        row.setPadding(dp(16), dp(4), dp(16), dp(6));
        row.addView(toolChip("📅  建日程", "建日程", () -> submitGoal(calendarPrompt())));
        TextView agenda = toolChip("🗓  看明天安排", "查看安排", () -> submitGoal(agendaPrompt()));
        LinearLayout.LayoutParams agendaParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        agendaParams.leftMargin = dp(8);
        row.addView(agenda, agendaParams);
        return row;
    }

    private TextView toolChip(String label, String description, Runnable action) {
        TextView chip = text(label, 14, INK);
        chip.setPadding(dp(14), dp(8), dp(14), dp(8));
        chip.setBackground(rounded(SURFACE, 18));
        chip.setContentDescription(description);
        chip.setOnClickListener(v -> {
            toolsRow.setVisibility(View.GONE);
            action.run();
        });
        return chip;
    }

    /** Natural-language examples sent to the model, exactly like typed messages. */
    private String tomorrowLabel() {
        LocalDate date = LocalDate.now(ZoneId.of("Europe/Zurich")).plusDays(1);
        return date.getMonthValue() + "月" + date.getDayOfMonth() + "日";
    }

    private String calendarPrompt() {
        return "帮我在日历里加一个" + tomorrowLabel() + "下午3点到4点的需求评审会，地点三楼大会议室";
    }

    private String agendaPrompt() {
        return "打开日历，告诉我" + tomorrowLabel() + "有什么安排";
    }

    private View buildComposer() {
        LinearLayout outer = new LinearLayout(this);
        outer.setPadding(dp(12), dp(6), dp(12), dp(6));
        LinearLayout pill = new LinearLayout(this);
        pill.setOrientation(LinearLayout.HORIZONTAL);
        pill.setGravity(Gravity.BOTTOM);
        pill.setPadding(dp(6), dp(6), dp(6), dp(6));
        pill.setBackground(outlined(SURFACE, BORDER, 26));

        TextView tools = text("+", 22, INK);
        tools.setGravity(Gravity.CENTER);
        tools.setContentDescription("工具");
        tools.setBackground(rounded(Color.WHITE, 20));
        tools.setOnClickListener(v -> toolsRow.setVisibility(
                toolsRow.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE));
        pill.addView(tools, new LinearLayout.LayoutParams(dp(40), dp(40)));

        taskInput = new EditText(this);
        taskInput.setHint("给 Agent 发消息…");
        taskInput.setTextSize(16);
        taskInput.setTextColor(INK);
        taskInput.setHintTextColor(SUBTLE);
        taskInput.setMinHeight(dp(40));
        taskInput.setMaxLines(5);
        taskInput.setPadding(dp(10), dp(8), dp(10), dp(8));
        taskInput.setBackground(null);
        taskInput.setImeOptions(EditorInfo.IME_ACTION_SEND);
        taskInput.setOnEditorActionListener((view, actionId, event) -> {
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                submitTypedTask();
                return true;
            }
            return false;
        });
        taskInput.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int start, int count, int after) { }
            @Override public void onTextChanged(CharSequence s, int start, int before, int count) { }
            @Override public void afterTextChanged(Editable s) { updateSendButton(); }
        });
        pill.addView(taskInput, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        sendButton = text("↑", 20, Color.WHITE);
        sendButton.setGravity(Gravity.CENTER);
        sendButton.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        sendButton.setContentDescription("发送");
        sendButton.setOnClickListener(v -> submitTypedTask());
        pill.addView(sendButton, new LinearLayout.LayoutParams(dp(40), dp(40)));
        updateSendButton();
        outer.addView(pill, matchWrap());
        return outer;
    }

    private void updateSendButton() {
        boolean ready = taskInput.getText().toString().trim().length() > 0;
        sendButton.setBackground(rounded(ready ? INK : Color.rgb(209, 213, 219), 20));
    }

    private void submitTypedTask() {
        String goal = taskInput.getText().toString().trim();
        if (goal.isEmpty()) {
            return;
        }
        submitGoal(goal);
    }

    /** One entry point for typed messages and suggestion cards. */
    private boolean submitGoal(String goal) {
        return submitGoal(goal, false);
    }

    /**
     * followUp: a request the paused task itself proposed (an action button). It is the next step
     * of that task, so it does not ask whether to abandon the paused one.
     * Returns whether the request was sent right away.
     */
    private boolean submitGoal(String goal, boolean followUp) {
        String status = getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE)
                .getString(AgentResultReceiver.KEY_STATUS, "");
        if ("queued".equals(status) || "running".equals(status)) {
            // A second task would overwrite the queued one; the agent runs one task at a time.
            hideWelcome();
            addAssistantMessage("我还在执行上一个任务，完成后再发新任务吧。可以点上面的实时画面看进度。");
            return false;
        }
        if ("needs_user".equals(status) && !followUp && !progressTakenOver) {
            // A new task starts from a clean private screen, closing what the paused one left.
            new AlertDialog.Builder(this)
                    .setTitle("上一个任务还在等你确认")
                    .setMessage("开始新任务前，Agent 会关闭后台屏幕上留下的页面。"
                            + "需要保留的话，请先点上一条任务的「接管到主屏」。\n\n"
                            + "Gmail 草稿通常会自动存进草稿箱；未保存的日程编辑页会丢失。")
                    .setNegativeButton("取消", null)
                    .setPositiveButton("继续新任务", (dialog, which) -> sendTypedTask(goal))
                    .show();
            return false;
        }
        sendTypedTask(goal);
        return true;
    }

    private void sendTypedTask(String goal) {
        taskInput.setText("");
        addUserMessage(goal);
        if (!isAgentDisplayAlive()) {
            addAssistantMessage("正在通过电脑准备独立焦点工作区，不会录制或操作主屏。");
        }
        enqueueTask(goal);
    }

    private void enqueueTask(String goal) {
        enqueueTask(goal, null);
    }

    private void enqueueTask(String goal, JSONObject calendarSpec) {
        String taskId = UUID.randomUUID().toString();
        try {
            File directory = new File(getFilesDir(), "chat");
            if (!directory.exists() && !directory.mkdirs()) {
                throw new IllegalStateException("Could not create chat directory");
            }
            JSONObject task = new JSONObject()
                    .put("id", taskId)
                    .put("goal", goal)
                    .put("createdAt", System.currentTimeMillis());
            if (calendarSpec != null) task.put("kind", "calendar_demo").put("calendarSpec", calendarSpec);
            if (nextClosePackage != null) task.put("closeOwnerPackage", nextClosePackage);
            task.put("mode", agentMode());
            File temp = new File(directory, "pending.tmp");
            File target = new File(directory, "pending.json");
            try (FileWriter writer = new FileWriter(temp)) {
                writer.write(task.toString(2));
                writer.write("\n");
            }
            if (!temp.renameTo(target)) {
                throw new IllegalStateException("Could not publish task");
            }
            getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE).edit()
                    .putString(AgentResultReceiver.KEY_TASK_ID, taskId)
                    .putString(AgentResultReceiver.KEY_STATUS, "queued")
                    .putString(AgentResultReceiver.KEY_MESSAGE, "已收到任务，正在等待 Agent 开始…")
                    .putString(AgentResultReceiver.KEY_STAGE, "")
                    .putString(AgentResultReceiver.KEY_PROGRESS, "[]")
                    .putLong(AgentResultReceiver.KEY_UPDATED_AT, System.currentTimeMillis()).apply();
            progressTaskId = taskId;
            addProgressTurn(taskId, calendarSpec != null, System.currentTimeMillis());
        } catch (Exception error) {
            addAssistantMessage("任务提交失败：" + error.getMessage());
        }
    }

    /** Open the chat of an active or just-finished task; otherwise start a new chat. */
    private void openInitialConversation() {
        SharedPreferences prefs = getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE);
        String status = prefs.getString(AgentResultReceiver.KEY_STATUS, "");
        long updated = prefs.getLong(AgentResultReceiver.KEY_UPDATED_AT, 0);
        boolean active = "queued".equals(status) || "running".equals(status) || "needs_user".equals(status)
                || System.currentTimeMillis() - updated <= RESTORE_WINDOW_MS;
        JSONObject owner = active ? store.load(store.ownerOf(prefs.getString(AgentResultReceiver.KEY_TASK_ID, ""))) : null;
        if (owner != null) showConversation(owner);
        else startNewConversation();
    }

    private void startNewConversation() {
        if (drawer != null) drawer.setVisibility(View.GONE);
        showConversation(store.create());
    }

    /** Rebuild the chat from its saved entries; only the live task keeps updating. */
    private void showConversation(JSONObject value) {
        conversation = value;
        messages.removeAllViews();
        progressTitle = null;
        toolsRow.setVisibility(View.GONE);
        SharedPreferences prefs = getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE);
        String liveTask = prefs.getString(AgentResultReceiver.KEY_TASK_ID, "");
        boolean liveHere = false;
        restoring = true;
        JSONArray entries = conversation.optJSONArray("messages");
        for (int i = 0; entries != null && i < entries.length(); i++) {
            JSONObject entry = entries.optJSONObject(i);
            if (entry == null) continue;
            switch (entry.optString("role")) {
                case "user": addUserMessage(entry.optString("text")); break;
                case "assistant": addAssistantMessage(entry.optString("text")); break;
                case "task": {
                    String taskId = entry.optString("taskId");
                    long startedAt = entry.optLong("startedAt");
                    addProgressTurn(taskId, entry.optBoolean("preset"), startedAt);
                    if (taskId.equals(liveTask)) { liveHere = true; break; }
                    // A saved task that is no longer the live one is history: static, no live preview.
                    String status = entry.optString("status");
                    boolean finished = "completed".equals(status) || "failed".equals(status) || "needs_user".equals(status);
                    if (!finished) status = "failed";
                    String message = finished ? entry.optString("message") : "这个任务没有返回结果（可能被中断）。";
                    progressFinishedAt = startedAt + entry.optLong("elapsed") * 1000;
                    renderTurn(status, message, entry.optString("progress", "[]"), true);
                    progressTitle.setText(turnTitle(status, "", prefs, ""));
                    progressPreviewRow.setVisibility(View.GONE);
                    progressActions.setVisibility(View.GONE);
                    // An unused follow-up stays available; a used one is never shown again (see markActionUsed).
                    break;
                }
                default: break;
            }
        }
        restoring = false;
        boolean empty = messages.getChildCount() == 0;
        welcome.setVisibility(empty ? View.VISIBLE : View.GONE);
        suggestions.setVisibility(empty ? View.VISIBLE : View.GONE);
        if (liveHere) {
            progressTaskId = liveTask;
            renderedUpdate = "";  // re-render the live task's current state
        } else {
            // Another chat (or none) owns the live task: never append it here.
            progressTaskId = liveTask;
            progressTitle = null;
            renderedUpdate = fingerprint(prefs);
        }
        scrollToBottom();
    }

    private void record(JSONObject entry) {
        if (restoring || conversation == null) return;
        try {
            conversation.getJSONArray("messages").put(entry);
            if ("新对话".equals(conversation.optString("title")) && "user".equals(entry.optString("role"))) {
                String text = entry.optString("text").replaceAll("\\s+", " ").trim();
                conversation.put("title", text.length() > 24 ? text.substring(0, 24) + "…" : text);
            }
        } catch (Exception ignored) { }
        store.save(conversation);
    }

    /** Persist the live task's public state into the chat that started it. */
    private void markActionUsed(String taskId) {
        if (taskId == null) return;
        SharedPreferences prefs = getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE);
        Set<String> used = new HashSet<>(prefs.getStringSet(KEY_USED_ACTIONS, new HashSet<>()));
        used.add(taskId);
        prefs.edit().putStringSet(KEY_USED_ACTIONS, used).apply();
    }

    private void persistTask(String taskId, String status, String message, String progress) {
        String ownerId = store.ownerOf(taskId);
        long elapsed = progressStartedAt > 0 && progressFinishedAt > 0
                ? Math.max(1, (progressFinishedAt - progressStartedAt) / 1000) : 0;
        JSONObject owner = conversation != null && conversation.optString("id").equals(ownerId)
                ? conversation : store.load(ownerId);
        if (owner != null && ConversationStore.updateTask(owner, taskId, status, message, progress, elapsed)) {
            store.save(owner);
        }
    }

    private String fingerprint(SharedPreferences prefs) {
        return prefs.getString(AgentResultReceiver.KEY_TASK_ID, "") + "|"
                + prefs.getString(AgentResultReceiver.KEY_STATUS, "") + "|"
                + prefs.getString(AgentResultReceiver.KEY_MESSAGE, "") + "|"
                + prefs.getString(AgentResultReceiver.KEY_PROGRESS, "[]");
    }

    private void renderLatestTaskUpdate() {
        SharedPreferences prefs = getSharedPreferences(
                AgentResultReceiver.PREFS, MODE_PRIVATE);
        String taskId = prefs.getString(AgentResultReceiver.KEY_TASK_ID, "");
        String status = prefs.getString(AgentResultReceiver.KEY_STATUS, "");
        String message = prefs.getString(AgentResultReceiver.KEY_MESSAGE, "");
        String progress = prefs.getString(AgentResultReceiver.KEY_PROGRESS, "[]");
        String stage = prefs.getString(AgentResultReceiver.KEY_STAGE, "");
        if (taskId.isEmpty()) {
            return;
        }
        boolean finished = "completed".equals(status) || "failed".equals(status) || "needs_user".equals(status);
        String fingerprint = fingerprint(prefs);
        boolean changed = !fingerprint.equals(renderedUpdate);
        String ownerId = store.ownerOf(taskId);
        if (ownerId != null && !ownerId.equals(conversation.optString("id"))) {
            // The task belongs to another chat: save its progress there, never show it here.
            if (changed) {
                renderedUpdate = fingerprint;
                persistTask(taskId, status, message, progress);
            }
            return;
        }
        if (changed && (!taskId.equals(progressTaskId) || progressTitle == null)) {
            // A task queued outside this chat view (or after a restart): start time unknown.
            progressTaskId = taskId;
            addProgressTurn(taskId, progress.contains("本地 UI 预设"), 0);
        }
        if (!taskId.equals(progressTaskId) || progressTitle == null) return;
        if (finished && progressFinishedAt == 0) progressFinishedAt = System.currentTimeMillis();
        if (changed) {
            renderedUpdate = fingerprint;
            renderTurn(status, message, progress, finished);
            persistTask(taskId, status, message, progress);
        }
        // Refresh every tick so the model wait timer keeps counting.
        progressTitle.setText(turnTitle(status, stage, prefs, latestStep(progress)));
    }

    private void renderTurn(String status, String message, String progress, boolean finished) {
        renderSteps(progress, finished);
        progressSpinner.setVisibility(finished ? View.GONE : View.VISIBLE);
        progressDone.setVisibility(finished ? View.VISIBLE : View.GONE);
        if ("completed".equals(status)) {
            progressDone.setText("✓");
            progressDone.setTextColor(OK);
        } else if (finished) {
            progressDone.setText("!");
            progressDone.setTextColor("failed".equals(status) ? ERROR : WARN);
        }
        progressSuggestions.removeAllViews();
        if (finished) {
            String lead = "completed".equals(status) ? "" : "failed".equals(status) ? "任务已停止：" : "需要你确认：";
            Matcher action = ACTION.matcher(message);
            String owner = progressTaskId;
            SharedPreferences prefs = getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE);
            boolean used = owner != null && prefs.getStringSet(KEY_USED_ACTIONS, new HashSet<>()).contains(owner);
            while (action.find() && !used) {
                // "request||close:package": the owner's tap also allows closing that app's main-screen window.
                String[] parts = action.group(2).split("\\|\\|close:", 2);
                String request = parts[0];
                String closePackage = parts.length > 1 ? parts[1].trim() : null;
                TextView button = pillButton(action.group(1), true);
                button.setContentDescription(action.group(1));
                button.setOnClickListener(v -> {
                    // One tap only: the follow-up is an ordinary request, sent like a typed message.
                    // Only the live paused task's own follow-up skips the "abandon it?" question.
                    boolean live = owner != null && owner.equals(prefs.getString(AgentResultReceiver.KEY_TASK_ID, ""));
                    nextClosePackage = closePackage;
                    boolean sent = submitGoal(request, live);
                    nextClosePackage = null;
                    if (sent) {
                        button.setEnabled(false);
                        button.setText("已提交");
                        markActionUsed(owner);
                    }
                });
                progressSuggestions.addView(button);
            }
            message = ACTION.matcher(message).replaceAll("").trim();
            progressResult.setText(firstLineBold(lead + message));
            progressResult.setTextColor("failed".equals(status) ? ERROR : INK);
            progressResult.setVisibility(View.VISIBLE);
            if (!progressUserToggled) progressSteps.setVisibility(View.GONE);
        } else {
            progressResult.setVisibility(View.GONE);
        }
        // A paused or stopped task may leave a draft/form: offer a one-tap handover.
        progressActions.setVisibility(!progressTakenOver
                && ("needs_user".equals(status) || "failed".equals(status)) ? View.VISIBLE : View.GONE);
        scrollToBottom();
    }

    private String turnTitle(String status, String stage, SharedPreferences prefs, String latest) {
        String chevron = progressSteps.getVisibility() == View.VISIBLE ? "  ˄" : "  ˅";
        switch (status) {
            case "completed": {
                String elapsed = progressStartedAt > 0
                        ? " · 用时 " + Math.max(1, (progressFinishedAt - progressStartedAt) / 1000) + " 秒" : "";
                return "已完成 · " + progressSteps.getChildCount() + " 步" + elapsed + chevron;
            }
            case "failed": return "已停止" + chevron;
            case "needs_user": return "等待你确认" + chevron;
            case "running":
                if ("model".equals(stage)) {
                    long waited = Math.max(0, (System.currentTimeMillis()
                            - prefs.getLong(AgentResultReceiver.KEY_UPDATED_AT, System.currentTimeMillis())) / 1000);
                    return "模型处理中 · " + waited + " 秒" + chevron;
                }
                return (latest.isEmpty() ? "正在执行…" : latest) + chevron;
            default: return "等待 Agent 开始…" + chevron;
        }
    }

    private void addProgressTurn(String taskId, boolean preset, long startedAt) {
        hideWelcome();
        if (!restoring) {
            store.bindTask(taskId, conversation.optString("id"));
            try {
                record(new JSONObject().put("role", "task").put("taskId", taskId).put("preset", preset)
                        .put("startedAt", startedAt).put("status", "queued").put("message", "").put("progress", "[]"));
            } catch (Exception ignored) { }
        }
        progressUserToggled = false;
        progressStartedAt = startedAt;
        progressFinishedAt = 0;
        LinearLayout turn = assistantTurn();
        LinearLayout column = (LinearLayout) turn.getChildAt(1);

        LinearLayout head = new LinearLayout(this);
        head.setGravity(Gravity.CENTER_VERTICAL);
        head.setContentDescription("执行过程");
        ProgressBar spinner = new ProgressBar(this, null, android.R.attr.progressBarStyleSmall);
        spinner.setIndeterminateTintList(ColorStateList.valueOf(MUTED));
        head.addView(spinner, new LinearLayout.LayoutParams(dp(16), dp(16)));
        TextView done = text("✓", 14, OK);
        done.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        done.setVisibility(View.GONE);
        head.addView(done, new LinearLayout.LayoutParams(dp(16), LinearLayout.LayoutParams.WRAP_CONTENT));
        TextView title = text("等待 Agent 开始…  ˄", 14, MUTED);
        title.setPadding(dp(8), 0, 0, 0);
        head.addView(title, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        column.addView(head, matchWrap());

        if (preset) {
            TextView tag = text("本地 UI 预设 · 不调用模型", 11, MUTED);
            tag.setPadding(dp(8), dp(2), dp(8), dp(2));
            tag.setBackground(rounded(SURFACE, 8));
            LinearLayout.LayoutParams tagParams = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
            tagParams.topMargin = dp(6);
            column.addView(tag, tagParams);
        }

        LinearLayout steps = new LinearLayout(this);
        steps.setOrientation(LinearLayout.VERTICAL);
        steps.setPadding(dp(12), dp(4), 0, dp(2));
        steps.setBackground(leftRule());
        LinearLayout.LayoutParams stepParams = matchWrap();
        stepParams.topMargin = dp(8);
        stepParams.leftMargin = dp(7);
        column.addView(steps, stepParams);

        LinearLayout previewRow = new LinearLayout(this);
        previewRow.setGravity(Gravity.CENTER_VERTICAL);
        previewRow.setPadding(0, dp(10), 0, 0);
        ImageView preview = new ImageView(this);
        preview.setScaleType(ImageView.ScaleType.FIT_CENTER);
        preview.setBackground(outlined(SURFACE, BORDER, 10));
        preview.setPadding(dp(2), dp(2), dp(2), dp(2));
        preview.setContentDescription("后台实时画面");
        preview.setOnClickListener(v -> openLiveView());
        previewRow.addView(preview, new LinearLayout.LayoutParams(dp(90), dp(160)));
        LinearLayout previewText = new LinearLayout(this);
        previewText.setOrientation(LinearLayout.VERTICAL);
        previewText.setPadding(dp(12), 0, 0, 0);
        TextView previewTitle = text("后台实时画面", 13, INK);
        previewTitle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        previewText.addView(previewTitle);
        previewText.addView(text("点按放大 · 只能看，不能点", 12, MUTED));
        previewRow.addView(previewText);
        column.addView(previewRow, matchWrap());

        LinearLayout actions = new LinearLayout(this);
        actions.setPadding(0, dp(12), 0, 0);
        actions.setVisibility(View.GONE);
        TextView takeover = pillButton("接管到主屏", true);
        takeover.setContentDescription("接管到主屏");
        takeover.setOnClickListener(v -> requestTakeover(takeover));
        actions.addView(takeover);
        TextView enlarge = pillButton("查看大图", false);
        enlarge.setOnClickListener(v -> openLiveView());
        LinearLayout.LayoutParams enlargeParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        enlargeParams.leftMargin = dp(8);
        actions.addView(enlarge, enlargeParams);

        TextView result = text("", 16, INK);
        result.setLineSpacing(0, 1.3f);
        result.setTextIsSelectable(true);
        result.setVisibility(View.GONE);
        LinearLayout.LayoutParams resultParams = matchWrap();
        resultParams.topMargin = dp(10);
        column.addView(result, resultParams);
        LinearLayout suggestionsRow = new LinearLayout(this);
        suggestionsRow.setPadding(0, dp(12), 0, 0);
        column.addView(suggestionsRow, matchWrap());
        column.addView(actions, matchWrap());

        // Capture this turn's views; older turns stay independently expandable.
        head.setOnClickListener(view -> {
            boolean expand = steps.getVisibility() != View.VISIBLE;
            steps.setVisibility(expand ? View.VISIBLE : View.GONE);
            title.setText(title.getText().toString().replace(expand ? "˅" : "˄", expand ? "˄" : "˅"));
            if (steps == progressSteps) progressUserToggled = true;
        });
        progressSpinner = spinner;
        progressDone = done;
        progressTitle = title;
        progressSteps = steps;
        progressResult = result;
        progressPreview = preview;
        progressActions = actions;
        progressPreviewRow = previewRow;
        progressSuggestions = suggestionsRow;
        progressTaskId = taskId;
        progressTakenOver = taskId.equals(getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE)
                .getString(KEY_TAKEN_OVER, ""));
        if (progressTakenOver) previewRow.setVisibility(View.GONE);
        previewStamp = 0;
        refreshPreview();
        renderSteps("[]", false);
        messages.addView(turn, matchWrap());
        scrollToBottom();
    }

    private void renderSteps(String progress, boolean finished) {
        progressSteps.removeAllViews();
        try {
            JSONArray events = new JSONArray(progress);
            for (int i = 0; i < events.length(); i++) {
                JSONObject event = events.optJSONObject(i);
                if (event == null) continue;
                boolean current = !finished && i == events.length() - 1;
                TextView step = text((current ? "●  " : "✓  ") + stepText(event.optString("message")),
                        13, current ? INK : MUTED);
                step.setPadding(0, dp(3), 0, dp(3));
                progressSteps.addView(step, matchWrap());
            }
        } catch (Exception ignored) { }
        if (progressSteps.getChildCount() == 0) {
            progressSteps.addView(text("已排队，等待电脑上的 Agent 接手…", 13, MUTED), matchWrap());
        }
    }

    private String latestStep(String progress) {
        try {
            JSONArray events = new JSONArray(progress);
            if (events.length() == 0) return "";
            return stepText(events.getJSONObject(events.length() - 1).optString("message"));
        } catch (Exception ignored) {
            return "";
        }
    }

    private static String stepText(String message) {
        return message.replaceFirst("^本地 UI 预设[：:]\\s*", "");
    }

    private CharSequence firstLineBold(String value) {
        int end = value.indexOf('\n');
        SpannableString span = new SpannableString(value);
        span.setSpan(new StyleSpan(Typeface.BOLD), 0, end < 0 ? value.length() : end,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        return span;
    }

    private File previewFile() {
        return new File(new File(getFilesDir(), "preview"), "frame.png");
    }

    private Bitmap decodePreview(int sampleSize) {
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inSampleSize = sampleSize;
        return BitmapFactory.decodeFile(previewFile().getPath(), options);
    }

    private void refreshPreview() {
        long modified = previewFile().lastModified();
        if (modified != 0 && modified != previewStamp) {
            previewStamp = modified;
            // A frame from before this task started belongs to an earlier task: do not show it.
            boolean fresh = progressStartedAt <= 0 || modified >= progressStartedAt;
            Bitmap thumb = progressPreview == null || !fresh ? null : decodePreview(4);
            if (thumb != null) progressPreview.setImageBitmap(thumb);
            Bitmap full = liveImage == null ? null : decodePreview(1);
            if (full != null) liveImage.setImageBitmap(full);
        }
        if (liveCaption != null) {
            long age = modified == 0 ? -1 : (System.currentTimeMillis() - modified) / 1000;
            liveCaption.setText(age < 0 ? "还没有收到后台画面：请确认电脑端 bridge 正在运行。"
                    : age <= 5 ? "Agent 独立屏幕 · 实时（约每秒刷新）· 只能看，不能点"
                    : "画面已 " + age + " 秒未更新：电脑端 bridge 可能未运行，显示的是最后一帧。");
        }
    }

    private void openLiveView() {
        if (liveDialog != null && liveDialog.isShowing()) return;
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(16), dp(8), dp(16), 0);
        liveCaption = text("", 12, MUTED);
        liveCaption.setPadding(0, 0, 0, dp(8));
        box.addView(liveCaption, matchWrap());
        liveImage = new ImageView(this);
        liveImage.setScaleType(ImageView.ScaleType.FIT_CENTER);
        liveImage.setAdjustViewBounds(true);
        liveImage.setBackgroundColor(SURFACE);
        liveImage.setContentDescription("后台实时画面大图");
        box.addView(liveImage, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                (int) (getResources().getDisplayMetrics().heightPixels * 0.6f)));
        previewStamp = 0;  // force a full-size decode for the dialog
        liveDialog = new AlertDialog.Builder(this)
                .setTitle("Agent 后台画面")
                .setView(box)
                .setNegativeButton("关闭", null)
                .setPositiveButton("接管到主屏", (dialog, which) -> requestTakeover(null))
                .create();
        liveDialog.setOnDismissListener(dialog -> { liveImage = null; liveCaption = null; });
        liveDialog.show();
        refreshPreview();
    }

    /** Ask the Mac bridge to move the paused private app window to the owner's screen. */
    private void requestTakeover(TextView button) {
        String status = getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE)
                .getString(AgentResultReceiver.KEY_STATUS, "");
        if ("queued".equals(status) || "running".equals(status)) {
            hideWelcome();
            addAssistantMessage("Agent 正在后台操作，暂停或结束后才能接管，避免和它同时点同一个页面。");
            return;
        }
        if (pendingTakeoverId != null) return;
        if (progressTakenOver) {
            hideWelcome();
            addAssistantMessage("这个页面已经交到主屏了，在最近任务里就能找到它。");
            return;
        }
        String id = UUID.randomUUID().toString();
        try {
            File directory = new File(getFilesDir(), "chat");
            if (!directory.exists() && !directory.mkdirs()) throw new IllegalStateException("Could not create chat directory");
            File temp = new File(directory, "takeover.tmp");
            try (FileWriter writer = new FileWriter(temp)) {
                writer.write(new JSONObject().put("id", id).put("requestedAt", System.currentTimeMillis()).toString());
            }
            if (!temp.renameTo(new File(directory, "takeover.json"))) throw new IllegalStateException("Could not publish request");
        } catch (Exception error) {
            addAssistantMessage("接管请求失败：" + error.getMessage());
            return;
        }
        pendingTakeoverId = id;
        pendingTakeoverAt = System.currentTimeMillis();
        pendingTakeoverButton = button;
        if (button != null) {
            button.setText("正在接管…");
            button.setEnabled(false);
        }
    }

    private void checkTakeoverResult() {
        if (pendingTakeoverId == null) return;
        String message = null;
        boolean ok = false;
        try {
            File result = new File(new File(getFilesDir(), "chat"), "takeover-result.json");
            if (result.exists()) {
                JSONObject value = new JSONObject(new String(Files.readAllBytes(result.toPath()), StandardCharsets.UTF_8));
                if (pendingTakeoverId.equals(value.optString("id"))) {
                    message = value.optString("message");
                    // "empty": nothing is left on the private screen, so the button has no use either.
                    ok = value.optBoolean("ok") || value.optBoolean("empty");
                }
            }
        } catch (Exception ignored) { }
        if (message == null && System.currentTimeMillis() - pendingTakeoverAt > 15_000) {
            message = "接管暂时没有响应（电脑端可能在忙或未运行），窗口没有移动。请再点一次「接管到主屏」重试。";
        }
        if (message == null) return;
        pendingTakeoverId = null;
        if (pendingTakeoverButton != null) {
            pendingTakeoverButton.setText("接管到主屏");
            pendingTakeoverButton.setEnabled(true);
            pendingTakeoverButton = null;
        }
        if (ok) {
            // The private screen is now empty: hide the stale preview and the takeover button.
            progressTakenOver = true;
            if (progressTaskId != null) getSharedPreferences(AgentResultReceiver.PREFS, MODE_PRIVATE)
                    .edit().putString(KEY_TAKEN_OVER, progressTaskId).apply();
            if (progressActions != null) progressActions.setVisibility(View.GONE);
            if (progressPreviewRow != null) progressPreviewRow.setVisibility(View.GONE);
            if (liveDialog != null) liveDialog.dismiss();
        }
        hideWelcome();
        addAssistantMessage(message);
    }

    private TextView pillButton(String label, boolean primary) {
        TextView button = text(label, 14, primary ? Color.WHITE : INK);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setPadding(dp(16), dp(9), dp(16), dp(9));
        button.setBackground(primary ? rounded(INK, 18) : outlined(Color.WHITE, BORDER, 18));
        return button;
    }

    private void addUserMessage(String value) {
        hideWelcome();
        try { record(new JSONObject().put("role", "user").put("text", value)); } catch (Exception ignored) { }
        LinearLayout row = new LinearLayout(this);
        row.setGravity(Gravity.END);
        row.setPadding(dp(40), dp(10), 0, dp(10));
        TextView bubble = text(value, 16, INK);
        bubble.setLineSpacing(0, 1.25f);
        bubble.setPadding(dp(16), dp(10), dp(16), dp(10));
        bubble.setBackground(rounded(SURFACE, 20));
        row.addView(bubble);
        messages.addView(row, matchWrap());
        scrollToBottom();
    }

    private void addAssistantMessage(String value) {
        hideWelcome();
        try { record(new JSONObject().put("role", "assistant").put("text", value)); } catch (Exception ignored) { }
        LinearLayout turn = assistantTurn();
        TextView body = text(value, 16, INK);
        body.setLineSpacing(0, 1.3f);
        body.setTextIsSelectable(true);
        ((LinearLayout) turn.getChildAt(1)).addView(body, matchWrap());
        messages.addView(turn, matchWrap());
        scrollToBottom();
    }

    /** Avatar + content column: the assistant side of a turn, without a bubble. */
    private LinearLayout assistantTurn() {
        LinearLayout turn = new LinearLayout(this);
        turn.setPadding(0, dp(10), 0, dp(10));
        turn.addView(avatar(26, 12), new LinearLayout.LayoutParams(dp(26), dp(26)));
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        column.setPadding(dp(12), dp(2), 0, 0);
        turn.addView(column, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        return turn;
    }

    private TextView avatar(int sizeDp, int glyphSp) {
        TextView mark = text("◆", glyphSp, Color.WHITE);
        mark.setGravity(Gravity.CENTER);
        mark.setBackground(rounded(INK, sizeDp / 2));
        return mark;
    }

    /** Side panel with saved chats, like the history drawer of chat assistants. */
    private FrameLayout buildDrawer() {
        FrameLayout overlay = new FrameLayout(this);
        View scrim = new View(this);
        scrim.setBackgroundColor(Color.argb(90, 0, 0, 0));
        scrim.setOnClickListener(v -> overlay.setVisibility(View.GONE));
        overlay.addView(scrim, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setBackgroundColor(Color.WHITE);
        panel.setClickable(true);
        panel.setPadding(dp(12), dp(12), dp(12), dp(12));
        LinearLayout top = new LinearLayout(this);
        top.setGravity(Gravity.CENTER_VERTICAL);
        top.setPadding(dp(4), 0, 0, dp(10));
        TextView heading = text("历史对话", 18, INK);
        heading.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        top.addView(heading, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        TextView fresh = pillButton("✎  新对话", true);
        fresh.setContentDescription("新建对话");
        fresh.setOnClickListener(v -> startNewConversation());
        top.addView(fresh);
        panel.addView(top, matchWrap());
        ScrollView scroll = new ScrollView(this);
        historyList = new LinearLayout(this);
        historyList.setOrientation(LinearLayout.VERTICAL);
        scroll.addView(historyList, matchWrap());
        panel.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        TextView note = text("对话只保存在这台手机上", 11, SUBTLE);
        note.setPadding(dp(4), dp(8), 0, 0);
        panel.addView(note, matchWrap());
        overlay.addView(panel, new FrameLayout.LayoutParams(
                (int) (getResources().getDisplayMetrics().widthPixels * 0.82f),
                FrameLayout.LayoutParams.MATCH_PARENT, Gravity.START));
        return overlay;
    }

    private void openHistory() {
        historyList.removeAllViews();
        List<JSONObject> chats = store.list();
        if (chats.isEmpty()) {
            TextView none = text("还没有历史对话。发出第一条消息后会自动保存在这里。", 14, MUTED);
            none.setPadding(dp(4), dp(8), dp(4), dp(8));
            historyList.addView(none, matchWrap());
        }
        String current = conversation == null ? "" : conversation.optString("id");
        for (JSONObject chat : chats) {
            String id = chat.optString("id");
            LinearLayout item = new LinearLayout(this);
            item.setOrientation(LinearLayout.VERTICAL);
            item.setPadding(dp(12), dp(10), dp(12), dp(10));
            item.setBackground(rounded(id.equals(current) ? SURFACE : Color.WHITE, 12));
            item.setContentDescription("对话：" + chat.optString("title"));
            TextView title = text(chat.optString("title", "新对话"), 15, INK);
            title.setSingleLine(true);
            title.setEllipsize(android.text.TextUtils.TruncateAt.END);
            item.addView(title, matchWrap());
            item.addView(text(timeLabel(chat.optLong("updatedAt")), 12, MUTED), matchWrap());
            item.setOnClickListener(v -> {
                drawer.setVisibility(View.GONE);
                JSONObject loaded = store.load(id);
                if (loaded != null) showConversation(loaded);
            });
            item.setOnLongClickListener(v -> {
                new AlertDialog.Builder(this)
                        .setTitle("删除这个对话？")
                        .setMessage("「" + chat.optString("title") + "」会从这台手机上删除，无法恢复。不影响已经在其他 APP 里完成的操作。")
                        .setNegativeButton("取消", null)
                        .setPositiveButton("删除", (dialog, which) -> {
                            store.delete(id);
                            if (id.equals(current)) startNewConversation();
                            openHistory();
                        }).show();
                return true;
            });
            LinearLayout.LayoutParams params = matchWrap();
            params.bottomMargin = dp(4);
            historyList.addView(item, params);
        }
        drawer.setVisibility(View.VISIBLE);
    }

    private String timeLabel(long millis) {
        ZonedDateTime time = Instant.ofEpochMilli(millis).atZone(ZoneId.systemDefault());
        LocalDate today = LocalDate.now(ZoneId.systemDefault());
        String clock = String.format(java.util.Locale.ROOT, "%02d:%02d", time.getHour(), time.getMinute());
        if (time.toLocalDate().equals(today)) return "今天 " + clock;
        if (time.toLocalDate().equals(today.minusDays(1))) return "昨天 " + clock;
        return time.getMonthValue() + " 月 " + time.getDayOfMonth() + " 日 " + clock;
    }

    @Override
    public void onBackPressed() {
        if (drawer != null && drawer.getVisibility() == View.VISIBLE) {
            drawer.setVisibility(View.GONE);
            return;
        }
        super.onBackPressed();
    }

    private void hideWelcome() {
        welcome.setVisibility(View.GONE);
        suggestions.setVisibility(View.GONE);
    }

    private void scrollToBottom() {
        // Scroll without moving keyboard focus away from the owner's composer.
        chatScroll.post(() -> chatScroll.smoothScrollTo(0, messages.getHeight()));
    }

    private void refreshAgentStatus() {
        if (isAgentDisplayAlive()) {
            agentStatus.setText("●  后台工作区已就绪");
            agentStatus.setTextColor(OK);
        } else {
            agentStatus.setText("○  等待电脑连接后台工作区");
            agentStatus.setTextColor(MUTED);
        }
    }

    private int agentDisplayId() {
        return getSharedPreferences(AgentDisplayService.PREFS, MODE_PRIVATE)
                .getInt(AgentDisplayService.KEY_DISPLAY_ID, -1);
    }

    private boolean isAgentDisplayAlive() {
        int id = agentDisplayId();
        if (id <= 0) {
            return false;
        }
        DisplayManager manager = getSystemService(DisplayManager.class);
        android.view.Display display = manager == null ? null : manager.getDisplay(id);
        return display != null
                && (display.getFlags() & AgentResultReceiver.REQUIRED_DISPLAY_FLAGS)
                == AgentResultReceiver.REQUIRED_DISPLAY_FLAGS;
    }

    private TextView text(String value, int sizeSp, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sizeSp);
        view.setTextColor(color);
        return view;
    }

    private GradientDrawable rounded(int color, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radiusDp));
        return drawable;
    }

    private GradientDrawable outlined(int color, int stroke, int radiusDp) {
        GradientDrawable drawable = rounded(color, radiusDp);
        drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    /** A thin vertical rule on the left, like a collapsible "thinking" trace. */
    private Drawable leftRule() {
        GradientDrawable line = new GradientDrawable();
        line.setColor(BORDER);
        LayerDrawable layer = new LayerDrawable(new Drawable[]{line});
        layer.setLayerWidth(0, dp(2));
        layer.setLayerGravity(0, Gravity.START | Gravity.FILL_VERTICAL);
        return layer;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
