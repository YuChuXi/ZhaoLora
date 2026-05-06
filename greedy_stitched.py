import os
import argparse
from collections import Counter
from PIL import Image

def graph_stitch(input_dir, output_dir, threshold):
    valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    files = [f for f in os.listdir(input_dir) if os.path.splitext(f)[1].lower() in valid_exts]
    N = len(files)
    
    if N < 9:
        print("❌ 文件夹内图片总数不足 9 张，无法构成任何九宫格。")
        return
        
    print(f"✅ 扫描到 {N} 张图片，开始分析尺寸并提取边缘特征...")

    full_paths = [os.path.join(input_dir, f) for f in files]
    raw_images = []
    widths, heights = [], []
    
    # 第 1 次遍历：纯读取尺寸，找出众数作为“标准参考值”
    for path in full_paths:
        img = Image.open(path).convert('RGB')
        raw_images.append(img)
        widths.append(img.width)
        heights.append(img.height)
        
    target_w = Counter(widths).most_common(1)[0][0]
    target_h = Counter(heights).most_common(1)[0][0]
    print(f"📏 推断出网格标准切片尺寸: 宽 {target_w}px, 高 {target_h}px")

    img_data = [] # 用于存储每张图的有效尺寸和边缘数据
    
    # 第 2 次遍历：逻辑提取边缘（不修改原图）
    for i, img in enumerate(raw_images):
        w, h = img.width, img.height
        
        # 确定“有效拼合尺寸” (Effective Dimensions)
        # 严重超标的图片（如底部截了长图），在逻辑上只取标准高度来匹配
        eff_w = target_w if w > target_w + 5 else w
        eff_h = target_h if h > target_h + 5 else h
        
        # 提取 1 像素边缘（注意：对于超标图，bottom 取的是 eff_h 处的缝隙）
        edge_top = list(img.crop((0, 0, eff_w, 1)).getdata())
        edge_bottom = list(img.crop((0, eff_h - 1, eff_w, eff_h)).getdata())
        edge_left = list(img.crop((0, 0, 1, eff_h)).getdata())
        edge_right = list(img.crop((eff_w - 1, 0, eff_w, eff_h)).getdata())
        
        img_data.append({
            'img': img,
            'eff_w': eff_w,
            'eff_h': eff_h,
            'edge_top': edge_top,
            'edge_bottom': edge_bottom,
            'edge_left': edge_left,
            'edge_right': edge_right
        })

    def calc_mad(e1, e2, length):
        total_diff = sum(abs(p1[0]-p2[0]) + abs(p1[1]-p2[1]) + abs(p1[2]-p2[2]) for p1, p2 in zip(e1, e2))
        return total_diff / (length * 3)

    # ================= 第 1 步：构建有向图邻接表 (严格尺寸剪枝) =================
    print("🧮 正在基于严格尺寸校验构建匹配网络...")
    adj_lr = {i: [] for i in range(N)}
    adj_tb = {i: [] for i in range(N)}

    for i in range(N):
        for j in range(N):
            if i == j: continue
            
            # 左右匹配：【高度必须严格一致】才计算 MSE/MAD
            if img_data[i]['eff_h'] == img_data[j]['eff_h']:
                mad_lr = calc_mad(img_data[i]['edge_right'], img_data[j]['edge_left'], img_data[i]['eff_h'])
                if mad_lr < threshold:
                    adj_lr[i].append(j)
                    
            # 上下匹配：【宽度必须严格一致】才计算 MSE/MAD
            if img_data[i]['eff_w'] == img_data[j]['eff_w']:
                mad_tb = calc_mad(img_data[i]['edge_bottom'], img_data[j]['edge_top'], img_data[i]['eff_w'])
                if mad_tb < threshold:
                    adj_tb[i].append(j)

    # ================= 第 2 步：寻找横排 (1x3) =================
    valid_rows = []
    for n1 in range(N):
        for n2 in adj_lr[n1]:
            for n3 in adj_lr[n2]:
                if n1 != n2 and n2 != n3 and n1 != n3:
                    valid_rows.append((n1, n2, n3))

    if not valid_rows:
        print("❌ 未发现有效横排。可能是阈值太严或杂图过多。")
        return

    # ================= 第 3 步：构建横排关系图 =================
    row_adj_tb = {i: [] for i in range(len(valid_rows))}
    
    for i, row_u in enumerate(valid_rows):
        for j, row_v in enumerate(valid_rows):
            if i == j: continue
            
            if (row_v[0] in adj_tb[row_u[0]] and 
                row_v[1] in adj_tb[row_u[1]] and 
                row_v[2] in adj_tb[row_u[2]]):
                
                if not set(row_u).intersection(set(row_v)):
                    row_adj_tb[i].append(j)

    # ================= 第 4 步：寻找九宫格 (3x3) =================
    grids = []
    used_images_sets = []

    for r1 in range(len(valid_rows)):
        for r2 in row_adj_tb[r1]:
            for r3 in row_adj_tb[r2]:
                if r1 != r2 and r2 != r3 and r1 != r3:
                    grid_nodes = list(valid_rows[r1]) + list(valid_rows[r2]) + list(valid_rows[r3])
                    
                    if len(set(grid_nodes)) == 9:
                        grid_set = set(grid_nodes)
                        if grid_set not in used_images_sets:
                            used_images_sets.append(grid_set)
                            grids.append((valid_rows[r1], valid_rows[r2], valid_rows[r3]))

    if not grids:
        print("❌ 拼出了部分横排，但无法闭合成 3x3 九宫格。")
        return

    # ================= 第 5 步：动态计算画布尺寸并裁切拼接 =================
    print(f"🎉 成功提取出 {len(grids)} 组九宫格！正在进行最终裁切与输出...")
    for group_idx, grid in enumerate(grids):
        # 提取第一行的 3 个宽度，和第一列的 3 个高度
        col_widths = [img_data[grid[0][c]]['eff_w'] for c in range(3)]
        row_heights = [img_data[grid[r][0]]['eff_h'] for r in range(3)]
        
        canvas_w = sum(col_widths)
        canvas_h = sum(row_heights)
        canvas = Image.new('RGB', (canvas_w, canvas_h))
        
        current_y = 0
        for r_idx, row in enumerate(grid):
            current_x = 0
            for c_idx, img_idx in enumerate(row):
                data = img_data[img_idx]
                patch = data['img']
                
                # 【关键点】：到拼合的最后一步，才对严重超标的图片进行实际裁切
                if patch.width != data['eff_w'] or patch.height != data['eff_h']:
                    patch = patch.crop((0, 0, data['eff_w'], data['eff_h']))
                    
                canvas.paste(patch, (current_x, current_y))
                current_x += data['eff_w']
                
            current_y += data['eff_h']
            
        out_file = os.path.join(output_dir, f"graph_result_{group_idx + 1}.jpg")
        canvas.save(out_file, quality=100) # 由于无损拼合，可以直接拉满画质
        print(f"✅ 第 {group_idx + 1} 组已保存至：{out_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="基于纯图论与严格尺寸剪枝的九宫格无损拼接工具")
    parser.add_argument("-i", "--input", type=str, required=True, help="输入文件夹路径")
    parser.add_argument("-o", "--output", type=str, default="output", help="输出路径 (默认: ./output)")
    parser.add_argument("-t", "--threshold", type=int, default=25, help="色差阈值 (默认: 25)")
    
    args = parser.parse_args()
    graph_stitch(input_dir=args.input, output_dir=args.output, threshold=args.threshold)