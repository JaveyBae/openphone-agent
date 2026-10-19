package ai.openphone.agent;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * Local chat history, one JSON file per conversation in the APP's private
 * storage. Nothing here is uploaded; tasks are linked to the conversation
 * that started them so progress never lands in another chat.
 */
final class ConversationStore {
    private static final String OWNERS = "conversation_tasks";
    private final File directory;
    private final SharedPreferences owners;

    ConversationStore(Context context) {
        directory = new File(context.getFilesDir(), "conversations");
        if (!directory.exists()) directory.mkdirs();
        owners = context.getSharedPreferences(OWNERS, Context.MODE_PRIVATE);
    }

    JSONObject create() {
        long now = System.currentTimeMillis();
        try {
            return new JSONObject().put("id", UUID.randomUUID().toString()).put("title", "新对话")
                    .put("createdAt", now).put("updatedAt", now).put("messages", new JSONArray());
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
    }

    JSONObject load(String id) {
        if (id == null || !id.matches("[0-9a-f-]{36}")) return null;
        try {
            File file = new File(directory, id + ".json");
            return file.exists()
                    ? new JSONObject(new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8)) : null;
        } catch (Exception error) {
            return null;
        }
    }

    /** Empty chats are not kept, like a fresh "new chat" that was never used. */
    void save(JSONObject conversation) {
        JSONArray entries = conversation.optJSONArray("messages");
        if (entries == null || entries.length() == 0) return;
        try {
            conversation.put("updatedAt", System.currentTimeMillis());
            String id = conversation.getString("id");
            File temp = new File(directory, id + ".tmp");
            try (FileWriter writer = new FileWriter(temp)) {
                writer.write(conversation.toString());
            }
            if (!temp.renameTo(new File(directory, id + ".json"))) temp.delete();
        } catch (Exception ignored) {
            // History is a convenience; a failed write must never break a task.
        }
    }

    /** Newest first. */
    List<JSONObject> list() {
        List<JSONObject> result = new ArrayList<>();
        File[] files = directory.listFiles((dir, name) -> name.endsWith(".json"));
        if (files == null) return result;
        for (File file : files) {
            JSONObject conversation = load(file.getName().replace(".json", ""));
            if (conversation != null) result.add(conversation);
        }
        result.sort((a, b) -> Long.compare(b.optLong("updatedAt"), a.optLong("updatedAt")));
        return result;
    }

    void delete(String id) {
        if (id != null && id.matches("[0-9a-f-]{36}")) new File(directory, id + ".json").delete();
    }

    void bindTask(String taskId, String conversationId) {
        owners.edit().putString(taskId, conversationId).apply();
    }

    String ownerOf(String taskId) {
        return owners.getString(taskId, null);
    }

    /** Store a task's latest public state inside the conversation that owns it. */
    static boolean updateTask(JSONObject conversation, String taskId, String status, String message,
                              String progress, long elapsedSeconds) {
        JSONArray entries = conversation.optJSONArray("messages");
        if (entries == null) return false;
        for (int i = entries.length() - 1; i >= 0; i--) {
            JSONObject entry = entries.optJSONObject(i);
            if (entry != null && "task".equals(entry.optString("role")) && taskId.equals(entry.optString("taskId"))) {
                try {
                    entry.put("status", status).put("message", message).put("progress", progress);
                    if (elapsedSeconds > 0) entry.put("elapsed", elapsedSeconds);
                } catch (Exception ignored) {
                    return false;
                }
                return true;
            }
        }
        return false;
    }
}
