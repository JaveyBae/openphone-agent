import ai.openphone.workspace.SmsControlPolicy;

public final class SmsControlPolicyTest {
    static void check(boolean expected, boolean banner, String cls, boolean blank, boolean closeChildren, int children,
                      int l, int t, int r, int b) {
        boolean actual = SmsControlPolicy.isSaveBannerClose(banner, cls, blank, closeChildren, children,
                l, t, r, b, 8, 136, 712, 432);
        if (actual != expected) throw new AssertionError("Unsafe banner-control match");
    }
    public static void main(String[] args) {
        check(true, true, "android.view.View", true, true, 2, 616, 144, 712, 240);
        check(false, false, "android.view.View", true, true, 2, 616, 144, 712, 240);
        check(false, true, "android.view.View", false, true, 2, 616, 144, 712, 240);
        check(false, true, "android.widget.Button", true, true, 2, 616, 144, 712, 240);
        check(false, true, "android.view.View", true, true, 1, 616, 144, 712, 240);
        check(false, true, "android.view.View", true, false, 2, 616, 144, 712, 240);
        check(false, true, "android.view.View", true, true, 0, 616, 144, 712, 240);
        check(false, true, "android.view.View", true, true, 2, 8, 136, 712, 432);
        check(false, true, "android.view.View", true, true, 2, 600, 352, 712, 432);
        check(false, true, "android.view.View", true, true, 2, 16, 144, 112, 240);
        check(false, true, "android.view.View", true, true, 2, 616, 1170, 712, 1266);
    }
}
