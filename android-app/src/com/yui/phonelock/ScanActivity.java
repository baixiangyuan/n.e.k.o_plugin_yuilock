package com.yui.phonelock;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.media.Image;
import android.media.ImageReader;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Size;
import android.view.Surface;
import android.view.TextureView;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.Toast;

import com.google.zxing.BinaryBitmap;
import com.google.zxing.MultiFormatReader;
import com.google.zxing.PlanarYUVLuminanceSource;
import com.google.zxing.Result;
import com.google.zxing.common.HybridBinarizer;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/**
 * 扫码配对：Camera2 预览 + ZXing 解码，识别 yuilock://pair?p=端口&t=令牌
 */
public class ScanActivity extends Activity {

    private TextureView textureView;
    private CameraManager cameraManager;
    private String cameraId;
    private CameraDevice cameraDevice;
    private CameraCaptureSession session;
    private ImageReader reader;
    private CaptureRequest.Builder requestBuilder;
    private HandlerThread bgThread;
    private Handler bgHandler;
    private final MultiFormatReader decoder = new MultiFormatReader();
    private volatile boolean done = false;

    private final TextureView.SurfaceTextureListener surfaceListener =
            new TextureView.SurfaceTextureListener() {
                @Override
                public void onSurfaceTextureAvailable(SurfaceTexture st, int w, int h) {
                    openCamera();
                }

                @Override
                public void onSurfaceTextureSizeChanged(SurfaceTexture st, int w, int h) {
                }

                @Override
                public boolean onSurfaceTextureDestroyed(SurfaceTexture st) {
                    return true;
                }

                @Override
                public void onSurfaceTextureUpdated(SurfaceTexture st) {
                }
            };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        FrameLayout fl = new FrameLayout(this);
        textureView = new TextureView(this);
        fl.addView(textureView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(fl);
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, 1);
            return;
        }
        textureView.setSurfaceTextureListener(surfaceListener);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == 1) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                if (textureView.isAvailable()) {
                    openCamera();
                } else {
                    textureView.setSurfaceTextureListener(surfaceListener);
                }
            } else {
                Toast.makeText(this, "需要相机权限才能扫码", Toast.LENGTH_SHORT).show();
                finish();
            }
        }
    }

    private void openCamera() {
        bgThread = new HandlerThread("yuilock-scan");
        bgThread.start();
        bgHandler = new Handler(bgThread.getLooper());
        cameraManager = (CameraManager) getSystemService(CAMERA_SERVICE);
        try {
            for (String id : cameraManager.getCameraIdList()) {
                CameraCharacteristics cc = cameraManager.getCameraCharacteristics(id);
                Integer facing = cc.get(CameraCharacteristics.LENS_FACING);
                if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) {
                    cameraId = id;
                    break;
                }
            }
            if (cameraId == null) {
                String[] all = cameraManager.getCameraIdList();
                if (all.length == 0) {
                    fail("没有可用相机");
                    return;
                }
                cameraId = all[0];
            }
            CameraCharacteristics cc = cameraManager.getCameraCharacteristics(cameraId);
            Size[] yuvSizes = cc.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
                    .getOutputSizes(ImageFormat.YUV_420_888);
            final Size yuvSize = choose(yuvSizes, 1280);
            Size[] texSizes = cc.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
                    .getOutputSizes(SurfaceTexture.class);
            final Size texSize = choose(texSizes, 1280);

            reader = ImageReader.newInstance(yuvSize.getWidth(), yuvSize.getHeight(),
                    ImageFormat.YUV_420_888, 2);
            reader.setOnImageAvailableListener(imageListener, bgHandler);

            cameraManager.openCamera(cameraId, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice cam) {
                    cameraDevice = cam;
                    try {
                        SurfaceTexture st = textureView.getSurfaceTexture();
                        st.setDefaultBufferSize(texSize.getWidth(), texSize.getHeight());
                        Surface display = new Surface(st);
                        requestBuilder = cam.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                        requestBuilder.addTarget(display);
                        requestBuilder.addTarget(reader.getSurface());
                        cam.createCaptureSession(Arrays.asList(display, reader.getSurface()),
                                new CameraCaptureSession.StateCallback() {
                                    @Override
                                    public void onConfigured(CameraCaptureSession s) {
                                        session = s;
                                        try {
                                            s.setRepeatingRequest(requestBuilder.build(), null, bgHandler);
                                        } catch (Exception e) {
                                            fail(e);
                                        }
                                    }

                                    @Override
                                    public void onConfigureFailed(CameraCaptureSession s) {
                                        fail("会话创建失败");
                                    }
                                }, bgHandler);
                    } catch (Exception e) {
                        fail(e);
                    }
                }

                @Override
                public void onDisconnected(CameraDevice cam) {
                    cam.close();
                }

                @Override
                public void onError(CameraDevice cam, int error) {
                    cam.close();
                    fail("相机错误 " + error);
                }
            }, bgHandler);
        } catch (Exception e) {
            fail(e);
        }
    }

    private static Size choose(Size[] sizes, int maxDim) {
        List<Size> list = new ArrayList<>(Arrays.asList(sizes));
        Collections.sort(list, (a, b) ->
                Integer.compare(b.getWidth() * b.getHeight(), a.getWidth() * a.getHeight()));
        for (Size s : list) {
            if (s.getWidth() <= maxDim && s.getHeight() <= maxDim) {
                return s;
            }
        }
        return list.get(list.size() - 1);
    }

    private final ImageReader.OnImageAvailableListener imageListener =
            new ImageReader.OnImageAvailableListener() {
                @Override
                public void onImageAvailable(ImageReader r) {
                    Image img = null;
                    try {
                        img = r.acquireLatestImage();
                        if (img == null || done) {
                            return;
                        }
                        String text = decode(img);
                        if (text != null) {
                            done = true;
                            Intent data = new Intent();
                            data.putExtra("text", text);
                            setResult(RESULT_OK, data);
                            finish();
                        }
                    } catch (Throwable ignored) {
                    } finally {
                        if (img != null) {
                            img.close();
                        }
                    }
                }
            };

    private String decode(Image img) {
        Image.Plane plane = img.getPlanes()[0];
        ByteBuffer buf = plane.getBuffer();
        byte[] y = new byte[buf.remaining()];
        buf.get(y);
        int w = img.getWidth();
        int h = img.getHeight();
        com.google.zxing.LuminanceSource src = new PlanarYUVLuminanceSource(
                y, plane.getRowStride(), h, 0, 0, w, h, false);
        for (int rot = 0; rot < 4; rot++) {
            Result r = tryDecode(src);
            if (r != null) {
                return r.getText();
            }
            src = src.rotateCounterClockwise();
        }
        return null;
    }

    private Result tryDecode(com.google.zxing.LuminanceSource src) {
        try {
            return decoder.decode(new BinaryBitmap(new HybridBinarizer(src)));
        } catch (Exception e) {
            return null;
        } finally {
            decoder.reset();
        }
    }

    private void fail(Object e) {
        Toast.makeText(this, "相机启动失败: " + e, Toast.LENGTH_LONG).show();
        finish();
    }

    private void closeCamera() {
        try {
            if (session != null) {
                session.close();
                session = null;
            }
        } catch (Exception ignored) {
        }
        try {
            if (cameraDevice != null) {
                cameraDevice.close();
                cameraDevice = null;
            }
        } catch (Exception ignored) {
        }
        try {
            if (reader != null) {
                reader.close();
                reader = null;
            }
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onPause() {
        closeCamera();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        closeCamera();
        if (bgThread != null) {
            bgThread.quitSafely();
        }
        super.onDestroy();
    }
}
