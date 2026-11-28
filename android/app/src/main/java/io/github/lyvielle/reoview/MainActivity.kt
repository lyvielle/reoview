package io.github.lyvielle.reoview

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ActivityInfo
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.Toast
import com.google.firebase.messaging.FirebaseMessaging
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

fun serverUrl(ctx: Context): String? = ctx.getSharedPreferences("reoview", Context.MODE_PRIVATE).getString("server", null)

/** Tell the Reoview server where to send this device's alerts. */
fun registerToken(ctx: Context, token: String) {
    val server = serverUrl(ctx) ?: return
    thread {
        try {
            val c = URL("$server/api/fcm/register").openConnection() as HttpURLConnection
            c.requestMethod = "POST"
            c.doOutput = true
            c.connectTimeout = 10000
            c.readTimeout = 10000
            c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write(JSONObject().put("token", token).toString().toByteArray()) }
            Log.i("reoview", "token registered: HTTP ${c.responseCode}")
            c.disconnect()
        } catch (e: Exception) {
            Log.w("reoview", "token registration failed", e)
        }
    }
}

class MainActivity : Activity() {
    private lateinit var web: WebView
    private var full: View? = null
    private var fullDone: WebChromeClient.CustomViewCallback? = null
    private var dialog: AlertDialog? = null

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        window.statusBarColor = Color.WHITE
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        web = WebView(this)
        setContentView(web)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.mediaPlaybackRequiresUserGesture = false
        val version = packageManager.getPackageInfo(packageName, 0).versionName
        web.settings.userAgentString += " ReoviewApp/$version"  // the web UI hides PWA-only parts when it sees this
        web.webViewClient = object : WebViewClient() {
            override fun onReceivedError(view: WebView, req: WebResourceRequest, err: WebResourceError) {
                if (req.isForMainFrame) askServer("Could not connect: ${err.description}")
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {  // video fullscreen button
                full = view
                fullDone = callback
                (window.decorView as FrameLayout).addView(view, FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                web.visibility = View.GONE
                requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE  // also with auto-rotate off
                @Suppress("DEPRECATION")
                view.systemUiVisibility = View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            }

            override fun onHideCustomView() {
                (window.decorView as FrameLayout).removeView(full)
                full = null
                web.visibility = View.VISIBLE
                requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
                fullDone?.onCustomViewHidden()
            }
        }
        web.setDownloadListener { url, ua, disposition, mime, _ -> download(url, ua, disposition, mime) }
        if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        if (serverUrl(this) == null) askServer(null) else connect(intent)
    }

    override fun onNewIntent(intent: Intent) {  // notification tapped while the app is open
        super.onNewIntent(intent)
        if (serverUrl(this) != null) web.loadUrl(serverUrl(this) + (intent.getStringExtra("url") ?: "/"))
    }

    private fun connect(intent: Intent?) {
        web.loadUrl(serverUrl(this) + (intent?.getStringExtra("url") ?: "/"))
        FirebaseMessaging.getInstance().token.addOnSuccessListener { registerToken(this, it) }
    }

    private fun askServer(error: String?) {
        dialog?.dismiss()
        val input = EditText(this).apply {
            setText(serverUrl(this@MainActivity) ?: "http://")
            setSingleLine()
        }
        val box = FrameLayout(this).apply { setPadding(48, 16, 48, 0); addView(input) }
        dialog = AlertDialog.Builder(this)
            .setTitle("Reoview server")
            .setMessage((error?.let { "$it\n\n" } ?: "") + "Address of your Reoview server, as shown on its \"Get the app\" page, e.g. http://192.168.1.10:8000")
            .setView(box)
            .setCancelable(false)
            .setPositiveButton("Connect") { _, _ ->
                var url = input.text.toString().trim().trimEnd('/')
                if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
                getSharedPreferences("reoview", MODE_PRIVATE).edit().putString("server", url).apply()
                connect(null)
            }
            .setNegativeButton("Close app") { _, _ -> finish() }
            .show()
    }

    private fun download(url: String, ua: String, disposition: String?, mime: String?) {
        val name = URLUtil.guessFileName(url, disposition, mime)
        val req = DownloadManager.Request(Uri.parse(url))
            .setMimeType(mime)
            .addRequestHeader("User-Agent", ua)
            .addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url) ?: "")
            .setTitle(name)
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
        val id = getSystemService(DownloadManager::class.java).enqueue(req)
        if (name.endsWith(".apk")) installWhenDone(id)
        Toast.makeText(this, "Downloading $name", Toast.LENGTH_SHORT).show()
    }

    /** Hand a finished APK download to the system installer (the update button on the web page). */
    private fun installWhenDone(id: Long) {
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) {
                if (i.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1) != id) return
                unregisterReceiver(this)
                val uri = getSystemService(DownloadManager::class.java).getUriForDownloadedFile(id) ?: return  // failed
                try {
                    startActivity(Intent(Intent.ACTION_VIEW)
                        .setDataAndType(uri, "application/vnd.android.package-archive")
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
                } catch (e: Exception) {
                    Toast.makeText(c, "Open the APK in Downloads to install it", Toast.LENGTH_LONG).show()
                }
            }
        }
        val filter = IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE)
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(receiver, filter, RECEIVER_EXPORTED)  // sent by the download provider
        else registerReceiver(receiver, filter)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        when {
            full != null -> web.webChromeClient?.onHideCustomView()
            web.canGoBack() -> web.goBack()
            else -> @Suppress("DEPRECATION") super.onBackPressed()
        }
    }
}
