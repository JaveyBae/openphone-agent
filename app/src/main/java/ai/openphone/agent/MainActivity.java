package ai.openphone.agent;

import android.Manifest;
import android.app.Activity;
import android.media.projection.MediaProjectionManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.Space;
import android.widget.TextView;

public class MainActivity extends Activity {
    private static final int REQUEST_CAPTURE = 501;
    private TextView statusText;
    private TextView commandText;

    private final BroadcastReceiver statusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            refreshStatus(intent.getStringExtra(AgentDisplayService.EXTRA_MESSAGE));
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildUi());

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7);
        }
        refreshStatus(null);
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
        refreshStatus(null);
    }

    @Override
    protected void onStop() {
        unregisterReceiver(statusReceiver);
        super.onStop();
    }

    private View buildUi() {
        int pad = dp(24);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, dp(28), pad, pad);
        root.setBackgroundColor(Color.rgb(248, 250, 252));

        TextView eyebrow = text("OPENPHONE LAB", 13, Color.rgb(37, 99, 235));
        eyebrow.setLetterSpacing(0.12f);
        root.addView(eyebrow);

        TextView title = text("Your screen stays yours.", 30, Color.rgb(15, 23, 42));
        title.setPadding(0, dp(10), 0, dp(8));
        root.addView(title);

        TextView subtitle = text(
                "This prototype gives the agent a separate Android display. "
                        + "Chrome can work there while you keep using this screen.",
                16,
                Color.rgb(71, 85, 105));
        root.addView(subtitle);

        Space gap = new Space(this);
        root.addView(gap, new LinearLayout.LayoutParams(1, dp(28)));

        statusText = text("Checking…", 18, Color.rgb(15, 23, 42));
        statusText.setPadding(dp(18), dp(18), dp(18), dp(18));
        statusText.setBackgroundColor(Color.WHITE);
        root.addView(statusText, matchWrap());

        root.addView(button("Start agent display", v -> requestCapturePermission()));
        root.addView(button("Refresh agent status", v -> refreshStatus(null)));
        root.addView(button("Stop agent display", v -> send(AgentDisplayService.ACTION_STOP)));

        commandText = text("", 13, Color.rgb(71, 85, 105));
        commandText.setPadding(0, dp(20), 0, 0);
        root.addView(commandText);

        TextView footer = text(
                "Prototype boundary: the Mac uses ADB for agent input. No root and no system-file changes.",
                13,
                Color.rgb(100, 116, 139));
        LinearLayout.LayoutParams footerParams = matchWrap();
        footerParams.weight = 1;
        footerParams.gravity = Gravity.BOTTOM;
        root.addView(footer, footerParams);
        return root;
    }

    private Button button(String label, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(label);
        button.setTextSize(15);
        button.setAllCaps(false);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = matchWrap();
        params.topMargin = dp(12);
        button.setLayoutParams(params);
        return button;
    }

    private TextView text(String value, int sizeSp, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sizeSp);
        view.setTextColor(color);
        view.setLineSpacing(0, 1.15f);
        return view;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void send(String action) {
        Intent intent = new Intent(this, AgentDisplayService.class).setAction(action);
        if (AgentDisplayService.ACTION_STOP.equals(action)) {
            startService(intent);
        } else {
            startForegroundService(intent);
        }
    }

    private void requestCapturePermission() {
        MediaProjectionManager manager =
                (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
        startActivityForResult(manager.createScreenCaptureIntent(), REQUEST_CAPTURE);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_CAPTURE) {
            return;
        }
        if (resultCode != RESULT_OK || data == null) {
            refreshStatus("Screen-capture permission was not granted");
            return;
        }
        Intent service = new Intent(this, AgentDisplayService.class)
                .setAction(AgentDisplayService.ACTION_START)
                .putExtra(AgentDisplayService.EXTRA_RESULT_CODE, resultCode)
                .putExtra(AgentDisplayService.EXTRA_PROJECTION_DATA, data);
        startForegroundService(service);
    }

    private void refreshStatus(String message) {
        int id = getSharedPreferences(AgentDisplayService.PREFS, MODE_PRIVATE)
                .getInt(AgentDisplayService.KEY_DISPLAY_ID, -1);
        String stored = getSharedPreferences(AgentDisplayService.PREFS, MODE_PRIVATE)
                .getString(AgentDisplayService.KEY_STATUS, "Stopped");
        String shown = message != null ? message : stored;
        statusText.setText(id >= 0
                ? "Agent display is running\nDisplay " + id + " · 720 × 1280\n" + shown
                : "Agent display is stopped\n" + shown);
        commandText.setText(id >= 0
                ? "ADB target: display " + id + "\n"
                    + "Mac controller: ./scripts/agentctl.sh open-chrome"
                : "Start the display, then launch Chrome into it.");
    }
}
