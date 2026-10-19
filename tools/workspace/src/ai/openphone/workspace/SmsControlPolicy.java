package ai.openphone.workspace;

/** Pure, testable geometry constraints for one known Messages banner control. */
public final class SmsControlPolicy {
    private SmsControlPolicy() { }

    public static boolean isSaveBannerClose(boolean knownBanner, String nodeClass,
            boolean unlabeled, boolean verifiedCloseChildren, int children, int left, int top, int right, int bottom,
            int cardLeft, int cardTop, int cardRight, int cardBottom) {
        int w = right - left, h = bottom - top;
        int cw = cardRight - cardLeft, ch = cardBottom - cardTop;
        // Never accept the whole card, its add-contact/report-spam buttons, or
        // another anonymous control elsewhere on the screen. This identifies
        // only the small top-right child of the exact Save-contact banner.
        return knownBanner && "android.view.View".equals(nodeClass) && unlabeled
                && verifiedCloseChildren && children == 2
                && w > 0 && h > 0 && cw > 0 && ch > 0
                && left >= cardLeft && top >= cardTop && right <= cardRight && bottom <= cardBottom
                && w * 4 < cw && h * 2 < ch
                && (left + right) / 2 >= cardRight - cw / 4
                && (top + bottom) / 2 < cardTop + ch / 3;
    }
}
