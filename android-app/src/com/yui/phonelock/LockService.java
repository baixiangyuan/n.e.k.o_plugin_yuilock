package com.yui.phonelock;

import android.app.AppOpsManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.app.admin.DevicePolicyManager;
import android.app.usage.UsageEvents;
import android.app.usage.UsageStatsManager;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothServerSocket;
import android.bluetooth.BluetoothSocket;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 核心前台服务：
 * - TCP 监听（局域网）+ UDP 自动发现应答
 * - 蓝牙 RFCOMM(SPP) 监听
 * - 熄屏锁屏（设备管理器 lockNow）
 * - 应用锁模式：轮询前台应用，非白名单一律弹回桌面；开机自动恢复
 */
public class LockService extends Service implements CommandHandler.Host {

    private static final String CHANNEL_ID = "yuilock";
    public static final String SPP_UUID_STR = "00001101-0000-1000-8000-00805F9B34FB";

    public static volatile boolean running = false;
    public static volatile boolean btListening = false;
    public static volatile int activePort = 0;
    public static volatile String lastEvent = "—";

    private volatile ServerSocket tcpServer;
    private volatile DatagramSocket udpSocket;
    private volatile BluetoothServerSocket btServer;
    private volatile Thread appLockThread;
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private WifiManager.MulticastLock multicastLock;
    private DevicePolicyManager dpm;
    private ComponentName admin;
    private SharedPreferences prefs;
    private Set<String> whitelist;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences("yuilock", MODE_PRIVATE);
        dpm = (DevicePolicyManager) getSystemService(DEVICE_POLICY_SERVICE);
        admin = new ComponentName(this, AdminReceiver.class);
        whitelist = new HashSet<>();
        whitelist.add(getPackageName());
        whitelist.add("com.android.systemui");
        try {
            Intent home = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME);
            ResolveInfo ri = getPackageManager().resolveActivity(home, PackageManager.MATCH_DEFAULT_ONLY);
            if (ri != null && ri.activityInfo != null) {
                whitelist.add(ri.activityInfo.packageName);
            }
        } catch (Exception ignored) {
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startInForeground();
        if (!running) {
            running = true;
            startListeners();
            if (prefs.getBoolean("applock_active", false)) {
                startAppLock();
            }
        }
        return START_STICKY;
    }

    private int port() {
        try {
            return Integer.parseInt(prefs.getString("port", "48912"));
        } catch (Exception e) {
            return 48912;
        }
    }

    public String token() {
        return prefs.getString("token", "");
    }

    private void startInForeground() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL_ID, "Yui Lock 服务",
                NotificationManager.IMPORTANCE_LOW));
        Notification.Builder b = new Notification.Builder(this, CHANNEL_ID);
        Notification n = b.setContentTitle("Yui Lock 运行中")
                .setContentText("端口 " + port() + " · 等待电脑端指令")
                .setSmallIcon(android.R.drawable.ic_lock_lock)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING);
        } else {
            startForeground(1, n);
        }
    }

    private synchronized void startListeners() {
        int p = port();
        activePort = p;
        if (multicastLock == null) {
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wm != null) {
                multicastLock = wm.createMulticastLock("yuilock");
                multicastLock.setReferenceCounted(false);
                multicastLock.acquire();
            }
        }
        Thread tcp = new Thread(() -> {
            try {
                ServerSocket ss = new ServerSocket();
                ss.setReuseAddress(true);
                ss.bind(new InetSocketAddress(p));
                tcpServer = ss;
                while (running) {
                    final Socket s = ss.accept();
                    pool.execute(() -> {
                        try {
                            serve(s.getInputStream(), s.getOutputStream());
                        } catch (Throwable ignored) {
                        } finally {
                            try {
                                s.close();
                            } catch (IOException ignored) {
                            }
                        }
                    });
                }
            } catch (Throwable t) {
                if (running) lastEvent = "TCP 监听失败: " + t;
            }
        }, "yuilock-tcp");
        Thread udp = new Thread(this::udpLoop, "yuilock-udp");
        Thread bt = new Thread(this::btLoop, "yuilock-bt");
        tcp.start();
        udp.start();
        bt.start();
        lastEvent = "服务已启动";
    }

    private void udpLoop() {
        try {
            DatagramSocket ds = new DatagramSocket(null);
            ds.setReuseAddress(true);
            ds.bind(new InetSocketAddress(port()));
            udpSocket = ds;
            byte[] buf = new byte[1024];
            while (running) {
                DatagramPacket pkt = new DatagramPacket(buf, buf.length);
                ds.receive(pkt);
                String msg = new String(pkt.getData(), 0, pkt.getLength(), StandardCharsets.UTF_8);
                if (msg.contains("yui_lock_discover")) {
                    JSONObject reply = new JSONObject();
                    reply.put("yui_lock_service", true);
                    reply.put("device", android.os.Build.MODEL);
                    reply.put("port", port());
                    reply.put("applock", isAppLockOn());
                    byte[] out = reply.toString().getBytes(StandardCharsets.UTF_8);
                    ds.send(new DatagramPacket(out, out.length, pkt.getAddress(), pkt.getPort()));
                }
            }
        } catch (Throwable t) {
            if (running) lastEvent = "UDP: " + t;
        }
    }

    private void btLoop() {
        try {
            if (Build.VERSION.SDK_INT >= 31
                    && checkSelfPermission("android.permission.BLUETOOTH_CONNECT")
                    != PackageManager.PERMISSION_GRANTED) {
                lastEvent = "蓝牙：未授权（去系统设置授予“附近设备”权限）";
                return;
            }
            BluetoothAdapter adapter = BluetoothAdapter.getDefaultAdapter();
            if (adapter == null || !adapter.isEnabled()) {
                lastEvent = "蓝牙：不可用";
                return;
            }
            BluetoothServerSocket bss = adapter.listenUsingRfcommWithServiceRecord(
                    "YuiLock", UUID.fromString(SPP_UUID_STR));
            btServer = bss;
            btListening = true;
            while (running) {
                final BluetoothSocket s = bss.accept();
                pool.execute(() -> {
                    try {
                        serve(s.getInputStream(), s.getOutputStream());
                    } catch (Throwable ignored) {
                    } finally {
                        try {
                            s.close();
                        } catch (IOException ignored) {
                        }
                    }
                });
            }
        } catch (Throwable t) {
            if (running) lastEvent = "蓝牙: " + t;
        } finally {
            btListening = false;
        }
    }

    private void serve(InputStream in, OutputStream out) {
        try {
            BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            String line;
            while (running && (line = r.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) continue;
                String resp = CommandHandler.handle(line, this);
                out.write(resp.getBytes(StandardCharsets.UTF_8));
                out.write('\n');
                out.flush();
            }
        } catch (Throwable ignored) {
        }
    }

    // ---------- CommandHandler.Host ----------

    public boolean isAdminActive() {
        return dpm != null && dpm.isAdminActive(admin);
    }

    public void lockScreen() {
        dpm.lockNow();
    }

    public boolean isAppLockOn() {
        return prefs.getBoolean("applock_active", false);
    }

    public int battery() {
        try {
            BatteryManager bm = (BatteryManager) getSystemService(BATTERY_SERVICE);
            return bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        } catch (Exception e) {
            return -1;
        }
    }

    public synchronized void setAppLock(boolean on) {
        prefs.edit().putBoolean("applock_active", on).apply();
        if (on) {
            startAppLock();
        } else {
            stopAppLock();
        }
        lastEvent = on ? "应用锁已开启" : "应用锁已解除";
    }

    private boolean hasUsageAccess() {
        try {
            AppOpsManager ops = (AppOpsManager) getSystemService(APP_OPS_SERVICE);
            return ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS,
                    android.os.Process.myUid(), getPackageName()) == AppOpsManager.MODE_ALLOWED;
        } catch (Exception e) {
            return false;
        }
    }

    private synchronized void startAppLock() {
        if (appLockThread != null && appLockThread.isAlive()) return;
        if (!hasUsageAccess()) {
            lastEvent = "应用锁：请先授予“使用情况访问权限”（应用里第②步）";
            return;
        }
        appLockThread = new Thread(() -> {
            UsageStatsManager usm = (UsageStatsManager) getSystemService(USAGE_STATS_SERVICE);
            while (running && prefs.getBoolean("applock_active", false)) {
                try {
                    long now = System.currentTimeMillis();
                    UsageEvents ev = usm.queryEvents(now - 3000, now);
                    UsageEvents.Event e = new UsageEvents.Event();
                    String top = null;
                    while (ev.hasNextEvent()) {
                        ev.getNextEvent(e);
                        if (e.getEventType() == UsageEvents.Event.MOVE_TO_FOREGROUND) {
                            top = e.getPackageName();
                        }
                    }
                    if (top != null && !whitelist.contains(top)) {
                        Intent home = new Intent(Intent.ACTION_MAIN)
                                .addCategory(Intent.CATEGORY_HOME)
                                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(home);
                        lastEvent = "应用锁拦截: " + top;
                    }
                    Thread.sleep(300);
                } catch (InterruptedException ie) {
                    return;
                } catch (Throwable t) {
                    try {
                        Thread.sleep(500);
                    } catch (InterruptedException ie) {
                        return;
                    }
                }
            }
        }, "yuilock-applock");
        appLockThread.start();
    }

    private synchronized void stopAppLock() {
        if (appLockThread != null) {
            appLockThread.interrupt();
            appLockThread = null;
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        stopAppLock();
        try {
            if (tcpServer != null) tcpServer.close();
        } catch (IOException ignored) {
        }
        try {
            if (udpSocket != null) udpSocket.close();
        } catch (Exception ignored) {
        }
        try {
            if (btServer != null) btServer.close();
        } catch (IOException ignored) {
        }
        if (multicastLock != null) {
            try {
                multicastLock.release();
            } catch (Throwable ignored) {
            }
            multicastLock = null;
        }
        pool.shutdownNow();
        super.onDestroy();
    }
}
