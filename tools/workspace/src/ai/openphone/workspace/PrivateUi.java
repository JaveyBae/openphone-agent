package ai.openphone.workspace;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.content.Context;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.util.Base64;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Scoped accessibility transport for skill replay on the PRIVATE display:
 * find a control by identity (id / text / description), never by coordinates,
 * then click, set text with exact readback, or scroll. Allowlisted apps only.
 *
 * One-shot:  PrivateUi <display> <session> <app> <mode> [selectorB64] [textB64]
 * Resident:  PrivateUi <display> <session> serve
 *   reads one JSON command per line from stdin ({"app","mode","selector","text"}),
 *   answers one JSON line each, and exits when the adb connection closes.
 *   No network socket: only the authorized adb session can send commands.
 */
public final class PrivateUi {
    private static final String ALLOWED = "com\\.google\\.android\\.calendar|org\\.fossify\\.calendar|com\\.google\\.android\\.gm"
            + "|com\\.google\\.android\\.apps\\.messaging"
            + "|ai\\.openphone\\.agent"  // the Agent browser
            + "|com\\.google\\.android\\.deskclock"
            + "|com\\.google\\.android\\.apps\\.maps"
            + "|com\\.google\\.android\\.contacts"
            + "|com\\.MobileTicket";
    /** Apps where a skill may only ever prepare a draft: send-like controls are refused outright. */
    private static final String DRAFT_ONLY = "com\\.google\\.android\\.apps\\.messaging|com\\.google\\.android\\.gm";
    private static final String MODES = "snapshot|click|text|scroll|scroll_back|open";
    private static final int MAX_COMMANDS = 2000;
    private static final java.util.regex.Pattern SEND_WORDS =
            java.util.regex.Pattern.compile("(?<![a-z])(send|schedule|delete|trash|archive)(?![a-z])");

    private final int display;
    private final String session;
    private final Context context;
    private final UiAutomation ui;
    private final AccessibilityServiceInfo info;
    private String app = "";

    private PrivateUi(int display, String session, Context context, UiAutomation ui) {
        this.display = display; this.session = session; this.context = context; this.ui = ui;
        this.info = ui.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
    }

    public static void main(String[] args) {
        UiAutomation ui = null;
        HandlerThread callback = null;
        try {
            if (Process.myUid() != 2000 || args.length < 3 || args.length > 6)
                throw new SecurityException("Authorized shell arguments required");
            int display = Integer.parseInt(args[0]);
            String session = args[1];
            if (display <= 0 || !session.matches("[0-9a-f-]{36}"))
                throw new SecurityException("Private display/session required");
            boolean serve = args.length == 3 && args[2].equals("serve");
            if (!serve && args.length < 4) throw new IllegalArgumentException("Unknown private UI operation");
            Looper.prepareMainLooper();
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object thread = at.getMethod("systemMain").invoke(null);
            Context context = (Context) at.getMethod("getSystemContext").invoke(thread);
            PrivateTextInput.verifyWorkspace(context, display, session);
            // A resident process may start while the app is still loading; every
            // command re-checks focus itself, so only one-shot calls check it here.
            if (!serve) PrivateTextInput.verifyFocus(display);
            callback = new HandlerThread("private-ui"); callback.start();
            Class<?> ci = Class.forName("android.app.IUiAutomationConnection");
            Object connection = Class.forName("android.app.UiAutomationConnection").getConstructor().newInstance();
            ui = (UiAutomation) UiAutomation.class.getConstructor(Looper.class, ci).newInstance(callback.getLooper(), connection);
            UiAutomation.class.getMethod("connect", int.class).invoke(ui, UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            PrivateUi transport = new PrivateUi(display, session, context, ui);
            if (serve) {
                transport.serve();
            } else {
                JSONObject selector = args.length > 4 ? new JSONObject(decode(args[4])) : null;
                String text = args.length > 5 ? decode(args[5]) : null;
                System.out.println(transport.respond(args[2], args[3], selector, text));
            }
        } catch (Throwable error) {
            System.out.println(failure(error));
        } finally {
            if (ui != null) try { UiAutomation.class.getMethod("disconnect").invoke(ui); } catch (Exception ignored) { }
            if (callback != null) callback.quitSafely();
            System.exit(0);
        }
    }

    /** Resident mode: one connection, many commands, every one fully re-checked. */
    private void serve() throws Exception {
        System.out.println(new JSONObject().put("ok", true).put("ready", true)
                .put("displayId", display).put("session", session));
        System.out.flush();
        BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        for (int count = 0; count < MAX_COMMANDS; count++) {
            String line = input.readLine();
            if (line == null) return;  // adb connection closed: leave nothing running
            JSONObject reply;
            try {
                JSONObject command = new JSONObject(line);
                reply = respond(command.getString("app"), command.getString("mode"),
                        command.isNull("selector") ? null : command.getJSONObject("selector"),
                        command.isNull("text") ? null : command.getString("text"));
            } catch (Throwable error) {
                reply = failure(error);
            }
            System.out.println(reply);
            System.out.flush();
        }
    }

    private static JSONObject failure(Throwable error) {
        Throwable cause = error.getCause() == null ? error : error.getCause();
        try { return new JSONObject().put("ok", false).put("error", cause.toString()); }
        catch (Exception ignored) { return new JSONObject(); }
    }

    private JSONObject respond(String app, String mode, JSONObject selector, String value) {
        try {
            return perform(app, mode, selector, value).put("ok", true).put("verified", true);
        } catch (Throwable error) {
            return failure(error);
        }
    }

    /** One operation with the same checks as the original single-shot helper. */
    private JSONObject perform(String requestedApp, String mode, JSONObject selector, String value) throws Exception {
        if (!requestedApp.matches(ALLOWED)) throw new SecurityException("App not allowed for skill replay");
        if (!mode.matches(MODES) || (mode.equals("snapshot") != (selector == null))
                || (mode.equals("text") != (value != null)))
            throw new IllegalArgumentException("Unknown private UI operation");
        if (!requestedApp.equals(app)) {
            app = requestedApp;
            info.packageNames = new String[]{app};
            ui.setServiceInfo(info);
        }
        PrivateTextInput.verifyWorkspace(context, display, session);
        PrivateTextInput.verifyFocus(display);
        if (selector != null) {
            if (selector.length() == 0) throw new IllegalArgumentException("Empty selector");
            java.util.Iterator<String> keys = selector.keys();
            while (keys.hasNext()) {
                if (!keys.next().matches("id|text|description|class|textStartsWith|descriptionStartsWith|descriptionEndsWith|scrollable"))
                    throw new IllegalArgumentException("Unsupported selector");
            }
        }
        List<AccessibilityNodeInfo> found = new ArrayList<>();
        JSONArray nodes = new JSONArray();
        for (int retry = 0; retry < 12; retry++) {
            found.clear(); nodes = new JSONArray();
            List<AccessibilityWindowInfo> windows = ui.getWindowsOnAllDisplays().get(display);
            if (windows != null) for (AccessibilityWindowInfo window : windows) {
                if (window.getDisplayId() != display || !window.isFocused()
                        || window.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) continue;
                AccessibilityNodeInfo root = window.getRoot();
                if (root != null) visit(root, selector, found, nodes, 0, new int[]{0});
            }
            if (mode.equals("snapshot") && nodes.length() > 0 || found.size() > 0) break;
            Thread.sleep(150);
        }
        JSONObject result = new JSONObject().put("displayId", display).put("session", session).put("package", app);
        if (mode.equals("snapshot")) {
            if (nodes.length() == 0) throw new IllegalStateException("No focused private app window");
            result.put("nodes", nodes);
        } else {
            if (found.size() != 1) throw new IllegalStateException("Selector matches " + found.size() + " nodes");
            AccessibilityNodeInfo node = found.get(0);
            PrivateTextInput.verifyWorkspace(context, display, session); PrivateTextInput.verifyFocus(display);
            if (!node.refresh() || !matches(node, selector) || !node.isEnabled() || !node.isVisibleToUser())
                throw new SecurityException("Private control changed");
            if (mode.equals("open") || mode.equals("click")) {
                // Labels often sit inside a clickable row/button: use its nearest clickable ancestor.
                for (int i=0; i<4 && !node.isClickable(); i++) {
                    node = node.getParent();
                    if (node == null || !app.contentEquals(node.getPackageName() == null ? "" : node.getPackageName()))
                        throw new SecurityException("No clickable private control");
                }
            }
            AccessibilityWindowInfo window = node.getWindow();
            if (window == null || window.getDisplayId() != display || !window.isFocused())
                throw new SecurityException("Wrong private window");
            if (!mode.equals("text") && app.matches(DRAFT_ONLY) && sendLike(node))
                throw new SecurityException("Draft-only app: send/delete control refused; owner sends manually");
            if (!mode.equals("text")) {
                boolean scroll = mode.equals("scroll") || mode.equals("scroll_back");
                int action = mode.equals("scroll") ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                        : mode.equals("scroll_back") ? AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD : AccessibilityNodeInfo.ACTION_CLICK;
                if (!(scroll ? node.isScrollable() : node.isClickable()) || !node.performAction(action))
                    throw new IllegalStateException("Private control rejected action");
                Thread.sleep(350); // Allow a new window to replace its predecessor.
            } else {
                if (value.isEmpty() || value.codePointCount(0,value.length()) > 500
                        || value.codePoints().anyMatch(Character::isISOControl) || node.isPassword() || !node.isEditable())
                    throw new SecurityException("Invalid private editor/text");
                Bundle b = new Bundle(); b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,value);
                node.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
                if (!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,b))
                    throw new IllegalStateException("Private editor rejected text");
                boolean readback = false;
                for (int i=0;i<12;i++) {
                    if (node.refresh() && value.contentEquals(node.getText() == null ? "" : node.getText())) { readback=true; break; }
                    Thread.sleep(100);
                }
                if (!readback) throw new IllegalStateException("Private text readback mismatch");
                result.put("readback",true);
            }
        }
        // Navigation replaces the private focused window asynchronously.
        // Wait only for that transition; never change focus or replay a click.
        for (int retry=0; ; retry++) {
            PrivateTextInput.verifyWorkspace(context, display, session);
            try { PrivateTextInput.verifyFocus(display); break; }
            catch (SecurityException error) {
                if (retry>=11 || !"Missing private focused window".equals(error.getMessage())) throw error;
                Thread.sleep(150);
            }
        }
        return result;
    }

    /** The control or any of its children names sending, scheduling or deleting. */
    private static boolean sendLike(AccessibilityNodeInfo node) {
        String id = String.valueOf(node.getViewIdResourceName()).toLowerCase(java.util.Locale.ROOT);
        String text = String.valueOf(node.getText());
        // The ONLY exception: Messages' recipient picker row "Send to 183492" opens a
        // conversation with that number; it never sends a message.
        if (text.matches("Send to \\+?[0-9][0-9 ()\\-]{2,24}") && node.getContentDescription() == null) text = "";
        String label = (text + " " + String.valueOf(node.getContentDescription())).toLowerCase(java.util.Locale.ROOT);
        // Whole English words only ("senders" or "sender image" are not a Send button);
        // Chinese terms match anywhere. The resource id counts too, e.g. ":id/send".
        String name = id.contains(":id/") ? id.substring(id.indexOf(":id/") + 4) : "";
        if (SEND_WORDS.matcher(label).find() || SEND_WORDS.matcher(name).find()) return true;
        for (String word : new String[]{"发送", "删除", "归档", "移至垃圾"}) {
            if (label.contains(word)) return true;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null && sendLike(child)) return true;
        }
        return false;
    }

    private static String decode(String s) { return new String(Base64.decode(s,Base64.NO_WRAP),StandardCharsets.UTF_8); }

    private static boolean matches(AccessibilityNodeInfo n, JSONObject s) {
        return (!s.has("scrollable") || s.optBoolean("scrollable") == n.isScrollable())
                && (!s.has("class") || s.optString("class").equals(String.valueOf(n.getClassName())))
                && (!s.has("id") || s.optString("id").equals(String.valueOf(n.getViewIdResourceName())))
                && (!s.has("text") || s.optString("text").equals(String.valueOf(n.getText())))
                && (!s.has("description") || s.optString("description").equals(String.valueOf(n.getContentDescription())))
                && (!s.has("textStartsWith") || String.valueOf(n.getText()).startsWith(s.optString("textStartsWith")))
                && (!s.has("descriptionStartsWith")
                    || String.valueOf(n.getContentDescription()).startsWith(s.optString("descriptionStartsWith")))
                && (!s.has("descriptionEndsWith")
                    || String.valueOf(n.getContentDescription()).endsWith(s.optString("descriptionEndsWith")));
    }

    private void visit(AccessibilityNodeInfo n, JSONObject selector, List<AccessibilityNodeInfo> found,
                       JSONArray nodes, int depth, int[] count) throws Exception {
        if (depth>40 || ++count[0]>1500) throw new IllegalStateException("Private UI tree limit");
        if (!app.contentEquals(n.getPackageName() == null ? "" : n.getPackageName()) || !n.isVisibleToUser()) return;
        Rect r = new Rect(); n.getBoundsInScreen(r);
        nodes.put(new JSONObject().put("id",n.getViewIdResourceName()).put("text",n.getText())
                .put("description",n.getContentDescription()).put("class",n.getClassName())
                .put("clickable",n.isClickable()).put("editable",n.isEditable()).put("checked",n.isChecked())
                .put("focused",n.isFocused()).put("scrollable",n.isScrollable())
                .put("bounds",r.toShortString()));
        if (selector!=null && matches(n,selector)) found.add(n);
        for (int i=0;i<n.getChildCount();i++) { AccessibilityNodeInfo child=n.getChild(i); if(child!=null) visit(child,selector,found,nodes,depth+1,count); }
    }
}
