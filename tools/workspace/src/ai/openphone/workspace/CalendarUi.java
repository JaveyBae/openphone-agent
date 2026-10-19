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
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/** Scoped accessibility transport for the offline calendar UI, never a DB writer. */
public final class CalendarUi {
    private static final String APP = "org.fossify.calendar";
    public static void main(String[] args) {
        UiAutomation ui = null;
        HandlerThread callback = null;
        try {
            if (Process.myUid() != 2000 || args.length < 3 || args.length > 5)
                throw new SecurityException("Authorized shell arguments required");
            int display = Integer.parseInt(args[0]);
            String session = args[1], mode = args[2];
            if (display <= 0 || !session.matches("[0-9a-f-]{36}"))
                throw new SecurityException("Private display/session required");
            if (!(mode.equals("snapshot") && args.length == 3)
                    && !(mode.equals("click") && args.length == 4)
                    && !(mode.equals("scroll") && args.length == 4)
                    && !(mode.equals("scroll_back") && args.length == 4)
                    && !(mode.equals("open") && args.length == 4)
                    && !(mode.equals("text") && args.length == 5))
                throw new IllegalArgumentException("Unknown calendar UI operation");
            Looper.prepareMainLooper();
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object thread = at.getMethod("systemMain").invoke(null);
            Context context = (Context) at.getMethod("getSystemContext").invoke(thread);
            PrivateTextInput.verifyWorkspace(context, display, session);
            PrivateTextInput.verifyFocus(display);
            callback = new HandlerThread("calendar-ui"); callback.start();
            Class<?> ci = Class.forName("android.app.IUiAutomationConnection");
            Object connection = Class.forName("android.app.UiAutomationConnection").getConstructor().newInstance();
            ui = (UiAutomation) UiAutomation.class.getConstructor(Looper.class, ci).newInstance(callback.getLooper(), connection);
            UiAutomation.class.getMethod("connect", int.class).invoke(ui, UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            AccessibilityServiceInfo info = ui.getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            info.packageNames = new String[]{APP}; ui.setServiceInfo(info);
            JSONObject selector = mode.equals("snapshot") ? null : new JSONObject(decode(args[3]));
            if (selector != null) {
                if (selector.length() == 0) throw new IllegalArgumentException("Empty selector");
                java.util.Iterator<String> keys = selector.keys();
                while (keys.hasNext()) {
                    String key = keys.next();
                    if (!key.equals("id") && !key.equals("text") && !key.equals("description"))
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
            JSONObject result = new JSONObject().put("displayId", display).put("session", session).put("package", APP);
            if (mode.equals("snapshot")) {
                if (nodes.length() == 0) throw new IllegalStateException("No focused private calendar window");
                result.put("nodes", nodes);
            } else {
                if (found.size() != 1) throw new IllegalStateException("Calendar selector matches " + found.size() + " nodes");
                AccessibilityNodeInfo node = found.get(0);
                PrivateTextInput.verifyWorkspace(context, display, session); PrivateTextInput.verifyFocus(display);
                if (!node.refresh() || !matches(node, selector) || !node.isEnabled() || !node.isVisibleToUser())
                    throw new SecurityException("Calendar control changed");
                if (mode.equals("open")) {
                    if (!selector.has("text")) throw new IllegalArgumentException("Open requires unique visible event text");
                    for (int i=0; i<4 && !node.isClickable(); i++) {
                        node = node.getParent();
                        if (node == null || !APP.contentEquals(node.getPackageName() == null ? "" : node.getPackageName()))
                            throw new SecurityException("No private event row");
                    }
                }
                AccessibilityWindowInfo window = node.getWindow();
                if (window == null || window.getDisplayId() != display || !window.isFocused())
                    throw new SecurityException("Wrong calendar window");
                if (mode.equals("click") || mode.equals("scroll") || mode.equals("scroll_back") || mode.equals("open")) {
                    boolean scroll = mode.equals("scroll") || mode.equals("scroll_back");
                    int action = mode.equals("scroll") ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                            : mode.equals("scroll_back") ? AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD : AccessibilityNodeInfo.ACTION_CLICK;
                    if (!(scroll ? node.isScrollable() : node.isClickable()) || !node.performAction(action))
                        throw new IllegalStateException("Calendar control rejected click");
                    Thread.sleep(350); // Allow a new calendar window to replace its predecessor.
                } else {
                    String value = decode(args[4]);
                    if (value.isEmpty() || value.codePointCount(0,value.length()) > 500
                            || value.codePoints().anyMatch(Character::isISOControl) || node.isPassword() || !node.isEditable())
                        throw new SecurityException("Invalid calendar editor/text");
                    Bundle b = new Bundle(); b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,value);
                    node.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
                    if (!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,b))
                        throw new IllegalStateException("Calendar editor rejected text");
                    boolean readback = false;
                    for (int i=0;i<12;i++) {
                        if (node.refresh() && value.contentEquals(node.getText() == null ? "" : node.getText())) { readback=true; break; }
                        Thread.sleep(100);
                    }
                    if (!readback) throw new IllegalStateException("Calendar text readback mismatch");
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
            System.out.println(result.put("ok",true).put("verified",true));
        } catch (Throwable error) {
            Throwable cause = error.getCause() == null ? error : error.getCause();
            try { System.out.println(new JSONObject().put("ok",false).put("error",cause.toString())); }
            catch (Exception ignored) { System.out.println("{\"ok\":false}"); }
        } finally {
            if (ui != null) try { UiAutomation.class.getMethod("disconnect").invoke(ui); } catch (Exception ignored) { }
            if (callback != null) callback.quitSafely();
            System.exit(0);
        }
    }
    private static String decode(String s) { return new String(Base64.decode(s,Base64.NO_WRAP),StandardCharsets.UTF_8); }
    private static boolean matches(AccessibilityNodeInfo n, JSONObject s) {
        return (!s.has("id") || s.optString("id").equals(String.valueOf(n.getViewIdResourceName())))
                && (!s.has("text") || s.optString("text").equals(String.valueOf(n.getText())))
                && (!s.has("description") || s.optString("description").equals(String.valueOf(n.getContentDescription())));
    }
    private static void visit(AccessibilityNodeInfo n, JSONObject selector, List<AccessibilityNodeInfo> found,
                              JSONArray nodes, int depth, int[] count) throws Exception {
        if (depth>40 || ++count[0]>1500) throw new IllegalStateException("Calendar tree limit");
        if (!APP.contentEquals(n.getPackageName() == null ? "" : n.getPackageName()) || !n.isVisibleToUser()) return;
        Rect r = new Rect(); n.getBoundsInScreen(r);
        nodes.put(new JSONObject().put("id",n.getViewIdResourceName()).put("text",n.getText())
                .put("description",n.getContentDescription()).put("class",n.getClassName())
                .put("clickable",n.isClickable()).put("editable",n.isEditable()).put("checked",n.isChecked())
                .put("bounds",r.toShortString()));
        if (selector!=null && matches(n,selector)) found.add(n);
        for (int i=0;i<n.getChildCount();i++) { AccessibilityNodeInfo child=n.getChild(i); if(child!=null) visit(child,selector,found,nodes,depth+1,count); }
    }
}
