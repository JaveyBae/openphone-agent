package ai.openphone.agent;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.File;
import java.io.FileWriter;
import java.util.UUID;

/** Main-screen chat UI. The private display remains an implementation detail. */
public class MainActivity extends Activity {

    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView agentStatus;
    private LinearLayout messages;
    private ScrollView chatScroll;
    private EditText taskInput;
    private String renderedUpdate = "";
    private String progressTaskId;
    private TextView progressBubble;

    private final Runnable resultPoller = new Runnable() {
        @Override
        public void run() {
            renderLatestTaskUpdate();
            refreshAgentStatus();
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
        setContentView(buildUi());
        addBubble(
                "你好，我是 OpenPhone Agent。\n\n"
                        + "你可以把任务直接告诉我。我会在后台的独立工作区执行，"
                        + "你可以继续使用手机。",
                false);

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
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(245, 247, 251));
        // Android 15 enforces edge-to-edge. Keep the header below system bars
        // and the composer above the IME instead of allowing either to overlap.
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                Insets safe = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout());
                Insets ime = insets.getInsets(WindowInsets.Type.ime());
                view.setPadding(safe.left, safe.top, safe.right,
                        Math.max(safe.bottom, ime.bottom));
                return insets;
            });
        } else {
            root.setFitsSystemWindows(true);
        }

        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(dp(18), dp(12), dp(18), dp(12));
        header.setBackgroundColor(Color.WHITE);

        TextView mark = text("◆", 24, Color.WHITE);
        mark.setGravity(Gravity.CENTER);
        mark.setBackground(rounded(Color.rgb(37, 99, 235), 14));
        header.addView(mark, new LinearLayout.LayoutParams(dp(44), dp(44)));

        LinearLayout titleBlock = new LinearLayout(this);
        titleBlock.setOrientation(LinearLayout.VERTICAL);
        titleBlock.setPadding(dp(12), 0, 0, 0);
        TextView title = text("OpenPhone Agent", 18, Color.rgb(15, 23, 42));
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        titleBlock.addView(title);
        agentStatus = text("正在检查后台工作区…", 12, Color.rgb(100, 116, 139));
        titleBlock.addView(agentStatus);
        header.addView(titleBlock, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        TextView privacy = text("私有屏", 12, Color.rgb(30, 64, 175));
        privacy.setGravity(Gravity.CENTER);
        privacy.setPadding(dp(10), dp(6), dp(10), dp(6));
        privacy.setBackground(rounded(Color.rgb(239, 246, 255), 14));
        privacy.setContentDescription("开启 Agent 私有工作区");
        privacy.setOnClickListener(view -> {
            if (!isAgentDisplayAlive()) {
                addBubble("请连接已授权的 ADB 电脑并启动 Agent 服务。发送任务后会自动准备独立焦点工作区；不需要录制主屏。", false);
            } else {
                addBubble("后台工作区已开启，可在电脑的双屏预览窗口查看。", false);
            }
        });
        header.addView(privacy);
        root.addView(header, matchWrap());

        chatScroll = new ScrollView(this);
        chatScroll.setFillViewport(true);
        messages = new LinearLayout(this);
        messages.setOrientation(LinearLayout.VERTICAL);
        messages.setPadding(dp(14), dp(18), dp(14), dp(18));
        chatScroll.addView(messages, matchWrap());
        root.addView(chatScroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        LinearLayout composer = new LinearLayout(this);
        composer.setOrientation(LinearLayout.HORIZONTAL);
        composer.setGravity(Gravity.BOTTOM);
        composer.setPadding(dp(12), dp(10), dp(12), dp(12));
        composer.setBackgroundColor(Color.WHITE);

        taskInput = new EditText(this);
        taskInput.setHint("告诉我你想完成什么…");
        taskInput.setTextSize(15);
        taskInput.setTextColor(Color.rgb(15, 23, 42));
        taskInput.setHintTextColor(Color.rgb(148, 163, 184));
        taskInput.setMinHeight(dp(52));
        taskInput.setMaxLines(4);
        taskInput.setPadding(dp(16), dp(10), dp(16), dp(10));
        taskInput.setBackground(rounded(Color.rgb(241, 245, 249), 22));
        taskInput.setImeOptions(EditorInfo.IME_ACTION_SEND);
        taskInput.setOnEditorActionListener((view, actionId, event) -> {
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                submitTypedTask();
                return true;
            }
            return false;
        });
        composer.addView(taskInput, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        Button sendButton = new Button(this);
        sendButton.setText("↑");
        sendButton.setTextSize(22);
        sendButton.setTextColor(Color.WHITE);
        sendButton.setAllCaps(false);
        sendButton.setBackground(rounded(Color.rgb(37, 99, 235), 26));
        sendButton.setOnClickListener(v -> submitTypedTask());
        LinearLayout.LayoutParams sendParams = new LinearLayout.LayoutParams(dp(54), dp(54));
        sendParams.leftMargin = dp(8);
        composer.addView(sendButton, sendParams);
        root.addView(composer, matchWrap());
        return root;
    }

    private void submitTypedTask() {
        String goal = taskInput.getText().toString().trim();
        if (goal.isEmpty()) {
            return;
        }
        taskInput.setText("");
        addBubble(goal, true);
        if (!isAgentDisplayAlive()) {
            addBubble("正在通过电脑准备独立焦点工作区，不会录制或操作主屏。", false);
        }
        enqueueTask(goal);
    }

    private void enqueueTask(String goal) {
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
            File temp = new File(directory, "pending.tmp");
            File target = new File(directory, "pending.json");
            try (FileWriter writer = new FileWriter(temp)) {
                writer.write(task.toString(2));
                writer.write("\n");
            }
            if (!temp.renameTo(target)) {
                throw new IllegalStateException("Could not publish task");
            }
            progressTaskId = taskId;
            progressBubble = addBubble("已收到任务，正在等待 Agent 开始…", false);
        } catch (Exception error) {
            addBubble("任务提交失败：" + error.getMessage(), false);
        }
    }

    private void renderLatestTaskUpdate() {
        SharedPreferences prefs = getSharedPreferences(
                AgentResultReceiver.PREFS, MODE_PRIVATE);
        String taskId = prefs.getString(AgentResultReceiver.KEY_TASK_ID, "");
        String status = prefs.getString(AgentResultReceiver.KEY_STATUS, "");
        String message = prefs.getString(AgentResultReceiver.KEY_MESSAGE, "");
        String fingerprint = taskId + "|" + status + "|" + message;
        if (taskId.isEmpty() || fingerprint.equals(renderedUpdate)) {
            return;
        }
        renderedUpdate = fingerprint;
        if (!taskId.equals(progressTaskId) || progressBubble == null) {
            progressTaskId = taskId;
            progressBubble = addBubble(message, false);
            return;
        }
        String prefix;
        switch (status) {
            case "completed": prefix = "✓ "; break;
            case "failed": prefix = "⚠ "; break;
            case "needs_user": prefix = "需要你确认："; break;
            default: prefix = ""; break;
        }
        progressBubble.setText(prefix + message);
        scrollToBottom();
    }

    private TextView addBubble(String value, boolean fromUser) {
        LinearLayout row = new LinearLayout(this);
        row.setGravity(fromUser ? Gravity.END : Gravity.START);
        row.setPadding(0, dp(5), 0, dp(5));

        TextView bubble = text(value, 15,
                fromUser ? Color.WHITE : Color.rgb(30, 41, 59));
        bubble.setLineSpacing(0, 1.15f);
        bubble.setPadding(dp(15), dp(12), dp(15), dp(12));
        bubble.setMaxWidth((int) (getResources().getDisplayMetrics().widthPixels * 0.82f));
        bubble.setBackground(rounded(
                fromUser ? Color.rgb(37, 99, 235) : Color.WHITE, 18));
        row.addView(bubble);
        messages.addView(row, matchWrap());
        scrollToBottom();
        return bubble;
    }

    private void scrollToBottom() {
        // Scroll without moving keyboard focus away from the owner's composer.
        chatScroll.post(() -> chatScroll.smoothScrollTo(0, messages.getHeight()));
    }

    private void refreshAgentStatus() {
        if (isAgentDisplayAlive()) {
            int id = getSharedPreferences(AgentDisplayService.PREFS, MODE_PRIVATE)
                    .getInt(AgentDisplayService.KEY_DISPLAY_ID, -1);
            agentStatus.setText("● 焦点已隔离 · Display " + id);
            agentStatus.setTextColor(Color.rgb(22, 163, 74));
        } else {
            agentStatus.setText("○ 等待电脑连接独立工作区");
            agentStatus.setTextColor(Color.rgb(100, 116, 139));
        }
    }

    private boolean isAgentDisplayAlive() {
        int id = getSharedPreferences(AgentDisplayService.PREFS, MODE_PRIVATE)
                .getInt(AgentDisplayService.KEY_DISPLAY_ID, -1);
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

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
