package com.yui.phonelock;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * 指令解析：TCP / 蓝牙两条通道共用。协议为 NDJSON。
 *
 * 安全设计：
 * - ping 无需认证（只返回无害状态）；
 * - lock/applock/unlock 必须通过挑战-应答认证，且挑战按连接隔离（Session）：
 *   客户端第一包 {cmd, nonce} → 手机回一次性 challenge →
 *   客户端同连接回 {cmd, nonce, proof = HMAC-SHA256(token, "cmd|nonce|challenge")}；
 *   proof 与命令、nonce、挑战三者绑定，不可挪用、不可重放，连接之间互不打断；
 * - 控制命令不带 proof 直接 ok=true 的情况不存在（服务端不会这么回）；
 * - 手机端 token 为空时直接拒绝一切控制命令。
 */
public final class CommandHandler {

    /** 每条连接一个：挑战与所属命令/nonce 绑定，取后即焚，60 秒过期。 */
    public static final class Session {
        private static final SecureRandom RANDOM = new SecureRandom();
        String challenge = "";
        String cmd = "";
        String nonce = "";
        long at = 0;

        void issue(String cmd, String nonce) {
            StringBuilder sb = new StringBuilder(32);
            for (int i = 0; i < 32; i++) {
                sb.append("0123456789abcdef".charAt(RANDOM.nextInt(16)));
            }
            this.challenge = sb.toString();
            this.cmd = cmd;
            this.nonce = nonce == null ? "" : nonce;
            this.at = System.currentTimeMillis();
        }

        String take(String cmd, String nonce) {
            if (challenge.isEmpty()
                    || !this.cmd.equals(cmd)
                    || !this.nonce.equals(nonce == null ? "" : nonce)
                    || System.currentTimeMillis() - at > 60_000) {
                challenge = "";
                return "";
            }
            String c = challenge;
            challenge = "";
            this.cmd = "";
            this.nonce = "";
            return c;
        }
    }

    public interface Host {
        String token();

        boolean isAdminActive();

        boolean isAppLockOn();

        void lockScreen();

        /** 返回 false 表示权限不足等，未开启（不落盘状态） */
        boolean setAppLock(boolean on);

        int battery();

        /** 最近事件（含失败原因），随 ping 返回给电脑端展示 */
        String appLockNote();
    }

    private CommandHandler() {
    }

    public static String handle(String line, Host h, Session session) {
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
            String nonce = req.optString("nonce", "");
            if (proof.isEmpty()) {
                session.issue(cmd, nonce);
                JSONObject o = new JSONObject();
                o.put("ok", false);
                o.put("error", "需要配对验证");
                o.put("auth", "hmac-sha256");
                o.put("challenge", session.challenge);
                return o.toString();
            }
            String challenge = session.take(cmd, nonce);
            if (challenge.isEmpty()) {
                return fail("验证挑战无效或已过期，请重新发起命令");
            }
            String want = hmacSha256Hex(expect, cmd + "|" + nonce + "|" + challenge);
            if (!constantTimeEquals(want, proof)) {
                return fail("令牌验证失败");
            }
            // 执行；最终响应（成功与失败都算）带 HMAC 签名（覆盖 challenge/命令/nonce/结果），
            // 客户端核对通过才采信，防中间人把成功改成失败或把失败改成成功。
            JSONObject resp;
            switch (cmd) {
                case "lock":
                    if (!h.isAdminActive()) {
                        resp = failJson("手机未激活设备管理器，请先在 Yui Lock 应用里完成第①步");
                        break;
                    }
                    h.lockScreen();
                    resp = okJson("lock");
                    break;
                case "applock":
                    resp = h.setAppLock(true) ? okJson("applock")
                            : failJson("开启失败：请先授予「使用情况访问权限」和「显示悬浮窗」权限");
                    break;
                case "unlock":
                    h.setAppLock(false);
                    resp = okJson("unlock");
                    break;
                default:
                    return fail("未知命令");
            }
            resp.put("sig", hmacSha256Hex(expect, responseSigMessage(
                    cmd, nonce, challenge, resp.optBoolean("ok"), resp.optString("error", ""))));
            return resp.toString();
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
            o.put("applock_note", h.appLockNote() == null ? "" : h.appLockNote());
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

    static String responseSigMessage(String cmd, String nonce, String challenge,
                                     boolean ok, String error) {
        return "resp|" + cmd + "|" + nonce + "|" + challenge + "|"
                + (ok ? "1" : "0") + "|" + (error == null ? "" : error);
    }

    private static JSONObject okJson(String action) {
        JSONObject o = new JSONObject();
        try {
            o.put("ok", true);
            o.put("action", action);
        } catch (Exception ignored) {
        }
        return o;
    }

    private static JSONObject failJson(String msg) {
        JSONObject o = new JSONObject();
        try {
            o.put("ok", false);
            o.put("error", msg);
        } catch (Exception ignored) {
        }
        return o;
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
