package ai.openphone.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Base64;

import java.nio.charset.StandardCharsets;

/** Receives task progress from the authorized host-side controller. */
public class AgentResultReceiver extends BroadcastReceiver {
    public static final String ACTION_TASK_UPDATE = "ai.openphone.agent.TASK_UPDATE";
    public static final String PREFS = "agent_task_updates";
    public static final String KEY_TASK_ID = "task_id";
    public static final String KEY_STATUS = "status";
    public static final String KEY_MESSAGE = "message";
    public static final String KEY_MESSAGE_BASE64 = "message_b64";

    @Override
    public void onReceive(Context context, Intent intent) {
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
