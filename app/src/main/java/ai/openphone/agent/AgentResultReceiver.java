package ai.openphone.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.hardware.display.DisplayManager;
import android.view.Display;
import android.util.Base64;

import java.nio.charset.StandardCharsets;

/** Receives task progress from the authorized host-side controller. */
public class AgentResultReceiver extends BroadcastReceiver {
    public static final String ACTION_TASK_UPDATE = "ai.openphone.agent.TASK_UPDATE";
    public static final String ACTION_WORKSPACE_UPDATE = "ai.openphone.agent.WORKSPACE_UPDATE";
    public static final int REQUIRED_DISPLAY_FLAGS = (1 << 7) | (1 << 11) | (1 << 12);
    public static final String PREFS = "agent_task_updates";
    public static final String KEY_TASK_ID = "task_id";
    public static final String KEY_STATUS = "status";
    public static final String KEY_MESSAGE = "message";
    public static final String KEY_MESSAGE_BASE64 = "message_b64";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (ACTION_WORKSPACE_UPDATE.equals(intent.getAction())) {
            int id = intent.getIntExtra("display_id", -1);
            DisplayManager manager = context.getSystemService(DisplayManager.class);
            Display display = id > 0 && manager != null ? manager.getDisplay(id) : null;
            if (display == null || !display.getName().startsWith("OpenPhone Isolated Workspace ")
                    || (display.getFlags() & REQUIRED_DISPLAY_FLAGS) != REQUIRED_DISPLAY_FLAGS) {
                return;
            }
            // Publish the shell backend before releasing the old projection,
            // so its onDestroy cannot overwrite the new workspace state.
            context.getSharedPreferences(AgentDisplayService.PREFS, Context.MODE_PRIVATE)
                    .edit().putString("backend", "adb-shell")
                    .putInt(AgentDisplayService.KEY_DISPLAY_ID, id)
                    .putString(AgentDisplayService.KEY_STATUS, "Focus-isolated workspace")
                    .apply();
            context.stopService(new Intent(context, AgentDisplayService.class));
            context.sendBroadcast(new Intent(AgentDisplayService.ACTION_STATUS)
                    .setPackage(context.getPackageName()));
            return;
        }
        if (!ACTION_TASK_UPDATE.equals(intent.getAction())) {
            return;
        }
        String taskId = intent.getStringExtra(KEY_TASK_ID);
        String status = intent.getStringExtra(KEY_STATUS);
        String message = intent.getStringExtra(KEY_MESSAGE);
        String encodedMessage = intent.getStringExtra(KEY_MESSAGE_BASE64);
        if (encodedMessage != null && !encodedMessage.isBlank()) {
            try {
                message = new String(
                        Base64.decode(encodedMessage, Base64.DEFAULT),
                        StandardCharsets.UTF_8);
            } catch (IllegalArgumentException ignored) {
                message = "Could not decode the agent update";
            }
        }
        if (taskId == null || taskId.isBlank() || status == null || status.isBlank()) {
            return;
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_TASK_ID, taskId)
                .putString(KEY_STATUS, status)
                .putString(KEY_MESSAGE, message == null ? "" : message)
                .apply();
    }
}
