package com.yui.phonelock;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/**
 * 开机自启：只恢复网络/蓝牙监听；应用锁随重启解除（用户要求：重启即解锁）。
 */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            return;
        }
        context.getSharedPreferences("yuilock", Context.MODE_PRIVATE)
                .edit().putBoolean("applock_active", false).apply();
        Intent svc = new Intent(context, LockService.class);
        if (Build.VERSION.SDK_INT >= 26) {
            context.startForegroundService(svc);
        } else {
            context.startService(svc);
        }
    }
}
