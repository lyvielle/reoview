package io.github.lyvielle.reoview

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/** Shows alerts sent by the Reoview server as data messages (so it works the same in foreground and background). */
class PushService : FirebaseMessagingService() {
    override fun onNewToken(token: String) = registerToken(this, token)

    override fun onMessageReceived(msg: RemoteMessage) {
        val d = msg.data
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("alerts", "Alerts", NotificationManager.IMPORTANCE_HIGH))
        val open = Intent(this, MainActivity::class.java)
            .putExtra("url", d["url"] ?: "/")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        val tag = d["tag"] ?: "reoview"
        val pi = PendingIntent.getActivity(this, tag.hashCode(), open,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(this, "alerts")
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(d["title"] ?: "Reoview")
            .setContentText(d["body"] ?: "Motion detected")
            .setAutoCancel(true)
            .setContentIntent(pi)
            .build()
        nm.notify(tag, 0, n)
    }
}
