package com.yui.phonelock;

import android.os.Build;

import org.json.JSONObject;

/**
 * 指令解析：TCP / 蓝牙两条通道共用。
 * 协议为 NDJSON：收到一行 JSON，回一行 JSON。
 * 命令：lock（熄屏锁屏）/ applock（应用锁）/ unlock（解除应用锁）/ ping（状态）。
 */
public final class CommandHandler {

    public interface Host {
        String token();

        boolean isAdminActive();

        boolean isAppLockOn();

        void lockScreen();

        void setAppLock(boolean on);

        int battery();
    }

    private CommandHandler() {
    }

    public static String handle(String line, Host h) {
        try {
            JSONObject req = new JSONObject(line.trim());
            String cmd = req.optString("cmd", "");
            String expect = h.token();
            String got = req.optString("token", "");
            if (expect != null && !expect.isEmpty() && !expect.equals(got)) {
                return fail("令牌不匹配，请检查电脑端插件的 token 设置");
            }
            if ("lock".equals(cmd)) {
                if (!h.isAdminActive()) {
                    return fail("手机未激活设备管理器，请先在 Yui Lock 应用里完成第①步");
                }
                h.lockScreen();
                return ok("lock");
            }
            if ("applock".equals(cmd)) {
                h.setAppLock(true);
                return ok("applock");
            }
            if ("unlock".equals(cmd)) {
                h.setAppLock(false);
                return ok("unlock");
            }
            if ("ping".equals(cmd) || "status".equals(cmd)) {
                JSONObject o = new JSONObject();
                o.put("ok", true);
                o.put("action", "ping");
                o.put("device", Build.MODEL);
                o.put("android", Build.VERSION.RELEASE);
                o.put("admin", h.isAdminActive());
                o.put("applock", h.isAppLockOn());
                o.put("battery", h.battery());
                return o.toString();
            }
            return fail("未知命令: " + cmd);
        } catch (Exception e) {
            return fail("请求格式错误");
        }
    }

    private static String ok(String action) {
        try {
            JSONObject o = new JSONObject();
            o.put("ok", true);
            o.put("action", action);
            return o.toString();
        } catch (Exception e) {
            return "{\"ok\":true}";
        }
    }

    private static String fail(String msg) {
        try {
            JSONObject o = new JSONObject();
            o.put("ok", false);
            o.put("error", msg);
            return o.toString();
        } catch (Exception e) {
            return "{\"ok\":false}";
        }
    }
}
