package com.yui.phonelock;

import android.app.Activity;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.provider.Settings;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Enumeration;

public class MainActivity extends Activity {

    private static final int PERMS_REQ = 10;
    private static final int ADMIN_REQ = 11;
    private static final int SCAN_REQ = 12;
    private static final int CAMERA_REQ = 20;

    private SharedPreferences prefs;
    private final Handler ui = new Handler();
    private DevicePolicyManager dpm;
    private ComponentName adminComp;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences("yuilock", MODE_PRIVATE);
        dpm = (DevicePolicyManager) getSystemService(DEVICE_POLICY_SERVICE);
        adminComp = new ComponentName(this, AdminReceiver.class);

        String token = prefs.getString("token", null);
        if (token == null || token.isEmpty()) {
            token = randomToken();
            prefs.edit().putString("token", token).apply();
        }
        ((EditText) findViewById(R.id.etPort)).setText(prefs.getString("port", "48912"));
        ((EditText) findViewById(R.id.etToken)).setText(token);
        ((TextView) findViewById(R.id.tvHelp)).setText(getString(R.string.help_text));

        findViewById(R.id.btnSave).setOnClickListener(v -> save());
        findViewById(R.id.btnAdmin).setOnClickListener(v -> askAdmin());
        findViewById(R.id.btnUsage).setOnClickListener(v ->
                startActivity(new Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)));
        findViewById(R.id.btnScan).setOnClickListener(v -> startScan());
        findViewById(R.id.btnStart).setOnClickListener(v -> {
            Intent i = new Intent(this, LockService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(i);
            } else {
                startService(i);
            }
        });
        findViewById(R.id.btnStop).setOnClickListener(v ->
                stopService(new Intent(this, LockService.class)));
        findViewById(R.id.btnTest).setOnClickListener(v -> {
            if (dpm.isAdminActive(adminComp)) {
                dpm.lockNow();
            } else {
                Toast.makeText(this, "请先激活锁屏权限（第①步）", Toast.LENGTH_SHORT).show();
            }
        });
        findViewById(R.id.btnBattery).setOnClickListener(v -> batteryWhiteList());

        requestPerms();
        poll();
        handleDeepLink(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleDeepLink(intent);
    }

    /** 系统相机扫配对码会以 VIEW 深链打开本应用；应用前请用户确认（Activity 已导出）。 */
    private void handleDeepLink(Intent intent) {
        try {
            if (intent == null || intent.getData() == null) {
                return;
            }
            android.net.Uri uri = intent.getData();
            if (!"yuilock".equals(uri.getScheme()) || !"pair".equals(uri.getHost())) {
                return;
            }
            final String text = uri.toString();
            new android.app.AlertDialog.Builder(this)
                    .setTitle("检测到配对码")
                    .setMessage("是否把该配对码里的端口和令牌应用到本机？\n\n" + text)
                    .setPositiveButton("应用", (d, w) -> {
                        if (applyPair(text)) {
                            Toast.makeText(this, "配对成功：端口和令牌已自动填好", Toast.LENGTH_LONG).show();
                        } else {
                            Toast.makeText(this, "配对码无效", Toast.LENGTH_SHORT).show();
                        }
                    })
                    .setNegativeButton("取消", null)
                    .show();
        } catch (Exception ignored) {
        }
    }

    private void requestPerms() {
        ArrayList<String> need = new ArrayList<>();
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
            need.add("android.permission.POST_NOTIFICATIONS");
        }
        if (Build.VERSION.SDK_INT >= 31
                && checkSelfPermission("android.permission.BLUETOOTH_CONNECT") != PackageManager.PERMISSION_GRANTED) {
            need.add("android.permission.BLUETOOTH_CONNECT");
        }
        if (checkSelfPermission("android.permission.CAMERA") != PackageManager.PERMISSION_GRANTED) {
            need.add("android.permission.CAMERA");
        }
        if (!need.isEmpty()) {
            requestPermissions(need.toArray(new String[0]), PERMS_REQ);
        }
    }

    private void startScan() {
        if (checkSelfPermission("android.permission.CAMERA") == PackageManager.PERMISSION_GRANTED) {
            startActivityForResult(new Intent(this, ScanActivity.class), SCAN_REQ);
        } else {
            requestPermissions(new String[]{"android.permission.CAMERA"}, CAMERA_REQ);
        }
    }

    /** 解析 yuilock://pair?p=端口&t=令牌 并保存 */
    private boolean applyPair(String text) {
        try {
            if (text == null) {
                return false;
            }
            java.net.URI uri = java.net.URI.create(text.trim());
            if (!"yuilock".equals(uri.getScheme()) || !"pair".equals(uri.getHost())) {
                return false;
            }
            String query = uri.getRawQuery();
            String port = null;
            String token = null;
            for (String kv : query.split("&")) {
                String[] pair = kv.split("=", 2);
                if (pair.length == 2) {
                    if ("p".equals(pair[0])) port = pair[1];
                    if ("t".equals(pair[0])) token = pair[1];
                }
            }
            if (port == null || token == null || token.isEmpty()) {
                return false;
            }
            int pv = Integer.parseInt(port);
            if (pv < 1024 || pv > 65535) {
                return false;
            }
            prefs.edit().putString("port", port).putString("token", token).apply();
            ((EditText) findViewById(R.id.etPort)).setText(port);
            ((EditText) findViewById(R.id.etToken)).setText(token);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private String randomToken() {
        SecureRandom r = new SecureRandom();
        StringBuilder sb = new StringBuilder();
        String chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
        for (int i = 0; i < 12; i++) {
            sb.append(chars.charAt(r.nextInt(chars.length())));
        }
        return sb.toString();
    }

    private void save() {
        String p = ((EditText) findViewById(R.id.etPort)).getText().toString().trim();
        int port;
        try {
            port = Integer.parseInt(p);
        } catch (Exception e) {
            port = 0;
        }
        if (port < 1024 || port > 65535) {
            Toast.makeText(this, "端口需在 1024-65535 之间", Toast.LENGTH_SHORT).show();
            return;
        }
        String token = ((EditText) findViewById(R.id.etToken)).getText().toString().trim();
        prefs.edit().putString("port", p).putString("token", token).apply();
        Toast.makeText(this, "已保存（服务运行中则停止后再启动生效）", Toast.LENGTH_LONG).show();
    }

    private void askAdmin() {
        if (dpm.isAdminActive(adminComp)) {
            Toast.makeText(this, "锁屏权限已激活", Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            Intent i = new Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN);
            i.putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, adminComp);
            i.putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION, getString(R.string.admin_desc));
            startActivityForResult(i, ADMIN_REQ);
        } catch (Exception e) {
            Toast.makeText(this, "无法打开设备管理器设置", Toast.LENGTH_SHORT).show();
        }
    }

    private void batteryWhiteList() {
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            try {
                startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Exception ignored) {
            }
        }
    }

    private void poll() {
        final TextView tvStatus = findViewById(R.id.tvStatus);
        final Button btnAdmin = findViewById(R.id.btnAdmin);
        Runnable r = new Runnable() {
            @Override
            public void run() {
                boolean adminOk = dpm.isAdminActive(adminComp);
                boolean intercepting = LockService.appLockRunning;
                boolean marked = prefs.getBoolean("applock_active", false);
                String applockText;
                if (intercepting) {
                    applockText = "拦截中（打开任何 App 会被弹回桌面，重启手机即解除）";
                } else if (marked) {
                    applockText = "已标记但拦截未运行（服务重启后会自动恢复，权限不足则自动关闭）";
                } else {
                    applockText = "关闭";
                }
                StringBuilder sb = new StringBuilder();
                sb.append("服务: ").append(LockService.running ? "运行中" : "未启动").append('\n');
                sb.append("端口: ").append(LockService.activePort).append('\n');
                sb.append("本机 IP: ").append(lanIps()).append('\n');
                sb.append("蓝牙监听: ").append(LockService.btListening ? "开" : "关").append('\n');
                sb.append("锁屏权限: ").append(adminOk ? "已激活" : "未激活").append('\n');
                sb.append("应用锁: ").append(applockText).append('\n');
                sb.append("最近事件: ").append(LockService.lastEvent);
                tvStatus.setText(sb.toString());
                btnAdmin.setText(adminOk ? "① 锁屏权限已激活 ✓" : getString(R.string.btn_admin));
                ui.postDelayed(this, 1000);
            }
        };
        ui.post(r);
    }

    static String lanIps() {
        StringBuilder sb = new StringBuilder();
        try {
            Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
            while (nis.hasMoreElements()) {
                NetworkInterface ni = nis.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                Enumeration<InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    if (a instanceof Inet4Address && !a.isLoopbackAddress()) {
                        if (sb.length() > 0) sb.append("  ");
                        sb.append(a.getHostAddress());
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return sb.length() == 0 ? "（未连接 Wi-Fi）" : sb.toString();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == CAMERA_REQ && grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startScan();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == SCAN_REQ && resultCode == RESULT_OK && data != null) {
            if (applyPair(data.getStringExtra("text"))) {
                Toast.makeText(this, "配对成功：端口和令牌已自动填好", Toast.LENGTH_LONG).show();
            } else {
                Toast.makeText(this, "二维码不是 Yui Lock 配对码", Toast.LENGTH_SHORT).show();
            }
        }
    }
}
