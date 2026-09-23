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
import android.provider.Settings;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.SynchronousQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/**
 * 核心前台服务：
 * - TCP 监听（局域网）+ UDP 自动发现应答（带 nonce 回显）
 * - 蓝牙 RFCOMM(SPP) 监听
 * - 熄屏锁屏（设备管理器 lockNow）
 * - 应用锁模式：轮询前台应用，非白名单一律弹回桌面
 *
 * 加固：
 * - accept 前先抢名额，收不下立刻断开；线程池队列有界；
 *   单条连接最多 30 条命令；首包 5s 超时，之后 20s；
 * - 认证挑战按连接隔离（CommandHandler.Session），proof 绑定 cmd|nonce|challenge；
 * - 应用锁状态与拦截线程一致：服务重启按已保存状态恢复并做权限预检，权限缺失自动清为关。
 */
public class LockService extends Service implements CommandHandler.Host {

    private static final String CHANNEL_ID = "yuilock";
    private static final int MAX_LINE = 4096;
    private static final int INITIAL_READ_TIMEOUT_MS = 5_000;
    private static final int READ_TIMEOUT_MS = 20_000;
    private static final int MAX_CONNECTIONS = 4;
    private static final int POOL_QUEUE_CAPACITY = 8;
    private static final int MAX_COMMANDS_PER_CONNECTION = 30;
    private static final long SESSION_MAX_MS = 60_000;
    public static final String SPP_UUID_STR = "00001101-0000-1000-8000-00805F9B34FB";

    public static volatile boolean running = false;
    public static volatile boolean btListening = false;
    public static volatile boolean appLockRunning = false;
    public static volatile int activePort = 0;
    public static volatile String lastEvent = "—";

    private volatile ServerSocket tcpServer;
    private volatile DatagramSocket udpSocket;
    private volatile BluetoothServerSocket btServer;
    private volatile Thread appLockThread;
    private final Semaphore slots = new Semaphore(MAX_CONNECTIONS);
    private final ThreadPoolExecutor pool = new ThreadPoolExecutor(
            MAX_CONNECTIONS, MAX_CONNECTIONS, 0L, TimeUnit.MILLISECONDS,
            new java.util.concurrent.ArrayBlockingQueue<>(POOL_QUEUE_CAPACITY),
            new ThreadPoolExecutor.AbortPolicy());
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
            resumeAppLockIfSaved();
        }
        return START_STICKY;
    }

    /** 服务重启（被杀拉起）：按已保存状态恢复应用锁，但必须通过权限预检，否则清为关。 */
    private void resumeAppLockIfSaved() {
        if (!prefs.getBoolean("applock_active", false)) {
            return;
        }
        if (hasUsageAccess()) {
            startAppLock();
            lastEvent = "应用锁已恢复";
        } else {
            prefs.edit().putBoolean("applock_active", false).apply();
            appLockRunning = false;
            lastEvent = "应用锁：缺少使用情况访问权限，已自动关闭";
        }
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
        boolean tokenOk = !prefs.getString("token", "").isEmpty();
        Notification n = b.setContentTitle("Yui Lock 运行中")
                .setContentText("端口 " + port() + (tokenOk ? " · 已启用配对令牌" : " · ⚠ 未设令牌，控制命令被拒绝"))
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
                ss.bind(new InetSocketAddress(p), 8);
                tcpServer = ss;
                while (running) {
                    final Socket s = ss.accept();
                    // 先抢名额再收下连接：收不下直接断开，队列不会无界堆积
                    if (!slots.tryAcquire()) {
                        try {
                            s.close();
                        } catch (IOException ignored) {
                        }
                        continue;
                    }
                    try {
                        pool.execute(() -> serveSocket(s));
                    } catch (RejectedExecutionException e) {
                        slots.release();
                        try {
                            s.close();
                        } catch (IOException ignored) {
                        }
                    }
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

    private void serveSocket(final Socket s) {
        try {
            s.setSoTimeout(INITIAL_READ_TIMEOUT_MS);
            serve(s.getInputStream(), s.getOutputStream(), s);
        } catch (Throwable ignored) {
        } finally {
            try {
                s.close();
            } catch (IOException ignored) {
            }
            slots.release();
        }
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
                    // 原样回显请求里的随机数，防止旧广播包重放
                    String nonce = "";
                    try {
                        nonce = new JSONObject(msg).optString("n", "");
                    } catch (Exception ignored) {
                    }
                    if (!nonce.isEmpty()) {
                        reply.put("n", nonce);
                        // 已配对时对发现应答签名（绑定本次随机数），客户端只采信带合法签名的包
                        String tokenNow = prefs.getString("token", "");
                        if (!tokenNow.isEmpty()) {
                            reply.put("sig", CommandHandler.hmacSha256Hex(tokenNow, "discover|" + nonce));
                        }
                    }
                    reply.put("device", android.os.Build.MODEL);
                    reply.put("port", port());
                    reply.put("token_set", !prefs.getString("token", "").isEmpty());
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
                if (!slots.tryAcquire()) {
                    try {
                        s.close();
                    } catch (IOException ignored) {
                    }
                    continue;
                }
                try {
                    pool.execute(() -> serveBt(s));
                } catch (RejectedExecutionException e) {
                    slots.release();
                    try {
                        s.close();
                    } catch (IOException ignored) {
                    }
                }
            }
        } catch (Throwable t) {
            if (running) lastEvent = "蓝牙: " + t;
        } finally {
            btListening = false;
        }
    }

    private void serveBt(final BluetoothSocket s) {
        // 蓝牙 socket 无 SoTimeout：用总时长看门狗兜底（客户端每命令一条连接，足够）
        final Thread killer = new Thread(() -> {
            try {
                Thread.sleep(READ_TIMEOUT_MS);
            } catch (InterruptedException ignored) {
                return;
            }
            try {
                s.close();
            } catch (IOException ignored) {
            }
        }, "yuilock-bt-killer");
        killer.start();
        try {
            serve(s.getInputStream(), s.getOutputStream(), null);
        } catch (Throwable ignored) {
        } finally {
            killer.interrupt();
            try {
                s.close();
            } catch (IOException ignored) {
            }
            slots.release();
        }
    }

    /** 有界读行：超过 4KB 直接断开；读超时（SoTimeout）抛 SocketTimeoutException 结束连接 */
    private String readLineBounded(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int c = -1;
        int n = 0;
        while ((c = in.read()) != -1) {
            if (c == '\n') {
                break;
            }
            if (c == '\r') {
                continue;
            }
            if (++n > MAX_LINE) {
                throw new IOException("指令超长");
            }
            buf.write(c);
        }
        if (c == -1 && buf.size() == 0) {
            return null;
        }
        return buf.toString("UTF-8");
    }

    private void serve(InputStream in, OutputStream out, Socket tcpSocket) {
        CommandHandler.Session session = new CommandHandler.Session();
        long deadline = System.currentTimeMillis() + SESSION_MAX_MS;
        try {
            int count = 0;
            while (running) {
                // 单连接总时长上限：空行刷屏也无法长期占住名额（空行同样计入命令上限）
                if (System.currentTimeMillis() > deadline) {
                    return;
                }
                String line;
                try {
                    line = readLineBounded(in);
                    if (tcpSocket != null && count == 0) {
                        // 首包必须 5 秒内到达；之后放宽到 20s
                        tcpSocket.setSoTimeout(READ_TIMEOUT_MS);
                    }
                } catch (SocketTimeoutException te) {
                    return; // 空闲超时，关闭连接
                }
                if (line == null) {
                    return;
                }
                if (++count > MAX_COMMANDS_PER_CONNECTION) {
                    return;
                }
                line = line.trim();
                if (line.isEmpty()) {
                    continue;
                }
                String resp = CommandHandler.handle(line, this, session);
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

    /** 对外只报告拦截线程的真实运行状态 */
    public boolean isAppLockOn() {
        return appLockRunning;
    }

    public int battery() {
        try {
            BatteryManager bm = (BatteryManager) getSystemService(BATTERY_SERVICE);
            return bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        } catch (Exception e) {
            return -1;
        }
    }

    /** 权限预检（使用情况访问 + 悬浮窗）通过才写状态并启动拦截线程；关闭永远成功 */
    public synchronized boolean setAppLock(boolean on) {
        if (on) {
            if (!hasUsageAccess()) {
                lastEvent = "应用锁开启失败：缺少「使用情况访问权限」";
                return false;
            }
            if (!Settings.canDrawOverlays(this)) {
                lastEvent = "应用锁开启失败：缺少「显示悬浮窗」权限（Android 10+ 拦截必需）";
                return false;
            }
            prefs.edit().putBoolean("applock_active", true).apply();
            startAppLock();
            lastEvent = "应用锁已开启";
            return true;
        }
        prefs.edit().putBoolean("applock_active", false).apply();
        stopAppLock();
        lastEvent = "应用锁已解除";
        return true;
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
        if (appLockThread != null && appLockThread.isAlive()) {
            appLockRunning = true;
            return;
        }
        if (!Settings.canDrawOverlays(this)) {
            // 悬浮窗权限是 Android 10+ 后台拉起桌面的前提：缺失时干脆不进入「开启」状态
            prefs.edit().putBoolean("applock_active", false).apply();
            appLockRunning = false;
            lastEvent = "应用锁：缺少「显示悬浮窗」权限，未开启";
            return;
        }
        appLockThread = new Thread(() -> {
            appLockRunning = true;
            UsageStatsManager usm = (UsageStatsManager) getSystemService(USAGE_STATS_SERVICE);
            try {
                int failStreak = 0;
                while (running && prefs.getBoolean("applock_active", false)) {
                    try {
                        // 悬浮窗权限被中途收回也视为失败
                        if (!Settings.canDrawOverlays(this)) {
                            failStreak++;
                        } else {
                            String top = queryTop(usm);
                            if (top != null && !whitelist.contains(top)) {
                                Intent home = new Intent(Intent.ACTION_MAIN)
                                        .addCategory(Intent.CATEGORY_HOME)
                                        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                                startActivity(home);
                                Thread.sleep(400);
                                // 后台拉起可能被系统静默拒绝（Android 10+）：核验是否真的回到桌面
                                String nowTop = queryTop(usm);
                                if (top.equals(nowTop)) {
                                    failStreak++;
                                    lastEvent = "应用锁拦截失败：请在系统设置授予本应用「显示悬浮窗」权限";
                                } else {
                                    failStreak = 0;
                                    lastEvent = "应用锁拦截: " + top;
                                }
                            }
                        }
                        if (failStreak >= 5) {
                            // 连续失败：不再保持「开启」状态，避免电脑端看到假开启
                            prefs.edit().putBoolean("applock_active", false).apply();
                            appLockRunning = false;
                            lastEvent = "应用锁已自动关闭：连续拦截失败（请授予「显示悬浮窗」权限后重试）";
                            return;
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
            } finally {
                appLockRunning = false;
            }
        }, "yuilock-applock");
        appLockThread.start();
    }

    public String appLockNote() {
        return lastEvent;
    }

    private String queryTop(UsageStatsManager usm) {
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
            return top;
        } catch (Exception e2) {
            return null;
        }
    }

    private synchronized void stopAppLock() {
        prefs.edit().putBoolean("applock_active", false).apply();
        if (appLockThread != null) {
            appLockThread.interrupt();
            appLockThread = null;
        }
        appLockRunning = false;
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
