import cv2
import numpy as np

def get_jpeg_blockiness_score(img_gray):
    """计算 JPEG 8x8 网格效应得分"""
    if img_gray is None:
        return 0
    img = img_gray.astype(np.float32)
    
    diff_h = np.abs(img[:, :-1] - img[:, 1:])
    diff_v = np.abs(img[:-1, :] - img[1:, :])
    
    h_bounds = diff_h[:, 7::8].mean()
    h_non_bounds = np.delete(diff_h, np.arange(7, diff_h.shape[1], 8), axis=1).mean()
    
    v_bounds = diff_v[7::8, :].mean()
    v_non_bounds = np.delete(diff_v, np.arange(7, diff_v.shape[0], 8), axis=0).mean()
    
    score = (h_bounds / (h_non_bounds + 1e-5)) + (v_bounds / (v_non_bounds + 1e-5))
    return score

def on_trackbar(val):
    """滑动条的回调函数"""
    quality = val
    # OpenCV 的滑块可能滑到 0，JPG质量不能为0，做个保护
    if quality == 0: 
        quality = 1 
        
    # 1. 压缩并重新解码
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    result, encimg = cv2.imencode('.jpg', img_bgr, encode_param)
    decimg = cv2.imdecode(encimg, 1)
    
    # 2. 计算得分
    gray = cv2.cvtColor(decimg, cv2.COLOR_BGR2GRAY)
    score = get_jpeg_blockiness_score(gray)
    
    # 3. 在图片左上角绘制文字
    display_img = decimg.copy()
    # 得分大于 2.5 变红 (OpenCV 颜色是 BGR 格式：蓝, 绿, 红)，否则是绿色
    color = (0, 0, 255) if score > 2.5 else (0, 255, 0) 
    
    text1 = f"JPG Quality: {quality}"
    text2 = f"Score: {score:.3f}"
    
    # 绘制带黑色描边的文字，防止在亮色背景下看不清
    cv2.putText(display_img, text1, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 4)
    cv2.putText(display_img, text1, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)
    cv2.putText(display_img, text2, (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 4)
    cv2.putText(display_img, text2, (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)
    
    # 4. 刷新窗口
    cv2.imshow('JPEG Compression Test', display_img)

# ================= 1. 读取并缩放原图 =================
IMAGE_PATH = "datasets/chawenjian_washed/1_zkz/1775579373339.png" # 填入你的图片路径

img_bgr = cv2.imread(IMAGE_PATH)
if img_bgr is None:
    print("未找到图片，生成测试用例...")
    img_bgr = np.zeros((600, 800, 3), dtype=np.uint8)
    for i in range(800):
        img_bgr[:, i] = (i // 3, i // 3, 255 - i // 3)
    cv2.putText(img_bgr, "No Image", (250, 300), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
else:
    # 强制限制最大分辨率，防止超大图片拖垮 CPU
    h, w = img_bgr.shape[:2]
    if w > 1200 or h > 900:
        scale = min(1200 / w, 900 / h)
        img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)))

# ================= 2. 初始化 OpenCV 窗口 =================
cv2.namedWindow('JPEG Compression Test')

# 创建滑动条 (名称, 窗口名称, 初始值, 最大值, 回调函数)
cv2.createTrackbar('Quality', 'JPEG Compression Test', 100, 100, on_trackbar)

# 手动触发一次回调，显示初始画面
on_trackbar(100)

# ================= 3. 保持窗口运行 =================
print("窗口已开启。选中图片窗口后，按键盘上的 'ESC' 键退出。")
while True:
    # 监听键盘输入，27 是 ESC 键的 ASCII 码
    if cv2.waitKey(1) & 0xFF == 27: 
        break

# 销毁所有窗口，释放内存
cv2.destroyAllWindows()