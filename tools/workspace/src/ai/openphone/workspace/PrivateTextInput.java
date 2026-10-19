package ai.openphone.workspace;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.content.Context;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Bundle;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.util.Base64;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.FileInputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Short-lived, shell-authorized Unicode input. Never uses IME or clipboard. */
public final class PrivateTextInput {
    private static final String DIRECTORY = "/data/local/tmp/openphone-workspace";
    private static final int REQUIRED_FLAGS = (1 << 7) | (1 << 11) | (1 << 12) | (1 << 3);

    public static void main(String[] args) {
        UiAutomation automation = null;
        HandlerThread callbacks = null;
        try {
            boolean smsTap = args.length == 6 && ("sms-tap".equals(args[3]) || "sms-check".equals(args[3]));
            boolean mailCheck = args.length == 6 && "mail-check".equals(args[3]);
            boolean orderCheck = args.length == 6 && "order-check".equals(args[3]);
            if (Process.myUid() != 2000 || Build.VERSION.SDK_INT < 35
                    || (args.length != 4 && !smsTap && !mailCheck && !orderCheck)) {
                    throw new SecurityException("Requires authorized Android 15 ADB shell and valid arguments");
            }
            int displayId = Integer.parseInt(args[0]);
            String session = args[1], expectedPackage = args[2];
            if (displayId <= 0 || !session.matches("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
                    || !expectedPackage.matches("ai\\.openphone\\.agent|com\\.jingdong\\.app\\.mall|com\\.taobao\\.taobao|com\\.xunmeng\\.pinduoduo|com\\.google\\.android\\.apps\\.messaging|org\\.fossify\\.calendar|com\\.google\\.android\\.calendar|com\\.google\\.android\\.gm|com\\.sankuai\\.meituan|com\\.google\\.android\\.deskclock|com\\.google\\.android\\.apps\\.maps|com\\.google\\.android\\.contacts|com\\.MobileTicket")) {
                throw new SecurityException("Invalid private input target");
            }
            String text = smsTap || mailCheck || orderCheck ? "" : new String(Base64.decode(args[3], Base64.NO_WRAP), StandardCharsets.UTF_8);
            if (!smsTap && !mailCheck && !orderCheck && (text.isEmpty() || text.codePointCount(0, text.length()) > 500
                    || text.codePoints().anyMatch(c -> Character.isISOControl(c)))) {
                throw new IllegalArgumentException("Text must contain 1..500 characters without control characters");
            }
            Looper.prepareMainLooper();
            Class<?> activityThread = Class.forName("android.app.ActivityThread");
            Object thread = activityThread.getMethod("systemMain").invoke(null);
            Context context = (Context) activityThread.getMethod("getSystemContext").invoke(thread);
            verifyWorkspace(context, displayId, session);
            verifyFocus(displayId);
            callbacks = new HandlerThread("openphone-private-text");
            callbacks.start();
            Class<?> connectionInterface = Class.forName("android.app.IUiAutomationConnection");
            Object connection = Class.forName("android.app.UiAutomationConnection").getConstructor().newInstance();
            automation = (UiAutomation) UiAutomation.class.getConstructor(Looper.class, connectionInterface)
                    .newInstance(callbacks.getLooper(), connection);
            UiAutomation.class.getMethod("connect", int.class).invoke(automation,
                    UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            AccessibilityServiceInfo info = automation.getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            info.packageNames = new String[]{expectedPackage};
            info.eventTypes = AccessibilityEvent.TYPE_WINDOWS_CHANGED
                    | AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                    | AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED;
            automation.setServiceInfo(info);

            if (smsTap) {
                if (!"com.google.android.apps.messaging".equals(expectedPackage)) {
                    throw new SecurityException("SMS safety guard: wrong application");
                }
                guardedSmsTap(automation, context, displayId, session, expectedPackage,
                        Integer.parseInt(args[4]), Integer.parseInt(args[5]), "sms-check".equals(args[3]));
                return;
            }

            if (orderCheck) {
                if (!"com.sankuai.meituan".equals(expectedPackage) && !"com.MobileTicket".equals(expectedPackage)
                        && !"ai.openphone.agent".equals(expectedPackage)) {
                    throw new SecurityException("Order safety guard: wrong application");
                }
                orderPageCheck(automation, context, displayId, session, expectedPackage);
                return;
            }

            if (mailCheck) {
                if (!"com.google.android.gm".equals(expectedPackage)) {
                    throw new SecurityException("Mail safety guard: wrong application");
                }
                mailControlCheck(automation, context, displayId, session, expectedPackage,
                        Integer.parseInt(args[4]), Integer.parseInt(args[5]));
                return;
            }

            AccessibilityNodeInfo target = null;
            // Only inspect roots on the specified private display. Never use
            // getRootInActiveWindow(), which may select the owner's main screen.
            for (int retry = 0; retry < 12 && target == null; retry++) {
                List<AccessibilityWindowInfo> windows = automation.getWindowsOnAllDisplays().get(displayId);
                List<AccessibilityNodeInfo> candidates = new ArrayList<>();
                if (windows != null) {
                    for (AccessibilityWindowInfo window : windows) {
                        if (window.getDisplayId() != displayId
                                || window.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) continue;
                        AccessibilityNodeInfo root = window.getRoot();
                        if (root == null || !packageMatches(root, expectedPackage)) continue;
                        collect(root, expectedPackage, candidates, 0, new int[]{0});
                    }
                }
                if (candidates.size() > 1) throw new IllegalStateException("Ambiguous private input fields");
                if (candidates.size() == 1) target = candidates.get(0);
                else Thread.sleep(150);
            }
            if (target == null) throw new IllegalStateException("No focused editable private field supports SET_TEXT");
            verifyWorkspace(context, displayId, session);
            verifyFocus(displayId);
            if (!target.refresh() || !eligible(target, expectedPackage)) {
                throw new IllegalStateException("Private input field changed before writing");
            }
            AccessibilityWindowInfo targetWindow = target.getWindow();
            if (targetWindow == null || targetWindow.getDisplayId() != displayId || !targetWindow.isFocused()) {
                throw new SecurityException("Input node is not on the verified private display");
            }
            Rect bounds = new Rect();
            target.getBoundsInScreen(bounds);
            Bundle arguments = new Bundle();
            arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            if (!target.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_TEXT.getId(), arguments)) {
                throw new IllegalStateException("Private field rejected SET_TEXT");
            }
            boolean verified = false;
            for (int retry = 0; retry < 12; retry++) {
                verifyWorkspace(context, displayId, session);
                verifyFocus(displayId);
                if (target.refresh() && eligible(target, expectedPackage) && text.contentEquals(target.getText())) {
                    AccessibilityWindowInfo window = target.getWindow();
                    if (window == null || window.getDisplayId() != displayId || !window.isFocused()) {
                        throw new SecurityException("Private input window changed during readback");
                    }
                    verified = true;
                    break;
                }
                Thread.sleep(100);
            }
            if (!verified) throw new IllegalStateException("Private text readback did not match; not retrying input");
            System.out.println(new JSONObject().put("ok", true).put("displayId", displayId)
                    .put("session", session).put("package", expectedPackage)
                    .put("method", "accessibility_set_text").put("verified", true)
                    .put("characters", text.codePointCount(0, text.length()))
                    .put("bounds", bounds.toShortString()));
        } catch (Throwable error) {
            Throwable cause = error.getCause() == null ? error : error.getCause();
            try { System.out.println(new JSONObject().put("ok", false)
                    .put("error", cause.getClass().getSimpleName() + ": " + cause.getMessage())); }
            catch (Exception ignored) { System.out.println("{\"ok\":false}"); }
        } finally {
            if (automation != null) {
                try { UiAutomation.class.getMethod("disconnect").invoke(automation); }
                catch (Exception ignored) { }
            }
            if (callbacks != null) callbacks.quitSafely();
            System.exit(0);
        }
    }

    private static boolean eligible(AccessibilityNodeInfo node, String expectedPackage) {
        return packageMatches(node, expectedPackage) && node.isFocused()
                && node.isEditable() && node.isEnabled() && node.isVisibleToUser() && !node.isPassword()
                && node.getActionList().contains(AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_TEXT);
    }

    private static void guardedSmsTap(UiAutomation automation, Context context, int displayId,
                                      String session, String expectedPackage, int x, int y, boolean checkOnly) throws Exception {
        List<AccessibilityNodeInfo> candidates = new ArrayList<>();
        for (int retry = 0; retry < 12 && candidates.isEmpty(); retry++) {
            List<AccessibilityWindowInfo> windows = automation.getWindowsOnAllDisplays().get(displayId);
            if (windows != null) {
                for (AccessibilityWindowInfo window : windows) {
                    if (window.getDisplayId() != displayId || !window.isFocused()
                            || window.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) continue;
                    AccessibilityNodeInfo root = window.getRoot();
                    if (root != null) collectTapTargets(root, expectedPackage, x, y, candidates, 0, new int[]{0});
                }
            }
            if (candidates.isEmpty()) Thread.sleep(150);
        }
        AccessibilityNodeInfo target = null;
        long smallest = Long.MAX_VALUE;
        for (AccessibilityNodeInfo node : candidates) {
            Rect rect = new Rect();
            node.getBoundsInScreen(rect);
            long area = (long) rect.width() * rect.height();
            if (area < smallest) { smallest = area; target = node; }
        }
        boolean allowed = target != null && safeSmsControl(target);
        if (checkOnly) {
            verifyWorkspace(context, displayId, session);
            verifyFocus(displayId);
            System.out.println(new JSONObject().put("ok", true).put("allowed", allowed)
                    .put("displayId", displayId).put("session", session).put("package", expectedPackage)
                    .put("method", "sms_control_check").put("executed", false)
                    .put("control", target == null ? JSONObject.NULL : describeControl(target)));
            return;
        }
        if (!allowed) {
            verifyWorkspace(context, displayId, session);
            verifyFocus(displayId);
            // A rejected click did not mutate the UI. Report a bounded,
            // machine-readable outcome so the planner can choose another SAFE
            // route, never convert it to a raw-coordinate fallback.
            System.out.println(new JSONObject().put("ok", false).put("verified", true)
                    .put("executed", false).put("displayId", displayId).put("session", session)
                    .put("package", expectedPackage).put("method", "sms_control_check")
                    .put("blockedControl", target == null ? "no_target"
                            : sendControl(target) ? "send_control" : "unknown_control")
                    .put("error", "SMS safety guard: rejected private control; no click performed"));
            return;
        }
        verifyWorkspace(context, displayId, session);
        verifyFocus(displayId);
        if (!target.refresh() || !safeSmsControl(target)) {
            throw new SecurityException("SMS safety guard: control changed before action");
        }
        AccessibilityWindowInfo window = target.getWindow();
        if (window == null || window.getDisplayId() != displayId || !window.isFocused()) {
            throw new SecurityException("SMS safety guard: wrong private window");
        }
        // Execute the inspected node, not a later raw coordinate tap that could
        // hit a newly appeared Send button. Unknown controls always fail closed.
        if (!target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            throw new IllegalStateException("SMS safety guard: safe control rejected click");
        }
        verifyWorkspace(context, displayId, session);
        verifyFocus(displayId);
        boolean privateEditableFocused = target.refresh() && eligible(target, expectedPackage);
        System.out.println(new JSONObject().put("ok", true).put("verified", true)
                .put("displayId", displayId).put("session", session).put("package", expectedPackage)
                .put("privateEditableFocused", privateEditableFocused)
                .put("method", "guarded_sms_click"));
    }

    /**
     * Check-only: report whether the clickable control under (x, y) in the
     * private Gmail window may send, delete or move mail. Never clicks.
     */
    private static void mailControlCheck(UiAutomation automation, Context context, int displayId,
                                         String session, String expectedPackage, int x, int y) throws Exception {
        List<AccessibilityNodeInfo> candidates = new ArrayList<>();
        for (int retry = 0; retry < 6 && candidates.isEmpty(); retry++) {
            List<AccessibilityWindowInfo> windows = automation.getWindowsOnAllDisplays().get(displayId);
            if (windows != null) {
                for (AccessibilityWindowInfo window : windows) {
                    if (window.getDisplayId() != displayId || !window.isFocused()
                            || window.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) continue;
                    AccessibilityNodeInfo root = window.getRoot();
                    if (root != null) collectTapTargets(root, expectedPackage, x, y, candidates, 0, new int[]{0});
                }
            }
            if (candidates.isEmpty()) Thread.sleep(150);
        }
        // Any clickable node under the point counts: a Send icon may sit inside
        // a larger clickable toolbar, and either one would receive the tap.
        AccessibilityNodeInfo blocked = null;
        for (AccessibilityNodeInfo node : candidates) {
            if (sendControl(node) || destructiveMailControl(node)) { blocked = node; break; }
        }
        verifyWorkspace(context, displayId, session);
        verifyFocus(displayId);
        System.out.println(new JSONObject().put("ok", true).put("verified", true)
                .put("allowed", blocked == null).put("executed", false)
                .put("displayId", displayId).put("session", session).put("package", expectedPackage)
                .put("method", "mail_control_check").put("candidates", candidates.size())
                .put("control", blocked == null ? JSONObject.NULL : describeControl(blocked)));
    }

    /** Words of pages only the owner may act on: placing/paying for an order, agreeing to
     *  terms or privacy policies, and logging in with a code. The Agent stops on such a page. */
    private static final String[] ORDER_WORDS = {"提交订单", "去支付", "立即支付", "确认支付", "确认付款", "立即付款",
            "立即下单", "免密支付", "支付方式", "极速支付", "submit order", "place order", "pay now",
            "用户协议", "隐私政策", "服务协议", "获取验证码", "验证码登录", "手机号登录", "一键登录",
            "全部接受", "全部拒绝", "接受全部", "拒绝全部", "accept all", "reject all", "预订选项", "book with"};

    /** Page-level check: a self-drawn control's own label may be missing, so any order/pay
     *  wording visible anywhere in the app's focused window stops every action on the page. */
    private static void orderPageCheck(UiAutomation automation, Context context, int displayId,
                                       String session, String expectedPackage) throws Exception {
        String found = null;
        int windowsSeen = 0;
        for (int retry = 0; retry < 6 && windowsSeen == 0; retry++) {
            List<AccessibilityWindowInfo> windows = automation.getWindowsOnAllDisplays().get(displayId);
            if (windows != null) {
                for (AccessibilityWindowInfo window : windows) {
                    if (window.getDisplayId() != displayId || window.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) continue;
                    AccessibilityNodeInfo root = window.getRoot();
                    if (root == null) continue;
                    windowsSeen++;
                    String word = orderWord(root, expectedPackage, 0, new int[]{0});
                    if (word != null && found == null) found = word;
                }
            }
            if (windowsSeen == 0) Thread.sleep(150);
        }
        if (windowsSeen == 0) throw new IllegalStateException("Order safety guard: no readable app window");
        verifyWorkspace(context, displayId, session);
        verifyFocus(displayId);
        System.out.println(new JSONObject().put("ok", true).put("verified", true)
                .put("allowed", found == null).put("executed", false)
                .put("displayId", displayId).put("session", session).put("package", expectedPackage)
                .put("method", "order_page_check").put("word", found == null ? JSONObject.NULL : found));
    }

    private static String orderWord(AccessibilityNodeInfo node, String expectedPackage, int depth, int[] count) {
        if (depth > 60 || ++count[0] > 4000) throw new IllegalStateException("Private UI exceeds limits");
        if (!packageMatches(node, expectedPackage)) return null;
        String label = ((node.getText() == null ? "" : node.getText().toString()) + " "
                + (node.getContentDescription() == null ? "" : node.getContentDescription().toString()))
                .toLowerCase(java.util.Locale.ROOT).replaceAll("\\s+", "");
        for (String word : ORDER_WORDS) {
            if (label.contains(word.replace(" ", ""))) return word;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            String word = child == null ? null : orderWord(child, expectedPackage, depth + 1, count);
            if (word != null) return word;
        }
        return null;
    }

    private static boolean destructiveMailControl(AccessibilityNodeInfo node) {
        String id = String.valueOf(node.getViewIdResourceName()).toLowerCase(java.util.Locale.ROOT);
        String label = ((node.getText() == null ? "" : node.getText().toString()) + " "
                + (node.getContentDescription() == null ? "" : node.getContentDescription().toString()))
                .toLowerCase(java.util.Locale.ROOT);
        for (String word : new String[]{"delete", "trash", "archive", "spam", "删除", "归档", "垃圾"}) {
            if (id.contains(word) || label.contains(word)) return true;
        }
        return false;
    }

    private static void collectTapTargets(AccessibilityNodeInfo node, String expectedPackage,
                                         int x, int y, List<AccessibilityNodeInfo> candidates,
                                         int depth, int[] count) {
        if (depth > 40 || ++count[0] > 1500) throw new IllegalStateException("Private UI exceeds limits");
        if (!packageMatches(node, expectedPackage) || !node.isVisibleToUser()) return;
        Rect rect = new Rect();
        node.getBoundsInScreen(rect);
        if (rect.contains(x, y) && node.isEnabled() && node.isClickable()) candidates.add(node);
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) collectTapTargets(child, expectedPackage, x, y, candidates, depth + 1, count);
        }
    }

    private static boolean safeSmsControl(AccessibilityNodeInfo node) {
        String id = String.valueOf(node.getViewIdResourceName()).toLowerCase(java.util.Locale.ROOT);
        String label = ((node.getText() == null ? "" : node.getText().toString()) + " "
                + (node.getContentDescription() == null ? "" : node.getContentDescription().toString()))
                .trim().toLowerCase(java.util.Locale.ROOT);
        // The recipient picker labels its row "Send to <number>" even though
        // clicking it only selects a recipient. Accept that exact row ONLY in
        // the New conversation picker; the composer Send button stays blocked.
        if (!id.contains("send") && hasRecipientRowLabel(node, 0, new int[]{0})) {
            AccessibilityNodeInfo root = node;
            for (int depth = 0; depth < 40; depth++) {
                AccessibilityNodeInfo parent = root.getParent();
                if (parent == null) break;
                root = parent;
            }
            if (hasPickerTitle(root, 0, new int[]{0})) return true;
        }
        if (id.contains("send") || label.contains("send") || label.contains("发送")) return false;
        if (recipientSuggestion(node)) return true;
        if (saveBannerClose(node)) return true;
        return node.isEditable() || id.matches(".*(?:recipient|contact|start_chat|new_conversation|conversation_list|compose_message_text|message_text|draft).*")
                || label.matches("(?:start chat|new message|next|done|navigate up|back|开始聊天|新建信息|下一步|完成|返回)");
    }

    /** A contact row in the New-conversation suggestion list: tapping it only sets the recipient.
     *  Requires the ContactSuggestionList parent and a phone-label child, so no stray control matches;
     *  a Send id/label is rejected before this is reached. */
    private static boolean recipientSuggestion(AccessibilityNodeInfo node) {
        if (node.isEditable()) return false;
        AccessibilityNodeInfo parent = node.getParent();
        String parentId = parent == null ? "" : String.valueOf(parent.getViewIdResourceName()).toLowerCase(java.util.Locale.ROOT);
        if (!parentId.contains("contactsuggestionlist") && !parentId.contains("suggestion")) return false;
        boolean phoneLabel = false;
        for (int i = 0; i < node.getChildCount() && i < 10; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null || child.getText() == null) continue;
            String text = child.getText().toString().trim();
            if (text.matches("(?i)(Mobile|Home|Work|Main|Other|Phone|\u624b\u673a|\u7535\u8bdd) [()+0-9 -]{3,30}")
                    || text.matches("[()+][0-9 ()-]{5,25}")) phoneLabel = true;
        }
        return phoneLabel;
    }

    private static boolean saveBannerClose(AccessibilityNodeInfo node) {
        AccessibilityNodeInfo parent = node.getParent();
        if (parent == null) return false;
        boolean title = false, explanation = false;
        // The card's title/explanation are direct children, so an unrelated
        // anonymous node elsewhere cannot match an ancestor spanning the app.
        for (int i = 0; i < parent.getChildCount() && i < 20; i++) {
            AccessibilityNodeInfo child = parent.getChild(i);
            if (child == null || !packageMatches(child, "com.google.android.apps.messaging")) continue;
            String text = child.getText() == null ? "" : child.getText().toString();
            title |= text.matches("Save [()+0-9 -]{3,30}\\?");
            explanation |= "Saving this number will add a new contact".equals(text);
        }
        Rect r = new Rect(), card = new Rect();
        node.getBoundsInScreen(r); parent.getBoundsInScreen(card);
        boolean closeIcon = false, anonymousButton = false;
        if (node.getChildCount() == 2) {
            for (int i = 0; i < 2; i++) {
                AccessibilityNodeInfo child = node.getChild(i);
                if (child == null || !packageMatches(child, "com.google.android.apps.messaging")
                        || child.getViewIdResourceName() != null || child.getText() != null) continue;
                closeIcon |= "android.view.View".contentEquals(child.getClassName())
                        && "Close".contentEquals(child.getContentDescription() == null
                                ? "" : child.getContentDescription());
                anonymousButton |= "android.widget.Button".contentEquals(child.getClassName())
                        && child.getContentDescription() == null;
            }
        }
        return SmsControlPolicy.isSaveBannerClose(title && explanation,
                String.valueOf(node.getClassName()), node.getViewIdResourceName() == null
                && node.getText() == null && node.getContentDescription() == null,
                closeIcon && anonymousButton, node.getChildCount(), r.left, r.top, r.right, r.bottom,
                card.left, card.top, card.right, card.bottom);
    }

    private static boolean sendControl(AccessibilityNodeInfo node) {
        String id = String.valueOf(node.getViewIdResourceName()).toLowerCase(java.util.Locale.ROOT);
        String label = ((node.getText() == null ? "" : node.getText().toString()) + " "
                + (node.getContentDescription() == null ? "" : node.getContentDescription().toString()))
                .toLowerCase(java.util.Locale.ROOT);
        return id.contains("send") || label.contains("send") || label.contains("发送");
    }

    private static JSONObject describeControl(AccessibilityNodeInfo node) throws Exception {
        Rect r = new Rect(); node.getBoundsInScreen(r);
        JSONObject value = new JSONObject().put("id", node.getViewIdResourceName())
                .put("class", node.getClassName()).put("bounds", r.toShortString())
                .put("children", node.getChildCount())
                .put("focused", node.isFocused()).put("editable", node.isEditable())
                .put("description", node.getContentDescription()).put("text", node.getText());
        org.json.JSONArray childNodes = new org.json.JSONArray();
        for (int i = 0; i < node.getChildCount() && i < 10; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) childNodes.put(new JSONObject().put("class", child.getClassName())
                    .put("id", child.getViewIdResourceName()).put("text", child.getText())
                    .put("description", child.getContentDescription()));
        }
        value.put("childNodes", childNodes);
        org.json.JSONArray ancestors = new org.json.JSONArray();
        AccessibilityNodeInfo parent = node.getParent();
        for (int depth = 0; depth < 3 && parent != null; depth++, parent = parent.getParent()) {
            Rect bounds = new Rect(); parent.getBoundsInScreen(bounds);
            JSONObject context = new JSONObject().put("id", parent.getViewIdResourceName())
                    .put("class", parent.getClassName()).put("bounds", bounds.toShortString());
            org.json.JSONArray children = new org.json.JSONArray();
            for (int i = 0; i < parent.getChildCount() && i < 20; i++) {
                AccessibilityNodeInfo child = parent.getChild(i);
                if (child != null) children.put(new JSONObject().put("id", child.getViewIdResourceName())
                        .put("text", child.getText()).put("description", child.getContentDescription())
                        .put("class", child.getClassName()));
            }
            context.put("children", children); ancestors.put(context);
        }
        return value.put("ancestors", ancestors);
    }

    private static boolean hasRecipientRowLabel(AccessibilityNodeInfo node, int depth, int[] count) {
        if (depth > 40 || ++count[0] > 1500) return false;
        String text = node.getText() == null ? "" : node.getText().toString().trim();
        if (text.matches("(?i)Send to [()+0-9 -]{3,30}")) return true;
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null && hasRecipientRowLabel(child, depth + 1, count)) return true;
        }
        return false;
    }

    private static boolean hasPickerTitle(AccessibilityNodeInfo node, int depth, int[] count) {
        if (depth > 40 || ++count[0] > 1500) return false;
        String text = node.getText() == null ? "" : node.getText().toString().trim();
        if ("New conversation".equals(text) || "新对话".equals(text) || "新建对话".equals(text)) return true;
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null && hasPickerTitle(child, depth + 1, count)) return true;
        }
        return false;
    }

    private static boolean packageMatches(AccessibilityNodeInfo node, String expectedPackage) {
        CharSequence actual = node.getPackageName();
        return actual != null && expectedPackage.contentEquals(actual);
    }

    private static void collect(AccessibilityNodeInfo node, String expectedPackage,
                                List<AccessibilityNodeInfo> candidates, int depth, int[] count) {
        if (depth > 40 || ++count[0] > 1500) throw new IllegalStateException("Private UI tree exceeds input limits");
        if (!packageMatches(node, expectedPackage)) return;
        if (eligible(node, expectedPackage)) candidates.add(node);
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) collect(child, expectedPackage, candidates, depth + 1, count);
        }
    }

    static void verifyWorkspace(Context context, int displayId, String session) throws Exception {
        JSONObject state;
        try (InputStream input = new FileInputStream(DIRECTORY + "/state.json")) {
            state = new JSONObject(read(input));
        }
        String name = "OpenPhone Isolated Workspace " + session;
        Display display = context.getSystemService(DisplayManager.class).getDisplay(displayId);
        if (!"adb-shell".equals(state.optString("backend")) || state.optInt("displayId", -1) != displayId
                || !session.equals(state.optString("session")) || !name.equals(state.optString("displayName"))
                || display == null || !name.equals(display.getName())
                || (display.getFlags() & REQUIRED_FLAGS) != REQUIRED_FLAGS
                || ((Integer) Display.class.getMethod("getRemoveMode").invoke(display)) != 1) {
            throw new SecurityException("Private workspace changed or lost isolation");
        }
    }

    /** verifyFocusOnce, waiting up to ~1.5 s (never acting) while a window of the private display is being replaced. */
    static void verifyFocus(int displayId) throws Exception {
        for (int retry = 0; ; retry++) {
            try { verifyFocusOnce(displayId); return; }
            catch (SecurityException error) {
                if (retry >= 9 || !"Missing private focused window".equals(error.getMessage())) throw error;
                Thread.sleep(150);
            }
        }
    }

    static void verifyFocusOnce(int displayId) throws Exception {
        java.lang.Process process = new ProcessBuilder("dumpsys", "input").start();
        String dump = read(process.getInputStream());
        if (process.waitFor() != 0 || !Pattern.compile("(?m)^\\s*FocusedDisplayId: 0\\s*$").matcher(dump).find()) {
            throw new SecurityException("Owner focus is not on display 0; no focus switching performed");
        }
        // The owner's display keeps the global focus (checked above). It may briefly have no
        // focused window of its own (Overview, the shade, its app recreating a window): that is
        // the owner using the phone. Typing needs the private display's focused window.
        Matcher windows = Pattern.compile("FocusedWindows:[ \\t]*\\n((?:[ \\t]+displayId=[^\\n]*\\n)*)").matcher(dump);
        if (!windows.find() || !windows.group(1).contains("displayId=" + displayId + ",")) {
            throw new SecurityException("Missing private focused window");
        }
    }

    private static String read(InputStream input) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int bytes;
        while ((bytes = input.read(buffer)) != -1) {
            output.write(buffer, 0, bytes);
            if (output.size() > 2_000_000) throw new IllegalStateException("Input diagnostic too large");
        }
        return new String(output.toByteArray(), StandardCharsets.UTF_8);
    }
}
