package ai.openphone.agent;

import android.app.Activity;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

/**
 * Browser surface owned by the agent task.
 *
 * <p>Using an in-app WebView is intentional. Chrome's launcher activity reuses a
 * single Android task, so opening Chrome on display 0 can migrate the task away
 * from the private display. This activity has its own task affinity and cannot be
 * stolen when the owner opens Chrome.</p>
 */
public class AgentBrowserActivity extends Activity {
    private static final String DEFAULT_URL = "https://www.wikipedia.org/";

    private EditText address;
    private ProgressBar progress;
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildUi());
        configureWebView();
        loadRequestedUrl();
    }

    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        loadRequestedUrl();
    }

    private View buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.WHITE);

        TextView badge = new TextView(this);
        badge.setText("AGENT DISPLAY · ISOLATED BROWSER");
        badge.setTextSize(11);
        badge.setTextColor(Color.rgb(30, 64, 175));
        badge.setBackgroundColor(Color.rgb(239, 246, 255));
        badge.setPadding(dp(14), dp(8), dp(14), dp(8));
        root.addView(badge, matchWrap());

        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setOrientation(LinearLayout.HORIZONTAL);
        toolbar.setPadding(dp(8), dp(6), dp(8), dp(6));
        toolbar.setBackgroundColor(Color.rgb(248, 250, 252));

        Button back = new Button(this);
        back.setText("‹");
        back.setTextSize(24);
        back.setAllCaps(false);
        back.setOnClickListener(v -> {
            if (webView.canGoBack()) {
                webView.goBack();
            }
        });
        toolbar.addView(back, new LinearLayout.LayoutParams(dp(58), dp(52)));

        address = new EditText(this);
        address.setSingleLine(true);
        address.setTextSize(14);
        address.setHint("https://");
        address.setInputType(android.text.InputType.TYPE_CLASS_TEXT
                | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        address.setOnEditorActionListener((view, actionId, event) -> {
            openAddressBarValue();
            return true;
        });
        LinearLayout.LayoutParams addressParams =
                new LinearLayout.LayoutParams(0, dp(52), 1f);
        toolbar.addView(address, addressParams);

        Button go = new Button(this);
        go.setText("Go");
        go.setTextSize(13);
        go.setAllCaps(false);
        go.setOnClickListener(v -> openAddressBarValue());
        toolbar.addView(go, new LinearLayout.LayoutParams(dp(70), dp(52)));
        root.addView(toolbar, matchWrap());

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        root.addView(progress, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(3)));

        webView = new WebView(this);
        root.addView(webView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        return root;
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isWebUrl(uri)) {
                    return false;
                }
                Toast.makeText(AgentBrowserActivity.this,
                        "Blocked non-web link: " + uri.getScheme(), Toast.LENGTH_SHORT).show();
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                address.setText(url);
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progress.setProgress(newProgress);
                progress.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
                String url = view.getUrl();
                if (url != null) {
                    address.setText(url);
                }
            }
        });
    }

    private void loadRequestedUrl() {
        String requested = getIntent().getDataString();
        loadUrl(requested == null ? DEFAULT_URL : requested);
    }

    private void openAddressBarValue() {
        loadUrl(address.getText().toString());
    }

    private void loadUrl(String raw) {
        String candidate = raw == null ? "" : raw.trim();
        if (!candidate.contains("://")) {
            candidate = "https://" + candidate;
        }
        Uri uri = Uri.parse(candidate);
        if (!isWebUrl(uri) || uri.getHost() == null) {
            Toast.makeText(this, "Only http/https URLs are allowed", Toast.LENGTH_SHORT).show();
            return;
        }
        address.setText(candidate);
        webView.loadUrl(candidate);
    }

    private boolean isWebUrl(Uri uri) {
        String scheme = uri.getScheme();
        return "https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
        }
        super.onDestroy();
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
