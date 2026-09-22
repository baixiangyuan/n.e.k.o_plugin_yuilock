package com.yui.phonelock;

import android.app.admin.DeviceAdminReceiver;
import android.content.Context;
import android.content.Intent;
import android.widget.Toast;

/** 设备管理器接收器——只声明 force-lock 权限（见 res/xml/device_admin.xml）。 */
public class AdminReceiver extends DeviceAdminReceiver {

    @Override
    public void onEnabled(Context context, Intent intent) {
        Toast.makeText(context, "锁屏权限已激活", Toast.LENGTH_SHORT).show();
    }

    @Override
    public CharSequence onDisableRequested(Context context, Intent intent) {
        return "停用后，电脑端的 Yui 将无法锁定这台手机。确定要停用吗？";
    }
}
