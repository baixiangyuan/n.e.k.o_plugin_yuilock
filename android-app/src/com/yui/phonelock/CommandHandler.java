package com.yui.phonelock;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * 指令解析：TCP / 蓝牙两条通道共用。协议为 NDJSON。
 *
 * 安全设计：
 * - ping 无需认证（只返回无害状态）；
 * - lock/applock/unlock 必须通过挑战-应答认证：手机下发一次性 challenge，
 *   客户端回 proof = HMAC-SHA256(token, challenge)。token 不在网络上明文出现，
 *   challenge 单次有效且 60 秒过期，无法重放；
 * - 手机端 token 为空时直接拒绝一切控制命令。
 */
public final class CommandHandler {

    public interface Host {
        String token();

        boolean isAdminActive();

        boolean isAppLockOn();

        void lockScreen();

        /** 返回 false 表示权限不足等，未开启（不落盘状态） */
        boolean setAppLock(boolean on);

        int battery();

        /** 生成并存一次性挑战 */
        String newChallenge();

        /** 取走当前挑战（取后即焚）；无或过期返回 "" */
        String takeChallenge();
    }

    private CommandHandler() {
    }

    public static String handle(String line, Host h) {
        try {
            JSONObject req = new JSONObject(line.trim());
            String cmd = req.optString("cmd", "");
            if ("ping".equals(cmd) || "status".equals(cmd)) {
                return ping(h);
            }
            boolean control = "lock".equals(cmd) || "applock".equals(cmd) || "unlock".equals(cmd);
            if (!control) {
                return fail("未知命令: " + cmd);
            }
            String expect = h.token();
            if (expect == null || expect.isEmpty()) {
                return fail("手机端未设置配对令牌，已拒绝控制命令（请在 Yui Lock 设置里配置令牌）");
            }
            String proof = req.optString("proof", "");
            if (proof.isEmpty()) {
                JSONObject o = new JSONObject();
                o.put("ok", false);
                o.put("error", "需要配对验证");
                o.put("auth", "hmac-sha256");
                o.put("challenge", h.newChallenge());
                return o.toString();
            }
            String challenge = h.takeChallenge();
            if (challenge.isEmpty()) {
                return fail("验证挑战已过期，请重新发起命令");
            }
            String want = hmacSha256Hex(expect, challenge);
            if (!constantTimeEquals(want, proof)) {
                return fail("令牌验证失败");
            }
            switch (cmd) {
                case "lock":
                    if (!h.isAdminActive()) {
                        return fail("手机未激活设备管理器，请先在 Yui Lock 应用里完成第①步");
                    }
                    h.lockScreen();
                    return ok("lock");
                case "applock":
                    return h.setAppLock(true) ? ok("applock")
                            : fail("开启失败：请先在应用里授予“使用情况访问权限”");
                case "unlock":
                    h.setAppLock(false);
                    return ok("unlock");
                default:
                    return fail("未知命令");
            }
        } catch (Exception e) {
            return fail("请求格式错误");
        }
    }

    private static String ping(Host h) {
        try {
            JSONObject o = new JSONObject();
            o.put("ok", true);
            o.put("action", "ping");
            o.put("device", android.os.Build.MODEL);
            o.put("android", android.os.Build.VERSION.RELEASE);
            o.put("admin", h.isAdminActive());
            o.put("applock", h.isAppLockOn());
            o.put("battery", h.battery());
            o.put("token_set", h.token() != null && !h.token().isEmpty());
            return o.toString();
        } catch (Exception e) {
            return "{\"ok\":false}";
        }
    }

    static String hmacSha256Hex(String key, String msg) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            byte[] out = mac.doFinal(msg.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(out.length * 2);
            for (byte b : out) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    private static boolean constantTimeEquals(String a, String b) {
        if (a == null || b == null) {
            return false;
        }
        return MessageDigest.isEqual(
                a.getBytes(StandardCharsets.UTF_8),
                b.toLowerCase().getBytes(StandardCharsets.UTF_8));
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
